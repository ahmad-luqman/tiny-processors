`timescale 1ns/1ps

module alu_tb;
    reg [7:0] a;
    reg [7:0] b;
    reg [2:0] op;
    wire [7:0] result;
    wire zero, negative, carry, overflow;
    integer checks = 0;
    integer ai, bi, oi;
    integer value, signed_a, signed_b, signed_value;
    reg expected_carry, expected_overflow;
    reg [7:0] expected_result;
    reg [1023:0] wave_path;

    alu dut (.a(a), .b(b), .op(op), .result(result), .zero(zero),
             .negative(negative), .carry(carry), .overflow(overflow));

    task check_vector;
        input [2:0] next_op;
        input [7:0] next_a, next_b, expected;
        input [3:0] flags; // Z, N, C, V
        begin
            op = next_op;
            a = next_a;
            b = next_b;
            // Allow combinational simulation events to settle; no DUT clock.
            #1;
            if ({result, zero, negative, carry, overflow} !== {expected, flags})
                $fatal(1, "Check %0d: op=%0d a=%h b=%h expected=%h ZNCV=%b got=%h ZNCV=%b",
                       checks, op, a, b, expected, flags, result,
                       {zero, negative, carry, overflow});
            checks = checks + 1;
            #9; // Each directed vector occupies 10 ns in the waveform.
        end
    endtask

    initial begin
        if ($value$plusargs("wave=%s", wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(0, alu_tb.dut);
        end

        // Let event-sensitive processes start before driving the first vector.
        // Inputs are unspecified until 10 ns; no power-up value is assumed.
        #10;
        // Hand-calculated examples anchor the independent exhaustive model.
        check_vector(0, 8'h00, 8'h00, 8'h00, 4'b1000);
        check_vector(0, 8'hff, 8'h01, 8'h00, 4'b1010);
        check_vector(0, 8'h7f, 8'h01, 8'h80, 4'b0101);
        check_vector(0, 8'h80, 8'h80, 8'h00, 4'b1011);
        check_vector(1, 8'h00, 8'h01, 8'hff, 4'b0100);
        check_vector(1, 8'h05, 8'h03, 8'h02, 4'b0010);
        check_vector(1, 8'h03, 8'h03, 8'h00, 4'b1010);
        check_vector(1, 8'h80, 8'h01, 8'h7f, 4'b0011);
        check_vector(1, 8'h7f, 8'hff, 8'h80, 4'b0101);
        check_vector(2, 8'haa, 8'h55, 8'h00, 4'b1000);
        check_vector(3, 8'haa, 8'h55, 8'hff, 4'b0100);
        check_vector(4, 8'haa, 8'h55, 8'hff, 4'b0100);
        check_vector(4, 8'haa, 8'haa, 8'h00, 4'b1000);
        check_vector(5, 8'h00, 8'h55, 8'hff, 4'b0100);
        check_vector(5, 8'hff, 8'haa, 8'h00, 4'b1000);
        check_vector(6, 8'h81, 8'h00, 8'h02, 4'b0010);
        check_vector(7, 8'h81, 8'hff, 8'h40, 4'b0010);
        check_vector(6, 8'h40, 8'hff, 8'h80, 4'b0100);
        check_vector(7, 8'h02, 8'h00, 8'h01, 4'b0000);
        $display("PASS: ALU directed examples (%0d checks)", checks);

        // A separate short run makes readable waves on both simulators.
        // The default run always includes the exhaustive tests below.
        if ($test$plusargs("examples-only"))
            $finish;

        // All 8 * 256 * 256 known input combinations. Interleave opcodes to
        // catch flags accidentally retained from the preceding operation.
        for (ai = 0; ai < 256; ai = ai + 1) begin
            for (bi = 0; bi < 256; bi = bi + 1) begin
                signed_a = (ai < 128) ? ai : ai - 256;
                signed_b = (bi < 128) ? bi : bi - 256;
                for (oi = 0; oi < 8; oi = oi + 1) begin
                    value = 0;
                    signed_value = 0;
                    expected_carry = 0;
                    expected_overflow = 0;
                    case (oi)
                        0: begin
                            value = ai + bi;
                            signed_value = signed_a + signed_b;
                            expected_carry = (value > 255);
                        end
                        1: begin
                            value = ai - bi;
                            signed_value = signed_a - signed_b;
                            expected_carry = (ai >= bi);
                        end
                        2: value = ai & bi;
                        3: value = ai | bi;
                        4: value = ai ^ bi;
                        5: value = 255 - ai;
                        6: begin
                            value = ai * 2;
                            expected_carry = (ai >= 128);
                        end
                        7: begin
                            value = ai / 2;
                            expected_carry = ((ai % 2) != 0);
                        end
                    endcase
                    if (oi < 2)
                        expected_overflow = (signed_value < -128 || signed_value > 127);
                    // Modulo 256 also handles a negative subtraction result.
                    expected_result = 8'((value + 256) % 256);
                    check_vector(oi[2:0], ai[7:0], bi[7:0], expected_result,
                                 {expected_result == 0, expected_result >= 128,
                                  expected_carry, expected_overflow});
                end
            end
        end
        $display("PASS: ALU (%0d checked vectors: 19 directed + 524288 exhaustive)", checks);
        $finish;
    end

    initial begin
        #6000000;
        $fatal(1, "Simulation timed out");
    end
endmodule
