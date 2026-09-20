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
    wire [3:0] mem_strb, fault_cause;
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
    reg stall_given = 0, random_stall = 0;
    integer delay = 0;
    reg delay_chosen = 0;
    integer request_age = 0;
    reg stalled_request = 0;
    wire [69:0] request = {mem_fetch, mem_we, mem_strb, mem_addr, mem_wdata};
    reg [69:0] held_request;    // the request as it was on the first stalled edge
    integer cycles = 0, steps = 0, stalls = 0, transfers = 0;
    integer max_cycles = 1000000;

    // The last accepted data transaction, printed at the next retirement.
    reg pending = 0, pending_write = 0;
    reg [31:0] pending_addr, pending_value;
    integer pending_width;
    reg done_pending = 0;
    reg [31:0] done_word;

    string image_path, trace_path, wave_path, console_path, text;
    integer trace_fd = 0, console_fd = 0;
    integer image_words = 0;
    integer fd, i;
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
                mem_error = !(mem_addr[2:0] == 3'd0 && mem_strb == 4'b0001); // TX byte only
            else if (mem_addr[2:0] == 3'd5 && mem_strb == 4'b0010) begin
                mem_rdata = 32'h0000_2000; // status byte 0x20 in lane 1 of the word at +4
                mem_error = 1'b0;          // a wider read of +4 is refused: the strobe says the width
            end
        end else if (mem_addr == DONE_ADDR) begin
            mem_error = !(mem_we && mem_strb == 4'b1111);
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
            if (console_fd != 0) $fclose(console_fd);
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
            if (stalled_request && (mem_valid !== 1'b1 || request !== held_request))
                $fatal(1, "Request changed while stalled at cycle %0d", cycles);
            if (mem_valid) begin
                if (mem_ready) begin
                    transfers = transfers + 1;
                    request_age = 0;
                    stalled_request = 0;
                    delay_chosen = 0;
                    // An accepted write takes effect on exactly one device.
                    if (!mem_error && mem_we) begin
                        if (in_ram) begin
                            if (mem_strb[0]) ram[ram_index][7:0] <= mem_wdata[7:0];
                            if (mem_strb[1]) ram[ram_index][15:8] <= mem_wdata[15:8];
                            if (mem_strb[2]) ram[ram_index][23:16] <= mem_wdata[23:16];
                            if (mem_strb[3]) ram[ram_index][31:24] <= mem_wdata[31:24];
                        end else if (in_console) begin
                            // The guest console: a file when +console is given, else stdout.
                            if (console_fd != 0) $fwrite(console_fd, "%c", mem_wdata[7:0]);
                            else $write("%c", mem_wdata[7:0]);
                        end else if (mem_addr == DONE_ADDR) begin
                            done_pending = 1;
                            done_word = mem_wdata;
                        end
                    end
                    if (!mem_fetch) begin
                        if (pending)
                            $fatal(1, "Two data transactions without a retirement between them");
                        pending = 1;
                        pending_write = mem_we;
                        pending_addr = mem_addr;
                        // Both directions show the strobed lanes: a store's written
                        // bytes, a load's raw bytes before the core extends them.
                        pending_value = narrowed(mem_strb, mem_we ? mem_wdata : mem_rdata);
                        pending_width = width_of(mem_strb);
                    end
                end else begin
                    stalls = stalls + 1;
                    request_age = request_age + 1;
                    stalled_request = 1;
                    held_request = request;
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

    // Numeric plusargs are read as text and must round-trip through %0d, so
    // "abc", "3junk", an empty value, or a number past 2^31-1 is refused
    // instead of silently becoming zero or wrapping.
    function integer plusarg_count;
        input string name;
        input string value_text;
        input integer minimum;
        integer value;
        begin
            // %d accepts x and z as digits, so an unknown value is refused explicitly.
            if ($sscanf(value_text, "%d", value) != 1 || (^value) === 1'bx ||
                $sformatf("%0d", value) != value_text || value < minimum)
                $fatal(1, "+%0s=%0s must be a decimal of at least %0d", name, value_text, minimum);
            plusarg_count = value;
        end
    endfunction

    // Count the image's words so $readmemh gets an exact range (Icarus warns
    // when a file is shorter than the whole array, and the RAM is 1M words)
    // and refuse anything that is not a hex word, because Icarus's $readmemh
    // keeps going after a bad token and the run would look like a decode fault.
    function integer count_words;
        input string path;
        integer fd;
        string token;
        reg [31:0] value;
        begin
            count_words = 0;
            fd = $fopen(path, "r");
            if (fd == 0) $fatal(1, "Cannot open %0s", path);
            // A word must read back as itself: eight lowercase hex digits, no x/z.
            while ($fscanf(fd, "%s", token) == 1) begin
                if ($sscanf(token, "%h", value) != 1 || (^value) === 1'bx || $sformatf("%h", value) != token)
                    $fatal(1, "Image %0s word %0d: '%0s' is not an eight-digit hex word", path, count_words, token);
                count_words = count_words + 1;
            end
            $fclose(fd);
        end
    endfunction

    initial begin
        if (!$value$plusargs("image=%s", image_path)) $fatal(1, "Missing +image=FILE");
        if ($value$plusargs("stall=%s", text)) begin
            stall = plusarg_count("stall", text, 0);
            stall_given = 1;
        end
        if ($value$plusargs("stall-seed=%s", text)) begin
            stall_seed = plusarg_count("stall-seed", text, 0);
            random_stall = 1;
        end
        if (stall_given && random_stall) $fatal(1, "+stall and +stall-seed are exclusive");
        if ($value$plusargs("max-cycles=%s", text))
            max_cycles = plusarg_count("max-cycles", text, 1);
        if ($value$plusargs("wave=%s", wave_path)) begin
            fd = $fopen(wave_path, "w"); // prove the path is writable: Verilator drops a bad VCD silently
            if (fd == 0) $fatal(1, "Cannot open wave file %0s", wave_path);
            $fclose(fd);
            $dumpfile(wave_path);
            $dumpvars(0, rv32_tb.dut);
        end
        if ($value$plusargs("trace=%s", trace_path)) begin
            trace_fd = $fopen(trace_path, "w");
            if (trace_fd == 0) $fatal(1, "Cannot open trace file %0s", trace_path);
        end
        // With +console the guest's bytes go to a file and stdout carries only
        // simulator diagnostics, so the runner can fail a run on any stdout output
        // without guessing which lines are the simulator's.
        if ($value$plusargs("console=%s", console_path)) begin
            console_fd = $fopen(console_path, "w");
            if (console_fd == 0) $fatal(1, "Cannot open console file %0s", console_path);
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
