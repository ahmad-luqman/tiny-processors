`timescale 1ns/1ps

// Testbench for the RV32 machine (rtl/rv32/rv32_soc.v): the host side of
// the devices (console bytes, the done word), a stall generator that holds
// the bus, the image loader, and the retirement trace printer that
// reproduces docs/rv32-emulator.md's trace contract. Contract for its
// plusargs, counters, and halt line: docs/rv32-rtl.md.
module rv32_tb;
    parameter integer RAM_WORDS = 1048576; // the contract's 4 MiB
    parameter integer CONSOLE_BUSY = 0;    // cycles the console waits before each byte
    parameter integer FB_WORDS = 19200;    // 320 x 240 pixels
    localparam integer STDERR = 32'h8000_0002;

    reg clk = 0;
    reg reset = 1;
    wire mem_valid, mem_we, mem_fetch, mem_ready, mem_error, retire, retire_rd_we, trap, halted;
    wire [31:0] mem_addr, mem_wdata, mem_rdata, retire_pc, retire_insn, retire_rd_value, trap_value, pc;
    wire [31:0] mtvec, mepc, mcause, mtval;
    wire [3:0] mem_strb, trap_cause;
    wire [4:0] retire_rd;
    wire [2:0] state;
    wire console_valid, done_valid, display_present, in_full;
    wire [7:0] console_byte;
    wire [31:0] done_wdata, display_frames;
    reg in_push = 0;
    reg [31:0] in_event = 0;

    // The input script (+input=FILE): `frame N down|up KEY` lines, delivered in
    // order; an event's frame must have been reached before it is pushed.
    localparam integer MAX_EVENTS = 4096;
    integer event_frame [0:MAX_EVENTS-1];
    reg [31:0] event_word [0:MAX_EVENTS-1];
    integer events = 0, next_event = 0;
    integer frame_reached = 0; // the frame count the guest has observably reached
    reg mem_hold = 1; // acceptance deferred until the stall generator releases it

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
    integer max_cycles = 10000000; // +max-cycles=N; the diagnostic needs about two million
    integer reset_at = 0;       // +reset-at=N: assert reset again after counted cycle N

    // The last accepted data transaction, printed at the next retirement.
    reg pending = 0, pending_write = 0, pending_error = 0;
    reg [31:0] pending_addr, pending_value;
    integer pending_width;
    reg done_pending = 0;
    reg [31:0] done_word;

    string image_path, trace_path, wave_path, console_path, checkpoints_path, input_path, text;
    integer trace_fd = 0, console_fd = 0, checkpoints_fd = 0;
    integer image_words = 0;
    integer fd, i;
    reg finished = 0;

    rv32_soc #(.RAM_WORDS(RAM_WORDS), .FB_WORDS(FB_WORDS), .CONSOLE_BUSY(CONSOLE_BUSY)) dut (.*);
    // The clock stops when the run ends, so the simulation drains without
    // $finish: Icarus and Verilator then exit silently and stdout stays the
    // guest console only.
    initial while (!finished) #5 clk = ~clk;

    // Hold the bus away from the sampling edge; the delay is chosen once per
    // request. The machine answers in the cycle the hold is released, so
    // `mem_ready` is what this block used to drive directly.
    always @(negedge clk) begin
        if (reset || !mem_valid) begin
            mem_hold = 1;
            delay_chosen = 0;
        end else begin
            if (!delay_chosen) begin
                delay = random_stall ? ($unsigned($random(stall_seed)) % 4) : stall;
                delay_chosen = 1;
            end
            mem_hold = !(request_age >= delay);
        end
    end

    // The host's keyboard: one event per cycle, as soon as its frame has been
    // reached (frame 0 from reset release). The device refuses guest accesses
    // while a push is presented, so a frame's events arrive as one burst.
    always @(negedge clk) begin
        in_push = 0;
        if (!reset && next_event < events && event_frame[next_event] <= frame_reached) begin
            if (in_full) begin
                $fwrite(STDERR, "rv32_tb: input queue full: dropped frame %0d event %h\n",
                        event_frame[next_event], event_word[next_event]);
            end else begin
                in_push = 1;
                in_event = event_word[next_event];
            end
            next_event = next_event + 1;
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

    // The checkpoint hash of docs/rv32.md: shifts and adds over the framebuffer's words in
    // address order, read through the hierarchy the way the host would read a display's memory.
    function [31:0] frame_hash;
        input integer words;
        integer k;
        reg [31:0] h;
        begin
            h = 32'd5381;
            for (k = 0; k < words; k = k + 1)
                h = ((h << 5) + h) ^ dut.fb.mem[k];
            frame_hash = h;
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
            end else if (halt_name == "double-fault") begin
                // The trap that could not be delivered; the first one is in the CSRs.
                $fwrite(STDERR, " cause=%0d tval=%h error=double-fault", trap_cause, trap_value);
            end else begin
                $fwrite(STDERR, " error=limit"); // even if a done store was accepted but never retired
            end
            $fwrite(STDERR, "\n");
            if (trace_fd != 0) $fclose(trace_fd);
            if (console_fd != 0) $fclose(console_fd);
            if (checkpoints_fd != 0) $fclose(checkpoints_fd);
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
                    // The host side of the devices: the machine's strobes say
                    // what was accepted in this cycle, once.
                    if (console_valid) begin
                        // The guest console: a file when +console is given, else stdout.
                        if (console_fd != 0) $fwrite(console_fd, "%c", console_byte);
                        else $write("%c", console_byte);
                    end
                    if (done_valid) begin
                        done_pending = 1;
                        done_word = done_wdata;
                    end
                    // A present snapshots the framebuffer as it is at acceptance: every earlier
                    // store has landed, this cycle's frame number is the count plus one.
                    if (display_present) begin
                        frame_reached = display_frames + 1;
                        if (checkpoints_fd != 0)
                            $fwrite(checkpoints_fd, "frame %0d %h\n", display_frames + 1, frame_hash(FB_WORDS));
                    end
                    if (!mem_fetch) begin
                        if (pending)
                            $fatal(1, "Two data transactions without a retirement between them");
                        pending = 1;
                        pending_write = mem_we;
                        pending_error = mem_error;
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
            if (trap) begin
                steps = steps + 1; // a trap counts as a step, as in the emulator
                if (trace_fd != 0)
                    $fwrite(trace_fd, "%0d %h %h trap %0d %h\n", steps, retire_pc, retire_insn, trap_cause, trap_value);
                // A refused load or store was an accepted transaction with error:
                // the trap line replaces its effect, so nothing carries over. A
                // write that was accepted without error has taken effect, and the
                // contract says a trapping instruction has none: that is a core bug.
                if (pending && pending_write && !pending_error)
                    $fatal(1, "Trap after an accepted write at cycle %0d", cycles);
                pending = 0;
            end
            // One outcome per run: a terminal outcome on the edge that also
            // reaches the cycle limit is reported as that outcome, not as a limit.
            if (retire && done_pending) begin
                finish_run("done");
            end else if (halted) begin
                finish_run("double-fault");
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

    // Upper case by hand: Icarus has no string toupper method.
    function string upper;
        input string text;
        integer k;
        byte c;
        begin
            upper = "";
            for (k = 0; k < text.len(); k = k + 1) begin
                c = text[k];
                if (c >= "a" && c <= "z") c = c - 32;
                upper = {upper, string'(c)};
            end
        end
    endfunction

    // A decimal of at most nine digits, or -1. Written by hand because $sscanf's %d accepts
    // x and z as digits and Verilator and Icarus count a failed %s differently.
    function integer decimal_of;
        input string text;
        integer k;
        byte c;
        begin
            decimal_of = (text.len() >= 1 && text.len() <= 9) ? 0 : -1;
            for (k = 0; k < text.len() && decimal_of >= 0; k = k + 1) begin
                c = text[k];
                if (c >= "0" && c <= "9") decimal_of = 10 * decimal_of + ({24'd0, c} - 32'd48);
                else decimal_of = -1;
            end
        end
    endfunction

    // A key name from board.h, any case, or a number 0..31; -1 for anything else.
    // (An if-chain rather than a case: Icarus 13 cannot compile a case on a string.)
    function integer key_code;
        input string name;
        string u;
        integer value;
        begin
            u = upper(name);
            value = decimal_of(name);
            if (u == "LEFT") key_code = 1;
            else if (u == "RIGHT") key_code = 2;
            else if (u == "UP") key_code = 3;
            else if (u == "DOWN") key_code = 4;
            else if (u == "SPACE") key_code = 5;
            else if (u == "ENTER") key_code = 6;
            else if (u == "ESCAPE") key_code = 7;
            else if (u == "A") key_code = 8;
            else if (u == "D") key_code = 9;
            else if (u == "W") key_code = 10;
            else if (u == "S") key_code = 11;
            else if (u == "P") key_code = 12;
            else if (u == "Q") key_code = 13;
            else if (u == "R") key_code = 14;
            else if (value >= 0 && value < 32) key_code = value;
            else key_code = -1;
        end
    endfunction

    // Read the input script (docs/rv32.md, "Input"): blank lines and `#` comments are skipped;
    // every other line is `frame N down|up KEY` with frames never decreasing. Lines are split
    // on blanks by hand so both simulators agree on what a token is.
    task read_input_script;
        input string path;
        integer fd, number, frame, code, last_frame, k, tokens;
        string line, token0, token1, token2, token3, extra;
        byte c;
        reg in_token;
        begin
            fd = $fopen(path, "r");
            if (fd == 0) $fatal(1, "Cannot open input script %0s", path);
            number = 0;
            last_frame = 0;
            while ($fgets(line, fd) != 0) begin
                number = number + 1;
                tokens = 0;
                in_token = 0;
                token0 = ""; token1 = ""; token2 = ""; token3 = ""; extra = "";
                for (k = 0; k < line.len(); k = k + 1) begin
                    c = line[k];
                    if (c == 8'h20 || c == 8'h09 || c == 8'h0A || c == 8'h0D) begin // blank, tab, LF, CR: Icarus has no \r escape
                        in_token = 0;
                    end else begin
                        if (!in_token) begin
                            tokens = tokens + 1;
                            in_token = 1;
                        end
                        case (tokens)
                            1: token0 = {token0, string'(c)};
                            2: token1 = {token1, string'(c)};
                            3: token2 = {token2, string'(c)};
                            4: token3 = {token3, string'(c)};
                            default: extra = {extra, string'(c)};
                        endcase
                    end
                end
                if (tokens == 0 || token0[0] == "#") continue;
                if (tokens != 4 || token0 != "frame" || (token2 != "down" && token2 != "up"))
                    $fatal(1, "Input script %0s line %0d: expected `frame N down|up KEY`", path, number);
                frame = decimal_of(token1);
                if (frame < 0) $fatal(1, "Input script %0s line %0d: frame %0s is not a number", path, number, token1);
                code = key_code(token3);
                if (code < 0) $fatal(1, "Input script %0s line %0d: unknown key %0s", path, number, token3);
                if (frame < last_frame)
                    $fatal(1, "Input script %0s line %0d: frame %0d comes after frame %0d", path, number, frame, last_frame);
                if (events == MAX_EVENTS) $fatal(1, "Input script %0s has more than %0d events", path, MAX_EVENTS);
                event_frame[events] = frame;
                event_word[events] = 32'h8000_0000 | ((token2 == "down") ? 32'h100 : 32'h0) | {27'd0, code[4:0]};
                events = events + 1;
                last_frame = frame;
            end
            $fclose(fd);
        end
    endtask

    initial begin
        if (!$value$plusargs("image=%s", image_path)) $fatal(1, "Missing +image=FILE");
        if ($value$plusargs("input=%s", input_path)) read_input_script(input_path);
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
        if ($value$plusargs("reset-at=%s", text))
            reset_at = plusarg_count("reset-at", text, 1);
        if ($value$plusargs("wave=%s", wave_path)) begin
            fd = $fopen(wave_path, "w"); // prove the path is writable: Verilator drops a bad VCD silently
            if (fd == 0) $fatal(1, "Cannot open wave file %0s", wave_path);
            $fclose(fd);
            $dumpfile(wave_path);
            $dumpvars(0, rv32_tb.dut.core); // the whole core, as before the SoC existed
            $dumpvars(1, rv32_tb.dut);      // plus the bus boundary and the device strobes
        end
        if ($value$plusargs("trace=%s", trace_path)) begin
            trace_fd = $fopen(trace_path, "w");
            if (trace_fd == 0) $fatal(1, "Cannot open trace file %0s", trace_path);
        end
        if ($value$plusargs("checkpoints=%s", checkpoints_path)) begin
            checkpoints_fd = $fopen(checkpoints_path, "w");
            if (checkpoints_fd == 0) $fatal(1, "Cannot open checkpoints file %0s", checkpoints_path);
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
        // The emulator allocates zero-filled RAM; unwritten words must read zero
        // here too. The image is loaded through the hierarchy: the RAM module
        // has no initial block, so synthesis never sees a file.
        for (i = 0; i < RAM_WORDS; i = i + 1)
            dut.ram.mem[i] = 32'd0;
        for (i = 0; i < FB_WORDS; i = i + 1)
            dut.fb.mem[i] = 32'd0; // unspecified by the contract; zero like the emulator's calloc
        $readmemh(image_path, dut.ram.mem, 0, image_words - 1);
        repeat (2) @(posedge clk);
        #1 reset = 0;
        // A second reset in the middle of the run, two edges long like the first: the
        // machine restarts, whatever transaction was in flight is abandoned (a held write
        // never lands), and the host forgets its own bookkeeping of that transaction. The
        // counters and the trace continue; the reset edges are not counted.
        if (reset_at > 0) begin
            wait (cycles == reset_at);
            #2; // after this edge's trace processing, before the falling edge
            reset = 1;
            pending = 0;
            stalled_request = 0;
            request_age = 0;
            done_pending = 0;
            repeat (2) @(posedge clk);
            #1 reset = 0;
        end
    end
endmodule
