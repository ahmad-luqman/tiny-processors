`timescale 1ns/1ps

module simd4_tb;
    parameter integer LANES = 4;
    localparam integer STATE_BITS = LANES * 96 + 24;
    localparam integer TRACE_BITS = STATE_BITS + 40;
    reg clk = 0;
    reg reset = 0;
    reg start = 0;
    reg [7:0] entry_pc = 0;
    wire busy, done, fault, mem_valid, mem_write, retired;
    wire [7:0] program_address, mem_address, pc, retire_pc;
    wire [15:0] mem_write_data, loop_count;
    wire [31:0] instruction, retire_instruction, cycles, stalls, memory_transfers, instructions;
    wire [1:0] state, memory_lane;
    wire [LANES*64-1:0] register_state;
    wire [LANES*32-1:0] accumulator_state;
    reg mem_ready = 0;
    reg [31:0] program_memory [0:255];
    reg [15:0] memory [0:255];
    reg [15:0] expected_memory [0:255];
    reg [15:0] saved_memory [0:255];
    reg [TRACE_BITS-1:0] expected_retirements [0:4095];
    reg [24:0] expected_transfers [0:4095];
    reg [STATE_BITS-1:0] expected_final [0:0];
    // Fixture metadata rows, in the order tools/simd4_run.py writes them.
    localparam integer META_RETIREMENTS = 0, META_TRANSFERS = 1, META_BASE_CYCLES = 2, META_FAULT = 3,
                       META_ENTRY = 4, META_STATE_BITS = 5, META_TRACE_BITS = 6, META_ROWS = 7;
    reg [31:0] metadata [0:META_ROWS-1];
    wire [31:0] program_data = program_memory[program_address];
    wire [15:0] mem_read_data = memory[mem_address];
    integer observed_cycles = 0;
    integer observed_stalls = 0;
    integer observed_transfers = 0;
    integer observed_retirements = 0;
    integer request_age = 0;
    integer wait_mode = 0;
    integer abort_after = -1;
    integer abort_at_retire = -1;
    integer launches = 1;
    integer i, run_number, timeout_cycles;
    reg checking = 0;
    reg stalled_request = 0;
    reg [24:0] held_request;
    reg [1:0] held_lane;
    reg [LANES*96+55:0] held_machine;
    reg [LANES*96-1:0] pre_reset_lane_state;
    string case_path, wave_path;

    simd4 #(.LANES(LANES)) dut (.*);
    always #5 clk = ~clk;

    function integer delay_for;
        input integer transfer_number;
        begin
            case (wait_mode)
                0: delay_for = 0;
                1: delay_for = 1;
                2: delay_for = 3;
                3: delay_for = transfer_number % 4;
                default: delay_for = 0;
            endcase
        end
    endfunction

    // Drive ready away from the sampling edge. Delay applies per request;
    // mode 0 accepts adjacent lane requests on consecutive rising edges.
    always @(negedge clk) begin
        if (reset || !mem_valid)
            mem_ready = 0;
        else if (abort_after >= 0 && observed_transfers >= abort_after)
            mem_ready = 0;
        else
            mem_ready = (request_age >= delay_for(observed_transfers));
    end

    // Scoreboard and memory responder sample the same pre-edge handshake.
    always @(posedge clk) begin
        if (reset || (start && !busy)) begin
            observed_cycles = 0;
            observed_stalls = 0;
            observed_transfers = 0;
            observed_retirements = 0;
            request_age = 0;
            stalled_request = 0;
        end else if (checking && busy) begin
            observed_cycles = observed_cycles + 1;
            if (stalled_request && (mem_valid !== 1 ||
                {mem_write, mem_address, mem_write_data} !== held_request ||
                memory_lane !== held_lane ||
                {pc, instruction, loop_count, accumulator_state, register_state} !== held_machine))
                $fatal(1, "Request or architectural state changed while stalled");
            if (mem_valid) begin
                if (mem_ready) begin
                    if (observed_transfers >= int'(metadata[META_TRANSFERS]) ||
                        {mem_write, mem_address, mem_write ? mem_write_data : mem_read_data} !==
                        expected_transfers[observed_transfers])
                        $fatal(1, "Transfer %0d differs from Python: write=%b address=%h data=%h",
                               observed_transfers, mem_write, mem_address,
                               mem_write ? mem_write_data : mem_read_data);
                    if (mem_write)
                        memory[mem_address] <= mem_write_data;
                    if ($test$plusargs("trace"))
                        $display("%t transfer lane=%0d write=%b address=%h data=%h", $time,
                                 memory_lane, mem_write, mem_address, mem_write ? mem_write_data : mem_read_data);
                    observed_transfers = observed_transfers + 1;
                    request_age = 0;
                    stalled_request = 0;
                end else begin
                    observed_stalls = observed_stalls + 1;
                    request_age = request_age + 1;
                    stalled_request = 1;
                    held_request = {mem_write, mem_address, mem_write_data};
                    held_lane = memory_lane;
                    held_machine = {pc, instruction, loop_count, accumulator_state, register_state};
                end
            end else begin
                request_age = 0;
                stalled_request = 0;
            end
        end
        #1;
        if (checking && !reset) begin
            if (retired) begin
                if (observed_retirements >= int'(metadata[META_RETIREMENTS]) ||
                    {retire_pc, retire_instruction, loop_count, pc, accumulator_state, register_state} !==
                    expected_retirements[observed_retirements])
                    $fatal(1, "Retirement %0d differs from Python: pc=%h instruction=%h acc=%h regs=%h",
                           observed_retirements, retire_pc, retire_instruction, accumulator_state, register_state);
                observed_retirements = observed_retirements + 1;
                if ($test$plusargs("trace"))
                    $display("%t retire pc=%h instruction=%h next=%h loop=%0d acc=%h regs=%h",
                             $time, retire_pc, retire_instruction, pc, loop_count, accumulator_state, register_state);
            end
            if (cycles !== observed_cycles || stalls !== observed_stalls ||
                memory_transfers !== observed_transfers || instructions !== observed_retirements)
                $fatal(1, "Performance counters differ from observed edges/transfers/retirements");
        end
    end

    task require_file;
        input string path;
        integer fd;
        begin
            fd = $fopen(path, "r");
            if (fd == 0) $fatal(1, "Cannot open %s", path);
            $fclose(fd);
        end
    endtask

    task launch;
        begin
            @(negedge clk);
            entry_pc = metadata[META_ENTRY][7:0];
            start = 1;
            @(posedge clk); #2;
            if (busy !== 1 || done !== 0 || fault !== 0 || register_state !== 0 || accumulator_state !== 0 ||
                loop_count !== 0 || cycles !== 0 || retired !== 0 || pc !== entry_pc)
                $fatal(1, "Launch did not initialize the machine");
            @(negedge clk);
            start = 0;
        end
    endtask

    task check_complete;
        begin
            timeout_cycles = 0;
            while (!done && timeout_cycles < 20000) begin
                @(posedge clk); #2;
                timeout_cycles = timeout_cycles + 1;
            end
            if (done !== 1 || busy !== 0 || fault !== metadata[META_FAULT][0] ||
                {loop_count, pc, accumulator_state, register_state} !== expected_final[0] ||
                instructions !== metadata[META_RETIREMENTS] || memory_transfers !== metadata[META_TRANSFERS] ||
                cycles !== metadata[META_BASE_CYCLES] + stalls)
                $fatal(1, "Final state/count mismatch or timeout");
            for (i = 0; i < 256; i = i + 1)
                if (memory[i] !== expected_memory[i])
                    $fatal(1, "Final memory[%0d]: expected %h, got %h", i, expected_memory[i], memory[i]);
            repeat (3) begin
                @(posedge clk); #2;
                if (busy !== 0 || done !== 1 || fault !== metadata[META_FAULT][0] ||
                    mem_valid !== 0 || retired !== 0 ||
                    {loop_count, pc, accumulator_state, register_state} !== expected_final[0])
                    $fatal(1, "Completed state did not hold");
            end
            $display("RESULT lanes=%0d wait=%0d cycles=%0d stalls=%0d transfers=%0d instructions=%0d fault=%0d",
                     LANES, wait_mode, cycles, stalls, memory_transfers, instructions, fault);
        end
    endtask

    initial begin
        $timeformat(-9, 0, " ns", 8);
        if (!$value$plusargs("case=%s", case_path)) $fatal(1, "Missing +case fixture prefix");
        if ($value$plusargs("wait=%d", wait_mode)) begin end
        if ($value$plusargs("abort-after=%d", abort_after)) begin end
        if ($value$plusargs("abort-at-retire=%d", abort_at_retire)) begin end
        if ($test$plusargs("relaunch")) launches = 2;
        require_file({case_path, ".meta.hex"});
        require_file({case_path, ".program.hex"});
        require_file({case_path, ".memory.hex"});
        require_file({case_path, ".expected-memory.hex"});
        require_file({case_path, ".final.hex"});
        $readmemh({case_path, ".meta.hex"}, metadata);
        // Icarus leaves missing rows X, and X compares false under two-state operators, so
        // the guards below use four-state comparisons after an explicit unknown check.
        for (i = 0; i < META_ROWS; i = i + 1)
            if ($isunknown(metadata[i])) $fatal(1, "Fixture metadata row %0d is missing", i);
        if (metadata[META_RETIREMENTS] > 4096 || metadata[META_TRANSFERS] > 4096 ||
            metadata[META_FAULT] > 1 || metadata[META_ENTRY] > 255)
            $fatal(1, "Invalid fixture metadata");
        // A fixture packed for another snapshot layout would otherwise be truncated or
        // zero-filled by $readmemh with a warning on Icarus and no diagnostic on Verilator.
        if (metadata[META_STATE_BITS] !== STATE_BITS)
            $fatal(1, "Fixture snapshot width %0d differs from STATE_BITS %0d", metadata[META_STATE_BITS], STATE_BITS);
        if (metadata[META_TRACE_BITS] !== TRACE_BITS)
            $fatal(1, "Fixture record width %0d differs from TRACE_BITS %0d", metadata[META_TRACE_BITS], TRACE_BITS);
        $readmemh({case_path, ".program.hex"}, program_memory);
        $readmemh({case_path, ".expected-memory.hex"}, expected_memory);
        $readmemh({case_path, ".final.hex"}, expected_final);
        if (metadata[META_RETIREMENTS] > 0) begin
            require_file({case_path, ".retire.hex"});
            $readmemh({case_path, ".retire.hex"}, expected_retirements, 0, metadata[META_RETIREMENTS] - 1);
        end
        if (metadata[META_TRANSFERS] > 0) begin
            require_file({case_path, ".transfers.hex"});
            $readmemh({case_path, ".transfers.hex"}, expected_transfers, 0, metadata[META_TRANSFERS] - 1);
        end
        if ($value$plusargs("wave=%s", wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(0, simd4_tb.dut);
        end
        @(negedge clk); reset = 1;
        @(posedge clk); #2;
        if (busy !== 0 || done !== 0 || fault !== 0 || register_state !== 0 || accumulator_state !== 0 || cycles !== 0)
            $fatal(1, "Initial reset failed");
        @(negedge clk); reset = 0;
        checking = 1;

        if (abort_after >= 0 || abort_at_retire >= 0) begin
            $readmemh({case_path, ".memory.hex"}, memory);
            launch;
            timeout_cycles = 0;
            // abort-after stops on a pending memory request; abort-at-retire stops while the
            // instruction after the N-th retirement sits in EXECUTE, so reset cancels its write.
            while (!((abort_after >= 0 && mem_valid && observed_transfers >= abort_after) ||
                     (abort_at_retire >= 0 && observed_retirements >= abort_at_retire && state == 2'd2)) &&
                   timeout_cycles < 10000) begin
                @(posedge clk); #2;
                timeout_cycles = timeout_cycles + 1;
            end
            if (timeout_cycles == 10000) $fatal(1, "Abort point was never reached");
            for (i = 0; i < 256; i = i + 1) saved_memory[i] = memory[i];
            pre_reset_lane_state = {accumulator_state, register_state};
            // The accumulator cases must reach their reset with a live accumulator in every
            // lane, or the clear below would only show that zero resets to zero.
            if ($test$plusargs("expect-nonzero-acc"))
                for (i = 0; i < LANES; i = i + 1)
                    if (accumulator_state[i*32 +: 32] == 0)
                        $fatal(1, "Abort point reached with lane %0d's accumulator still zero", i);
            @(negedge clk); reset = 1;
            #1;
            if (mem_valid !== 0 || {accumulator_state, register_state} !== pre_reset_lane_state)
                $fatal(1, "Reset must cancel request immediately but reset registers synchronously");
            @(posedge clk); #2;
            if (busy !== 0 || done !== 0 || fault !== 0 || register_state !== 0 || accumulator_state !== 0 ||
                loop_count !== 0 || cycles !== 0 || stalls !== 0 || memory_transfers !== 0 || instructions !== 0)
                $fatal(1, "Reset failed to clear an in-flight kernel");
            for (i = 0; i < 256; i = i + 1)
                if (memory[i] !== saved_memory[i]) $fatal(1, "Reset changed completed memory effects");
            @(negedge clk); reset = 0;
            abort_after = -1;
            abort_at_retire = -1;
        end

        for (run_number = 0; run_number < launches; run_number = run_number + 1) begin
            $readmemh({case_path, ".memory.hex"}, memory);
            launch;
            // A competing launch while busy must not change PC or clear state.
            @(negedge clk); start = 1; entry_pc = 8'hee;
            @(negedge clk); start = 0;
            check_complete;
        end
        $display("PASS: SIMD4 Python snapshots, transfers, memory, handshake, and lifecycle checks");
        $finish;
    end

    initial begin
        #1000000;
        $fatal(1, "SIMD4 simulation timed out");
    end
endmodule
