`timescale 1ns/1ps

// Three clocks per instruction; asynchronous memory reads, edge-triggered writes.
module sap8 (
    input  wire        clk,
    input  wire        reset,
    output wire [7:0]  program_address,
    input  wire [15:0] program_data,
    output wire [7:0]  data_address,
    input  wire [7:0]  data_read,
    output wire [7:0]  data_write,
    output wire        data_write_enable,
    output reg  [7:0]  pc,
    output reg  [7:0]  acc,
    output reg  [7:0]  out,
    output reg         zero,
    output reg         negative,
    output reg         carry,
    output reg         overflow,
    output reg         halted,
    output reg         fault,
    output reg  [1:0]  state,
    output reg  [15:0] instruction,
    output reg         retired,
    output reg  [7:0]  retire_pc,
    output reg  [15:0] retire_instruction
);
    localparam [1:0] FETCH = 2'd0, DECODE = 2'd1,
                     EXECUTE = 2'd2, STOP = 2'd3;
    localparam [7:0] LDI = 8'h00, LDA = 8'h01, STA = 8'h02,
                     ADD = 8'h03, SUB = 8'h04, JMP = 8'h05,
                     JZ = 8'h06, OUT = 8'h07, HLT = 8'h08;

    reg [7:0] instruction_pc;
    wire [7:0] opcode = instruction[15:8];
    wire [7:0] operand = instruction[7:0];
    wire [7:0] alu_result;
    wire alu_zero, alu_negative, alu_carry, alu_overflow;
    wire [2:0] alu_op = (opcode == SUB) ? 3'd1 : 3'd0;

    assign program_address = pc;
    assign data_address = operand;
    assign data_write = acc;
    // The memory samples this combinational strobe at the execute edge.
    assign data_write_enable = !reset && !halted &&
                               (state == EXECUTE) && (opcode == STA);

    alu arithmetic (.a(acc), .b(data_read), .op(alu_op), .result(alu_result),
                    .zero(alu_zero), .negative(alu_negative),
                    .carry(alu_carry), .overflow(alu_overflow));

    always @(posedge clk) begin
        if (reset) begin
            pc <= 8'd0;
            acc <= 8'd0;
            out <= 8'd0;
            zero <= 1'b1;
            negative <= 1'b0;
            carry <= 1'b0;
            overflow <= 1'b0;
            halted <= 1'b0;
            fault <= 1'b0;
            state <= FETCH;
            instruction <= 16'd0;
            instruction_pc <= 8'd0;
            retired <= 1'b0;
            retire_pc <= 8'd0;
            retire_instruction <= 16'd0;
        end else begin
            retired <= 1'b0;
            case (state)
                FETCH: begin
                    instruction <= program_data;
                    instruction_pc <= pc;
                    pc <= pc + 8'd1;
                    state <= DECODE;
                end
                DECODE: state <= EXECUTE;
                EXECUTE: begin
                    retired <= 1'b1;
                    retire_pc <= instruction_pc;
                    retire_instruction <= instruction;
                    state <= FETCH;
                    case (opcode)
                        LDI, LDA: begin
                            acc <= (opcode == LDI) ? operand : data_read;
                            zero <= ((opcode == LDI) ? operand : data_read) == 8'd0;
                            negative <= (opcode == LDI) ? operand[7] : data_read[7];
                            carry <= 1'b0;
                            overflow <= 1'b0;
                        end
                        STA: begin end // External memory captures data_write.
                        ADD, SUB: begin
                            acc <= alu_result;
                            zero <= alu_zero;
                            negative <= alu_negative;
                            carry <= alu_carry;
                            overflow <= alu_overflow;
                        end
                        JMP: pc <= operand;
                        JZ: if (zero) pc <= operand;
                        OUT: out <= acc;
                        HLT: begin
                            halted <= 1'b1;
                            state <= STOP;
                        end
                        default: begin
                            retired <= 1'b0;
                            fault <= 1'b1;
                            halted <= 1'b1;
                            state <= STOP;
                        end
                    endcase
                end
                STOP: begin end // Registers retain state; reset restarts execution.
            endcase
        end
    end
endmodule
