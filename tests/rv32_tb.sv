`timescale 1ns/1ps

// Testbench for the RV32 multicycle core: the RAM, the console, the done
// register, a stall generator on the ready/valid port, and the retirement
// trace printer that reproduces docs/rv32-emulator.md's trace contract.
// Contract for its plusargs, counters, and halt line: docs/rv32-rtl.md.
module rv32_tb;
    parameter integer RAM_WORDS = 1048576; // the contract's 4 MiB
    localparam [31:0] RAM_BASE = 32'h8000_0000;
    localparam [31:0] CONSOLE_BASE = 32'h1000_0000;
    localparam [31:0] DONE_ADDR = 32'h0010_0000;
    localparam [31:0] RAM_BYTES = RAM_WORDS * 4;
    localparam integer STDERR = 32'h8000_0002;

    reg clk = 0;
    reg reset = 1;
    wire mem_valid, mem_we, mem_fetch, retire, retire_rd_we, halted, fault, unsupported;
    wire [31:0] mem_addr, mem_wdata, retire_pc, retire_insn, retire_rd_value, fault_value, pc;
    wire [3:0] mem_wstrb, fault_cause;
    wire [4:0] retire_rd;
    wire [2:0] state;
    reg mem_ready = 0;
    reg [31:0] mem_rdata;
    reg mem_error;

    reg [31:0] ram [0:RAM_WORDS-1];
    wire in_ram = (mem_addr >= RAM_BASE) && ((mem_addr - RAM_BASE) < RAM_BYTES);
    wire [31:0] ram_index = (mem_addr - RAM_BASE) >> 2;
    wire [31:0] ram_word = ram[ram_index]; // a continuous read keeps the array out of @* sensitivity
    wire in_console = (mem_addr[31:3] == CONSOLE_BASE[31:3]);

    // Stall generator and counters.
    integer stall = 0;          // +stall=N: fixed cycles per request
    integer stall_seed = 0;     // +stall-seed=S: 0..3 cycles per request from $random
    reg random_stall = 0;
    integer delay = 0;
    reg delay_chosen = 0;
    integer request_age = 0;
    reg stalled_request = 0;
    reg [69:0] held_request;    // {mem_fetch, mem_we, mem_wstrb, mem_addr, mem_wdata}
    integer cycles = 0, steps = 0, stalls = 0, transfers = 0;
    integer max_cycles = 1000000;

    // The last accepted data transaction, printed at the next retirement.
    reg pending = 0, pending_write = 0;
    reg [31:0] pending_addr, pending_value;
    integer pending_width;
    reg done_pending = 0;
    reg [31:0] done_word;

    string image_path, trace_path, wave_path;
    integer trace_fd = 0;
    integer image_words = 0;
    integer i;

    reg finished = 0;

    rv32 dut (.*);
    // The clock stops when the run ends, so the simulation drains without
    // $finish: Icarus and Verilator then exit silently and stdout stays the
    // guest console only.
    initial while (!finished) #5 clk = ~clk;

    // Response: combinational from the request, sampled by the core at the
    // accepting edge. Devices answer data accesses only; a fetch from one
    // is refused like any other unmapped fetch.
    always @* begin
        mem_rdata = 32'd0;
        mem_error = 1'b1;
        if (in_ram) begin
            mem_rdata = ram_word;
            mem_error = 1'b0;
        end else if (mem_fetch) begin
            mem_error = 1'b1;
        end else if (in_console) begin
            if (mem_we)
                mem_error = !(mem_addr[2:0] == 3'd0 && mem_wstrb == 4'b0001); // TX byte only
            else if (mem_addr[2:0] == 3'd5) begin
                mem_rdata = 32'h0000_2000; // status byte 0x20 in lane 1 of the word at +4
                mem_error = 1'b0;
            end
        end else if (mem_addr == DONE_ADDR) begin
            mem_error = !(mem_we && mem_wstrb == 4'b1111);
        end
    end

    // Drive ready away from the sampling edge; the delay is chosen once per request.
    always @(negedge clk) begin
        if (reset || !mem_valid) begin
            mem_ready = 0;
            delay_chosen = 0;
        end else begin
            if (!delay_chosen) begin
                delay = random_stall ? ($unsigned($random(stall_seed)) % 4) : stall;
                delay_chosen = 1;
            end
            mem_ready = (request_age >= delay);
        end
    end

    function [31:0] narrowed;
        input [3:0] strobe;
        input [31:0] data;
        begin
            case (strobe)
                4'b1111: narrowed = data;
                4'b0011: narrowed = {16'd0, data[15:0]};
                4'b1100: narrowed = {16'd0, data[31:16]};
                4'b0001: narrowed = {24'd0, data[7:0]};
                4'b0010: narrowed = {24'd0, data[15:8]};
                4'b0100: narrowed = {24'd0, data[23:16]};
                4'b1000: narrowed = {24'd0, data[31:24]};
                default: narrowed = data;
            endcase
        end
    endfunction

    function integer width_of;
        input [3:0] strobe;
        begin
            case (strobe)
                4'b1111: width_of = 4;
                4'b0011, 4'b1100: width_of = 2;
                default: width_of = 1;
            endcase
        end
    endfunction

    task finish_run;
        input string halt_name;
        begin
            $fwrite(STDERR, "rv32_tb: halt=%0s cycles=%0d steps=%0d stalls=%0d transfers=%0d",
                    halt_name, cycles, steps, stalls, transfers);
            if (halt_name == "done") begin
                $fwrite(STDERR, " done=%h", done_word);
                if (done_word == 32'h5555)
                    $fwrite(STDERR, " pass");
                else if (done_word[15:0] == 16'h3333 && done_word[31:16] >= 1 && done_word[31:16] <= 255)
                    $fwrite(STDERR, " fail=%0d", done_word[31:16]);
                else if (done_word == 32'h7777)
                    $fwrite(STDERR, " error=reserved-reset-word");
                else
                    $fwrite(STDERR, " error=undefined-done-word");
            end else if (halt_name == "fault") begin
                $fwrite(STDERR, " cause=%0d tval=%h error=fault", fault_cause, fault_value);
            end else if (halt_name == "unsupported") begin
                $fwrite(STDERR, " pc=%h word=%h error=unsupported", retire_pc, retire_insn);
            end else begin
                $fwrite(STDERR, " error=limit"); // even if a done store was accepted but never retired
            end
            $fwrite(STDERR, "\n");
            if (trace_fd != 0) $fclose(trace_fd);
            finished = 1;
        end
    endtask

    // Handshake bookkeeping samples the pre-edge request; the trace samples
    // the core's registered retirement just after the edge.
    always @(posedge clk) begin
        if (reset && (mem_valid !== 0 || mem_we !== 0))
            $fatal(1, "Request on the bus during reset");
        if (!reset) begin
            cycles = cycles + 1;
            // The contract: nothing about a request changes while it waits,
            // including on the edge that finally accepts it.
            if (stalled_request && {mem_valid, mem_fetch, mem_we, mem_wstrb, mem_addr, mem_wdata} !== {1'b1, held_request})
                $fatal(1, "Request changed while stalled at cycle %0d", cycles);
            if (mem_valid) begin
                if (mem_ready) begin
                    transfers = transfers + 1;
                    request_age = 0;
                    stalled_request = 0;
                    delay_chosen = 0;
                    if (!mem_error && mem_we && in_ram) begin
                        if (mem_wstrb[0]) ram[ram_index][7:0] <= mem_wdata[7:0];
                        if (mem_wstrb[1]) ram[ram_index][15:8] <= mem_wdata[15:8];
                        if (mem_wstrb[2]) ram[ram_index][23:16] <= mem_wdata[23:16];
                        if (mem_wstrb[3]) ram[ram_index][31:24] <= mem_wdata[31:24];
                    end
                    if (!mem_error && mem_we && in_console)
                        $write("%c", mem_wdata[7:0]);
                    if (!mem_error && mem_we && mem_addr == DONE_ADDR) begin
                        done_pending = 1;
                        done_word = mem_wdata;
                    end
                    if (!mem_fetch) begin
                        if (pending)
                            $fatal(1, "Two data transactions without a retirement between them");
                        pending = 1;
                        pending_write = mem_we;
                        pending_addr = mem_addr;
                        pending_value = mem_we ? narrowed(mem_wstrb, mem_wdata) : mem_rdata;
                        pending_width = mem_we ? width_of(mem_wstrb) : 4;
                    end
                end else begin
                    stalls = stalls + 1;
                    request_age = request_age + 1;
                    stalled_request = 1;
                    held_request = {mem_fetch, mem_we, mem_wstrb, mem_addr, mem_wdata};
                end
            end else begin
                request_age = 0;
                stalled_request = 0;
            end
        end
        #1;
        if (!reset) begin
            if (retire) begin
                steps = steps + 1;
                if (trace_fd != 0) begin
                    $fwrite(trace_fd, "%0d %h %h", steps, retire_pc, retire_insn);
                    if (retire_rd_we) $fwrite(trace_fd, " x%0d=%h", retire_rd, retire_rd_value);
                    if (pending && pending_write)
                        $fwrite(trace_fd, " mem[%h]<-%h/%0d", pending_addr, pending_value, pending_width);
                    if (pending && !pending_write)
                        $fwrite(trace_fd, " mem[%h]->%h/%0d", pending_addr, pending_value, pending_width);
                    $fwrite(trace_fd, "\n");
                end
                pending = 0;
            end
            // One outcome per run: a terminal outcome on the edge that also
            // reaches the cycle limit is reported as that outcome, not as a limit.
            if (retire && done_pending) begin
                finish_run("done");
            end else if (halted && fault) begin
                steps = steps + 1; // a trap counts as a step, as in the emulator
                if (trace_fd != 0)
                    $fwrite(trace_fd, "%0d %h %h trap %0d %h\n", steps, retire_pc, retire_insn, fault_cause, fault_value);
                finish_run("fault");
            end else if (halted) begin
                finish_run("unsupported");
            end else if (cycles >= max_cycles) begin
                finish_run("limit");
            end
        end
    end

    // Count the image's words so $readmemh gets an exact range: Icarus warns
    // when a file is shorter than the whole array, and the RAM is 1M words.
    // %s skips whitespace, so blank lines and trailing newlines do not count.
    function integer count_words;
        input string path;
        integer fd;
        string token;
        begin
            count_words = 0;
            fd = $fopen(path, "r");
            if (fd == 0) $fatal(1, "Cannot open %0s", path);
            while ($fscanf(fd, "%s", token) == 1)
                count_words = count_words + 1;
            $fclose(fd);
        end
    endfunction

    initial begin
        $timeformat(-9, 0, " ns", 8);
        if (!$value$plusargs("image=%s", image_path)) $fatal(1, "Missing +image=FILE");
        if ($value$plusargs("stall=%d", stall)) begin end
        if ($value$plusargs("stall-seed=%d", stall_seed)) random_stall = 1;
        if ($value$plusargs("max-cycles=%d", max_cycles)) begin end
        if (stall < 0 || max_cycles <= 0) $fatal(1, "+stall and +max-cycles must not be negative");
        if ($value$plusargs("wave=%s", wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(0, rv32_tb.dut);
        end
        if ($value$plusargs("trace=%s", trace_path)) begin
            trace_fd = $fopen(trace_path, "w");
            if (trace_fd == 0) $fatal(1, "Cannot open trace file %0s", trace_path);
        end
        image_words = count_words(image_path);
        if (image_words == 0 || image_words > RAM_WORDS) $fatal(1, "Image %0s has %0d words", image_path, image_words);
        // The emulator allocates zero-filled RAM; unwritten words must read zero here too.
        for (i = 0; i < RAM_WORDS; i = i + 1)
            ram[i] = 32'd0;
        $readmemh(image_path, ram, 0, image_words - 1);
        repeat (2) @(posedge clk);
        #1 reset = 0;
    end
endmodule
