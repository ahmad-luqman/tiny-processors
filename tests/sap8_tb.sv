`timescale 1ns/1ps

module sap8_tb;
    reg clk = 0;
    reg reset = 0;
    wire [7:0] program_address, data_address, data_write, pc, acc, out, retire_pc;
    wire data_write_enable, zero, negative, carry, overflow, halted, fault, retired;
    wire [1:0] state;
    wire [15:0] instruction, retire_instruction;
    reg [15:0] program_memory [0:255];
    reg [7:0] data_memory [0:255];
    reg [7:0] reference_memory [0:255];
    wire [15:0] program_data = program_memory[program_address];
    wire [7:0] data_read = data_memory[data_address];
    reg [7:0] ref_pc, ref_acc, ref_out;
    reg ref_z, ref_n, ref_c, ref_v, ref_halted, ref_fault;
    integer checked_instructions = 0;
    integer scenarios = 0;
    integer i, j, k;
    reg [31:0] random_state = 32'h51a8cafe;
    reg [7:0] random_opcode, random_operand;
    reg [1023:0] wave_path;

    sap8 dut (.*);
    always #5 clk = ~clk;
    always @(posedge clk)
        if (data_write_enable)
            data_memory[data_address] <= data_write;

    task prepare;
        integer address;
        begin
            for (address = 0; address < 256; address = address + 1) begin
                program_memory[address] = 16'h0800; // Unused code halts.
                data_memory[address] = 0;
            end
        end
    endtask

    task check_architecture;
        integer address;
        begin
            if ({pc, acc, out, zero, negative, carry, overflow, halted, fault} !==
                {ref_pc, ref_acc, ref_out, ref_z, ref_n, ref_c, ref_v, ref_halted, ref_fault})
                $fatal(1, "Architectural mismatch at %t: pc=%h/%h acc=%h/%h out=%h/%h ZNCV=%b/%b halt=%b/%b fault=%b/%b",
                       $time, pc, ref_pc, acc, ref_acc, out, ref_out,
                       {zero, negative, carry, overflow}, {ref_z, ref_n, ref_c, ref_v},
                       halted, ref_halted, fault, ref_fault);
            for (address = 0; address < 256; address = address + 1)
                if (data_memory[address] !== reference_memory[address])
                    $fatal(1, "Memory[%h] mismatch: expected=%h got=%h",
                           address[7:0], reference_memory[address], data_memory[address]);
        end
    endtask

    task reset_cpu;
        integer address;
        reg [7:0] old_pc, old_acc, old_out;
        reg [3:0] old_flags;
        begin
            scenarios = scenarios + 1;
            for (address = 0; address < 256; address = address + 1)
                reference_memory[address] = data_memory[address];
            @(negedge clk);
            old_pc = pc; old_acc = acc; old_out = out;
            old_flags = {zero, negative, carry, overflow};
            reset = 1;
            #1;
            if (data_write_enable !== 0)
                $fatal(1, "Reset failed to suppress a store");
            if ({pc, acc, out, zero, negative, carry, overflow} !==
                {old_pc, old_acc, old_out, old_flags})
                $fatal(1, "Synchronous reset changed registers between edges");
            @(posedge clk);
            #1;
            ref_pc = 0; ref_acc = 0; ref_out = 0;
            ref_z = 1; ref_n = 0; ref_c = 0; ref_v = 0;
            ref_halted = 0; ref_fault = 0;
            check_architecture;
            if ({state, instruction, retired, retire_pc, retire_instruction} !== 43'd0)
                $fatal(1, "Reset did not clear control/retirement registers");
            @(negedge clk);
            reset = 0;
        end
    endtask

    // Instruction-level reference interpreter: wide integer arithmetic and
    // signed range checks, with no reuse of DUT ALU or control equations.
    task execute_one;
        reg [7:0] address, operand, opcode;
        reg [15:0] word;
        integer av, bv, value, signed_a, signed_b, signed_value;
        begin
            if (state !== 0 || halted !== 0)
                $fatal(1, "Expected FETCH before instruction");
            address = ref_pc;
            word = program_memory[address];
            opcode = word[15:8]; operand = word[7:0];
            if (program_address !== address || data_write_enable !== 0)
                $fatal(1, "Bad fetch bus signals");

            @(posedge clk); #1;
            ref_pc = ref_pc + 8'd1;
            check_architecture;
            if (state !== 1 || instruction !== word || retired !== 0 || data_write_enable !== 0)
                $fatal(1, "FETCH must latch instruction and enter DECODE");
            @(negedge clk); #1;
            check_architecture;
            if (state !== 1 || instruction !== word)
                $fatal(1, "Control changed between rising edges");

            @(posedge clk); #1;
            check_architecture;
            if (state !== 2 || retired !== 0 || instruction !== word ||
                data_address !== operand || data_write !== ref_acc ||
                data_write_enable !== (opcode == 2))
                $fatal(1, "DECODE must enter EXECUTE with the correct memory bus");
            @(negedge clk); #1;
            check_architecture;

            av = {24'd0, ref_acc};
            bv = {24'd0, reference_memory[operand]};
            signed_a = (av < 128) ? av : av - 256;
            signed_b = (bv < 128) ? bv : bv - 256;
            case (opcode)
                0, 1: begin
                    ref_acc = (opcode == 0) ? operand : reference_memory[operand];
                    ref_z = (ref_acc == 0); ref_n = (ref_acc >= 128);
                    ref_c = 0; ref_v = 0;
                end
                2: reference_memory[operand] = ref_acc;
                3, 4: begin
                    value = (opcode == 3) ? av + bv : av - bv;
                    signed_value = (opcode == 3) ? signed_a + signed_b : signed_a - signed_b;
                    ref_acc = value[7:0];
                    ref_z = (ref_acc == 0); ref_n = (ref_acc >= 128);
                    ref_c = (opcode == 3) ? (value > 255) : (av >= bv);
                    ref_v = (signed_value < -128 || signed_value > 127);
                end
                5: ref_pc = operand;
                6: if (ref_z) ref_pc = operand;
                7: ref_out = ref_acc;
                8: ref_halted = 1;
                default: begin ref_fault = 1; ref_halted = 1; end
            endcase
            @(posedge clk); #1;
            check_architecture;
            if (state !== (ref_halted ? 2'd3 : 2'd0) || retired !== !ref_fault ||
                data_write_enable !== 0)
                $fatal(1, "Incorrect execute/stop transition");
            if (!ref_fault && {retire_pc, retire_instruction} !== {address, word})
                $fatal(1, "Incorrect retirement metadata");
            checked_instructions = checked_instructions + 1;
            if ($test$plusargs("trace")) begin
                $display("%t pc=%h instruction=%h next=%h acc=%h ZNCV=%b out=%h halt=%b fault=%b",
                         $time, address, word, pc, acc, {zero, negative, carry, overflow}, out, halted, fault);
                if (opcode == 2)
                    $display("         memory[%h] <- %h", operand, ref_acc);
            end
        end
    endtask

    task run_to_stop;
        integer steps;
        begin
            steps = 0;
            while (!ref_halted && steps < 256) begin
                execute_one;
                steps = steps + 1;
            end
            if (!ref_halted)
                $fatal(1, "Program exceeded 256 instructions");
            // Halt/fault must hold state and suppress stores on subsequent edges.
            repeat (3) begin
                @(posedge clk); #1;
                check_architecture;
                if (retired !== 0 || state !== 3 || data_write_enable !== 0)
                    $fatal(1, "STOP failed to hold or retirement pulse did not clear");
            end
        end
    endtask

    initial begin
        $timeformat(-9, 0, " ns", 8);
        if ($value$plusargs("wave=%s", wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(0, sap8_tb.dut);
        end

        // Hand-encoded addition: save 7, load 5, add saved value, output 12.
        prepare;
        program_memory[0] = 16'h0007;
        program_memory[1] = 16'h02f0;
        program_memory[2] = 16'h0005;
        program_memory[3] = 16'h03f0;
        program_memory[4] = 16'h0700;
        reset_cpu;
        run_to_stop;
        if (out !== 12 || data_memory[240] !== 7 || fault !== 0)
            $fatal(1, "Hand-encoded addition failed");
        if ($test$plusargs("examples-only")) begin
            $display("PASS: SAP8 hand-encoded addition (%0d instructions)", checked_instructions);
            $finish;
        end

        // Arithmetic boundary cases with independently stated final flags.
        for (i = 0; i < 5; i = i + 1) begin
            prepare;
            case (i)
                0: begin program_memory[0] = 16'h00ff; data_memory[255] = 1; end
                1: begin program_memory[0] = 16'h007f; data_memory[255] = 1; end
                2: begin program_memory[0] = 16'h0080; data_memory[255] = 128; end
                3: begin program_memory[0] = 16'h0000; data_memory[255] = 1; end
                4: begin program_memory[0] = 16'h0080; data_memory[255] = 1; end
            endcase
            program_memory[1] = (i < 3) ? 16'h03ff : 16'h04ff;
            program_memory[2] = 16'h02fe; // STA, OUT, JMP, HLT preserve flags.
            program_memory[3] = 16'h07ab; // Ignored operand is deliberately nonzero.
            program_memory[4] = 16'h0506;
            program_memory[6] = 16'h08ff;
            reset_cpu;
            run_to_stop;
            case (i)
                0: if ({acc, zero, negative, carry, overflow} !== 12'h0_0a) $fatal(1, "Unsigned wrap");
                1: if ({acc, zero, negative, carry, overflow} !== 12'h8_05) $fatal(1, "Positive overflow");
                2: if ({acc, zero, negative, carry, overflow} !== 12'h0_0b) $fatal(1, "Negative overflow");
                3: if ({acc, zero, negative, carry, overflow} !== 12'hf_f4) $fatal(1, "Borrow");
                4: if ({acc, zero, negative, carry, overflow} !== 12'h7_f3) $fatal(1, "Subtraction overflow");
            endcase
        end

        // Reset Z takes the first branch. Sequential fetch at ff wraps to 00;
        // the same JZ is now not taken because LDI changed the stored Z flag.
        prepare;
        program_memory[0] = 16'h06fe;
        program_memory[254] = 16'h002a;
        program_memory[255] = 16'h0700;
        reset_cpu;
        run_to_stop;
        if (out !== 42 || pc !== 2 || fault !== 0)
            $fatal(1, "PC wrap or taken/untaken JZ failed");

        // LDA updates flags and clears prior C/V. Data address zero is valid.
        prepare;
        data_memory[0] = 128;
        program_memory[0] = 16'h0080;
        program_memory[1] = 16'h0300;
        program_memory[2] = 16'h0100;
        reset_cpu;
        run_to_stop;
        if ({acc, zero, negative, carry, overflow} !== 12'h804)
            $fatal(1, "LDA flag behavior failed");

        // Reset in FETCH, DECODE, and EXECUTE; in the latter a store is pending.
        for (i = 0; i < 3; i = i + 1) begin
            prepare;
            program_memory[0] = 16'h0063;
            program_memory[1] = 16'h02ff;
            data_memory[255] = 8'ha5;
            reset_cpu;
            execute_one;
            repeat (i) begin @(posedge clk); #1; end
            reset_cpu;
            if (data_memory[255] !== 8'ha5)
                $fatal(1, "Reset committed a pending store");
            // Restart from address zero, then complete the original program.
            run_to_stop;
            if (data_memory[255] !== 99)
                $fatal(1, "Execution failed after reset");
        end

        // Every unsupported opcode faults without a store or retirement.
        for (i = 9; i < 256; i = i + 1) begin
            prepare;
            program_memory[0] = {i[7:0], 8'hff};
            reset_cpu;
            run_to_stop;
            if (fault !== 1 || pc !== 1 || acc !== 0)
                $fatal(1, "Illegal opcode %h did not fault safely", i[7:0]);
        end

        // Deterministic mixed programs. Branches only move forward so every
        // program terminates; directed tests above cover backward PC wrap.
        for (i = 0; i < 12; i = i + 1) begin
            prepare;
            for (j = 0; j < 256; j = j + 1) begin
                random_state = random_state * 32'd1664525 + 32'd1013904223;
                data_memory[j] = random_state[31:24];
            end
            for (k = 0; k < 63; k = k + 1) begin
                random_state = random_state * 32'd1664525 + 32'd1013904223;
                random_opcode = {5'd0, random_state[26:24]};
                random_operand = random_state[15:8];
                if (random_opcode == 5 || random_opcode == 6)
                    random_operand = 8'(k + 1 + (int'(random_operand) % (63 - k)));
                program_memory[k] = {random_opcode, random_operand};
            end
            reset_cpu;
            run_to_stop;
            if (fault !== 0)
                $fatal(1, "Valid mixed program faulted");
        end
        $display("PASS: SAP8 (%0d checked instructions, %0d reset scenarios)", checked_instructions, scenarios);
        $finish;
    end

    initial begin
        #1000000;
        $fatal(1, "SAP8 simulation timed out");
    end
endmodule
