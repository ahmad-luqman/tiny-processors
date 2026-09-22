`timescale 1ns/1ps
// G2 shader core: four SIMT lanes sharing one PC, one instruction per tick,
// with the typed IF/LOOP mask stack of docs/rv32-3d.md. It knows nothing
// about vertices: the parent supplies the instruction word, the constant it
// names and each lane's input slot, and reads the output slots back. A
// fragment stage could drive the same core with different slots.
module rv32_g3d_core (
    input  wire          clk,
    input  wire          cancel,
    input  wire          begin_batch,     // zero registers and outputs, PC 0, mask = valid_lanes
    input  wire [3:0]    valid_lanes,
    input  wire          step,            // execute `word` this tick
    input  wire [31:0]   word,            // program[pc[6:0]]
    input  wire [31:0]   const_value,     // constant word[4:0]
    input  wire [127:0]  input_values,    // lane l in [32l+31:32l]: its input slot word[2:0]
    input  wire [31:0]   vcount,
    input  wire [4:0]    vid_base,        // vertex id of lane 0
    input  wire [15:0]   limit,
    output reg  [7:0]    pc,
    // This tick's outcome, combinational so the parent acts on the same edge.
    output wire          counted,         // the instruction began execution (INSTRUCTIONS)
    output wire          finished,        // END with an empty stack
    output wire          faulted,
    output wire [3:0]    fault_code,
    output wire [1023:0] outputs          // lane l slot s in [256l+32s+31:256l+32s]
);
    localparam [5:0] OP_END=0, OP_LDI=1, OP_LDC=2, OP_IN=3, OP_OUT=4, OP_SPC=5, OP_MOV=6, OP_ADD=7,
                     OP_SUB=8, OP_MUL=9, OP_MAD=10, OP_MIN=11, OP_MAX=12, OP_ABS=13, OP_AND=14, OP_OR=15,
                     OP_XOR=16, OP_SHL=17, OP_SRA=18, OP_ADDI=19, OP_SLT=20, OP_SEQ=21, OP_IF=22,
                     OP_ELSE=23, OP_ENDIF=24, OP_LOOP=25, OP_ENDLOOP=26, OP_BREAK=27, OP_COUNT=28;
    localparam [3:0] E_ILLEGAL=3, E_OVERFLOW=4, E_MISMATCH=5, E_LIMIT=6, E_PC=7;
    localparam [3:0] DEPTH=8;

    reg [31:0] regs [0:63];          // lane l register r at 16l+r
    reg [31:0] outs [0:31];          // lane l slot s at 8l+s
    reg [3:0]  mask, pred, depth;
    reg        stack_loop [0:7];
    reg [3:0]  stack_parent [0:7];
    reg [3:0]  stack_taken [0:7];
    reg [15:0] count;

    wire [5:0] op = word[31:26];
    wire [3:0] rd = word[25:22], ra = word[21:18], rb = word[17:14], rc = word[13:10];
    wire [6:0] target = word[6:0];
    wire [3:0] top = depth - 4'd1;
    wire       top_is_loop = stack_loop[top[2:0]];

    genvar g;
    generate for (g = 0; g < 32; g = g + 1) begin : flatten
        assign outputs[32*g+31:32*g] = outs[g];
    end endgenerate

    // Innermost loop entry for BREAK: the highest index below depth with its tag set.
    reg       loop_found;
    reg [2:0] loop_index;
    integer   j;
    always @* begin
        loop_found = 1'b0;
        loop_index = 3'd0;
        for (j = 0; j < 8; j = j + 1)
            if (j < {28'd0, depth} && stack_loop[j]) begin loop_found = 1'b1; loop_index = j[2:0]; end
    end

    // Per-lane operands and ALU result. Every lane computes; the mask decides
    // which results the sequential block keeps.
    wire [31:0] lane_a [0:3];
    wire [31:0] lane_b [0:3];
    wire [31:0] lane_result [0:3];
    wire [63:0] lane_unused [0:3];
    genvar n;
    generate for (n = 0; n < 4; n = n + 1) begin : lane
        wire [31:0] a = regs[{n[1:0], ra}], b = regs[{n[1:0], rb}], c = regs[{n[1:0], rc}];
        wire [63:0] product = $signed(a) * $signed(b);
        wire [31:0] scaled = product[47:16];                 // Q16.16: floor of the product / 2^16
        wire [31:0] special = word[1:0] == 2'd0 ? {30'd0, n[1:0]} :
                              word[1:0] == 2'd1 ? {27'd0, vid_base + n[4:0]} :
                              word[1:0] == 2'd2 ? vcount : 32'd0;
        reg [31:0] result;
        always @* begin
            case (op)
                OP_LDI:  result = {{10{word[21]}}, word[21:0]};
                OP_LDC:  result = const_value;
                OP_IN:   result = input_values[32*n +: 32];
                OP_SPC:  result = special;
                OP_MOV:  result = a;
                OP_ADD:  result = a + b;
                OP_SUB:  result = a - b;
                OP_MUL:  result = scaled;
                OP_MAD:  result = c + scaled;
                OP_MIN:  result = $signed(a) < $signed(b) ? a : b;
                OP_MAX:  result = $signed(a) > $signed(b) ? a : b;
                OP_ABS:  result = a[31] ? 32'd0 - a : a;
                OP_AND:  result = a & b;
                OP_OR:   result = a | b;
                OP_XOR:  result = a ^ b;
                OP_SHL:  result = a << word[4:0];
                OP_SRA:  result = $signed(a) >>> word[4:0];
                OP_ADDI: result = a + {{18{word[13]}}, word[13:0]};
                default: result = 32'd0;
            endcase
        end
        assign lane_a[n] = a;
        assign lane_b[n] = b;
        assign lane_result[n] = result;
        assign lane_unused[n] = product;
    end endgenerate

    // Combinational decision for this tick; the sequential block applies it.
    reg       halt_limit, halt_pc, halt_illegal, halt_overflow, halt_mismatch;
    reg [7:0] next_pc;
    always @* begin
        halt_limit = count == limit;
        halt_pc = !halt_limit && pc[7];
        halt_illegal = !halt_limit && !halt_pc && op >= OP_COUNT;
        halt_overflow = 1'b0;
        halt_mismatch = 1'b0;
        next_pc = pc + 8'd1;
        if (!halt_limit && !halt_pc && !halt_illegal) begin
            case (op)
                OP_END: halt_mismatch = depth != 4'd0;
                OP_IF: begin
                    halt_overflow = depth == DEPTH;
                    if ((mask & pred) == 4'd0) next_pc = {1'b0, target};
                end
                OP_LOOP: halt_overflow = depth == DEPTH;
                OP_ELSE: begin
                    halt_mismatch = depth == 4'd0 || top_is_loop;
                    if ((stack_parent[top[2:0]] & ~stack_taken[top[2:0]]) == 4'd0) next_pc = {1'b0, target};
                end
                OP_ENDIF: halt_mismatch = depth == 4'd0 || top_is_loop;
                OP_ENDLOOP: begin
                    halt_mismatch = depth == 4'd0 || !top_is_loop;
                    if (mask != 4'd0) next_pc = {1'b0, target};
                end
                OP_BREAK: halt_mismatch = !loop_found;
                default: ;
            endcase
        end
    end
    wire halted = halt_limit || halt_pc || halt_illegal || halt_overflow || halt_mismatch;
    assign counted = step && !(halt_limit || halt_pc || halt_illegal);
    assign finished = step && !halted && op == OP_END;
    assign faulted = step && halted;
    assign fault_code = halt_limit ? E_LIMIT : halt_pc ? E_PC : halt_illegal ? E_ILLEGAL :
                        halt_overflow ? E_OVERFLOW : E_MISMATCH;

    integer l, k;
    always @(posedge clk) begin
        if (cancel) begin
            pc <= 8'd0; count <= 16'd0; mask <= 4'd0; pred <= 4'd0; depth <= 4'd0;
        end else if (begin_batch) begin
            pc <= 8'd0; count <= 16'd0; pred <= 4'd0; depth <= 4'd0; mask <= valid_lanes;
            for (k = 0; k < 64; k = k + 1) regs[k] <= 32'd0;
            for (k = 0; k < 32; k = k + 1) outs[k] <= 32'd0;
        end else if (step && !halted) begin
                count <= count + 16'd1;
                pc <= next_pc;
                case (op)
                    OP_END: ;
                    OP_IF, OP_LOOP: begin
                        stack_loop[depth[2:0]] <= op == OP_LOOP;
                        stack_parent[depth[2:0]] <= mask;
                        stack_taken[depth[2:0]] <= op == OP_IF ? mask & pred : 4'd0;
                        depth <= depth + 4'd1;
                        if (op == OP_IF) mask <= mask & pred;
                    end
                    OP_ELSE: mask <= stack_parent[top[2:0]] & ~stack_taken[top[2:0]];
                    OP_ENDIF: begin mask <= stack_parent[top[2:0]]; depth <= top; end
                    OP_ENDLOOP: if (mask == 4'd0) begin mask <= stack_parent[top[2:0]]; depth <= top; end
                    OP_BREAK: begin
                        for (k = 0; k < 8; k = k + 1)
                            if (k > {29'd0, loop_index} && k < {28'd0, depth}) stack_parent[k] <= stack_parent[k] & ~mask;
                        mask <= 4'd0;
                    end
                    default:
                        for (l = 0; l < 4; l = l + 1)
                            if (mask[l]) begin
                                if (op == OP_SLT) pred[l] <= $signed(lane_a[l]) < $signed(lane_b[l]);
                                else if (op == OP_SEQ) pred[l] <= lane_a[l] == lane_b[l];
                                else if (op == OP_OUT) outs[{l[1:0], word[2:0]}] <= lane_a[l];
                                else regs[{l[1:0], rd}] <= lane_result[l];
                            end
                endcase
        end
    end
    wire unused_ok = &{1'b0, lane_unused[0][63:48], lane_unused[0][15:0], lane_unused[1][63:48],
                       lane_unused[1][15:0], lane_unused[2][63:48], lane_unused[2][15:0],
                       lane_unused[3][63:48], lane_unused[3][15:0], word[9:7]};
endmodule
