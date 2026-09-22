`timescale 1ns/1ps

module simd4 #(
    parameter integer LANES = 4 // Supported configurations: 1, 2, 4.
) (
    input  wire clk,
    input  wire reset,
    input  wire start,
    input  wire [7:0] entry_pc,
    output wire busy,
    output reg done,
    output reg fault,
    output wire [7:0] program_address,
    input  wire [31:0] program_data,
    output wire mem_valid,
    output wire mem_write,
    output wire [7:0] mem_address,
    output wire [15:0] mem_write_data,
    input  wire mem_ready,
    input  wire [15:0] mem_read_data,
    output reg [7:0] pc,
    output reg [31:0] instruction,
    output reg [1:0] state,
    output reg [1:0] memory_lane,
    output reg [15:0] loop_count,
    output reg retired,
    output reg [7:0] retire_pc,
    output reg [31:0] retire_instruction,
    output reg [31:0] cycles,
    output reg [31:0] stalls,
    output reg [31:0] memory_transfers,
    output reg [31:0] instructions,
    output wire [LANES*64-1:0] register_state,
    output wire [LANES*32-1:0] accumulator_state
);
    localparam [1:0] IDLE = 2'd0, FETCH = 2'd1, EXECUTE = 2'd2, MEMORY = 2'd3;
    localparam [7:0] HLT = 8'h00, LDI = 8'h01, LANE = 8'h02, ADD = 8'h03,
                     ADDI = 8'h04, LOAD = 8'h05, STORE = 8'h06,
                     SETLOOP = 8'h07, LOOP = 8'h08, MUL = 8'h09,
                     MAC = 8'h0a, MACU = 8'h0b, CLRA = 8'h0c, RDA = 8'h0d;
    localparam [7:0] LAST_OPCODE = RDA;
    // Two-bit subtraction intentionally gives 3 when LANES=4.
    localparam [1:0] LAST_LANE = LANES[1:0] - 2'd1;
    localparam integer LANE_BITS = (LANES == 4) ? 2 : 1;
    reg [7:0] instruction_pc;
    wire [7:0] opcode = instruction[31:24];
    wire [1:0] rd = instruction[23:22];
    wire [1:0] ra = instruction[21:20];
    wire [1:0] rb = instruction[19:18];
    wire [15:0] immediate = instruction[15:0];
    // MAC sign-extends both operands; MUL and MACU zero-extend. The low 16 bits
    // of a 16x16 product do not depend on that choice, the upper 16 bits do.
    wire sign_extend = (opcode == MAC);
    wire launch = start && !busy;
    wire [7:0] address_base [0:LANES-1];
    wire [15:0] store_data [0:LANES-1];

    assign busy = (state != IDLE);
    assign program_address = pc;
    assign mem_valid = !reset && (state == MEMORY);
    assign mem_write = (opcode == STORE);
    // The low byte of a 16-bit sum equals this explicitly eight-bit sum.
    assign mem_address = address_base[memory_lane[LANE_BITS-1:0]] + immediate[7:0];
    assign mem_write_data = store_data[memory_lane[LANE_BITS-1:0]];

    // RDA returns bits [shift+15:shift] of the accumulator, filling positions
    // above bit 31 with the sign: a 16-bit window of the arithmetically shifted
    // value with no saturation, no rounding, and no flag for the dropped bits.
    // Each output bit is one mux over the accumulator bits, a barrel shifter.
    function automatic [15:0] accumulator_read;
        input [31:0] value;
        input [4:0] shift;
        integer position, source;
        begin
            for (position = 0; position < 16; position = position + 1) begin
                source = {27'd0, shift} + position;
                accumulator_read[position] = (source > 31) ? value[31] : value[source];
            end
        end
    endfunction

    genvar lane;
    generate
        for (lane = 0; lane < LANES; lane = lane + 1) begin: lanes
            reg [15:0] r [0:3];
            reg [31:0] acc;
            // Named aliases make individual registers visible in both VCDs.
            wire [15:0] r0 = r[0], r1 = r[1], r2 = r[2], r3 = r[3];
            // One 17x17 signed multiplier per lane serves MUL, MAC and MACU: the
            // extension bit is the operand's sign for MAC and zero otherwise.
            wire signed [16:0] multiplicand = {sign_extend & r[ra][15], r[ra]};
            wire signed [16:0] multiplier = {sign_extend & r[rb][15], r[rb]};
            // A 32-bit context keeps exactly the product bits the accumulator can hold.
            wire signed [31:0] product = multiplicand * multiplier;
            wire [15:0] read_back = accumulator_read(acc, immediate[4:0]);
            assign address_base[lane] = r[ra][7:0];
            assign store_data[lane] = r[rd];
            assign register_state[lane*64 +: 64] = {r3, r2, r1, r0};
            assign accumulator_state[lane*32 +: 32] = acc;
            always @(posedge clk) begin
                if (reset || launch) begin
                    r[0] <= 16'd0;
                    r[1] <= 16'd0;
                    r[2] <= 16'd0;
                    r[3] <= 16'd0;
                    acc <= 32'd0;
                end else if (state == EXECUTE) begin
                    case (opcode)
                        LDI:  r[rd] <= immediate;
                        LANE: r[rd] <= lane[15:0];
                        ADD:  r[rd] <= r[ra] + r[rb];
                        ADDI: r[rd] <= r[ra] + immediate;
                        MUL:  r[rd] <= product[15:0];
                        MAC, MACU: acc <= acc + product;
                        CLRA: acc <= 32'd0;
                        RDA:  r[rd] <= read_back;
                        default: begin end // Hold this lane's registers.
                    endcase
                end else if (mem_valid && mem_ready && opcode == LOAD && memory_lane == lane[1:0])
                    r[rd] <= mem_read_data;
            end
        end
    endgenerate

    always @(posedge clk) begin
        if (reset || launch) begin
            state <= reset ? IDLE : FETCH;
            pc <= reset ? 8'd0 : entry_pc;
            instruction <= 32'd0;
            instruction_pc <= 8'd0;
            memory_lane <= 2'd0;
            loop_count <= 16'd0;
            done <= 1'b0;
            fault <= 1'b0;
            retired <= 1'b0;
            retire_pc <= 8'd0;
            retire_instruction <= 32'd0;
            cycles <= 32'd0;
            stalls <= 32'd0;
            memory_transfers <= 32'd0;
            instructions <= 32'd0;
        end else begin
            retired <= 1'b0;
            if (busy)
                cycles <= cycles + 32'd1;
            case (state)
                IDLE: begin end
                FETCH: begin
                    instruction <= program_data;
                    instruction_pc <= pc;
                    pc <= pc + 8'd1;
                    state <= EXECUTE;
                end
                EXECUTE: begin
                    if (opcode == LOAD || opcode == STORE) begin
                        memory_lane <= 2'd0;
                        state <= MEMORY;
                    end else if (opcode > LAST_OPCODE ||
                                 (opcode == SETLOOP && immediate == 0) ||
                                 (opcode == LOOP && loop_count == 0)) begin
                        state <= IDLE;
                        done <= 1'b1;
                        fault <= 1'b1;
                    end else begin
                        retired <= 1'b1;
                        retire_pc <= instruction_pc;
                        retire_instruction <= instruction;
                        instructions <= instructions + 32'd1;
                        state <= FETCH;
                        case (opcode)
                            HLT: begin state <= IDLE; done <= 1'b1; end
                            SETLOOP: loop_count <= immediate;
                            LOOP: begin
                                loop_count <= loop_count - 16'd1;
                                if (loop_count > 1)
                                    pc <= immediate[7:0];
                            end
                            default: begin end // Lane datapaths perform arithmetic.
                        endcase
                    end
                end
                MEMORY: begin
                    if (!mem_ready)
                        stalls <= stalls + 32'd1;
                    else begin
                        memory_transfers <= memory_transfers + 32'd1;
                        if (memory_lane == LAST_LANE) begin
                            retired <= 1'b1;
                            retire_pc <= instruction_pc;
                            retire_instruction <= instruction;
                            instructions <= instructions + 32'd1;
                            state <= FETCH;
                        end else
                            memory_lane <= memory_lane + 2'd1;
                    end
                end
            endcase
        end
    end
endmodule
