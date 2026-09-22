`timescale 1ns/1ps
// G2 programmable 3D device (docs/rv32-3d.md): the CPU window, the device-local
// windows, the vertex stage on rv32_g3d_core, and the fixed-function pipeline
// (projection, cull, setup, raster, depth). One state per phase of the tick
// schedule; tools/rv32_g3d.c is the same machine in C and both must reproduce
// the oracle's CYCLES exactly, plus STALLS here.
module rv32_g3d #(
    parameter integer RAM_WORDS = 1048576
) (
    input  wire        clk,
    input  wire        reset,
    // CPU side (the bus decoder selects the 8 KiB window).
    input  wire        valid,
    input  wire        we,
    input  wire [31:0] addr,
    input  wire [31:0] wdata,
    input  wire [3:0]  strb,
    input  wire        other_busy,      // G1 owns the engine port
    output wire        ready,
    output wire        error,
    output reg  [31:0] rdata,
    output wire        busy,
    output wire        cancel,
    output wire [31:0] zbase_out,       // the depth buffer the CPU may not write while busy
    // Engine memory port: one outstanding word access, held until accepted.
    output wire        memory_valid,
    output wire        memory_we,
    output wire [31:0] memory_addr,
    output wire [3:0]  memory_strb,
    output wire [31:0] memory_wdata,
    input  wire        memory_ready,
    input  wire [31:0] memory_rdata
);
    localparam [63:0] RAM_BYTES = RAM_WORDS * 64'd4;
    localparam [31:0] Z_BYTES = 32'd153600;
    localparam [31:0] G3D_COMMAND = 32'h00, G3D_STATUS = 32'h04, G3D_CULLED = 32'h2c;
    localparam [31:0] G3D_VCOUNT = 32'h40, G3D_TCOUNT = 32'h44, G3D_ZBASE = 32'h48, G3D_LIMIT = 32'h4c;
    localparam [31:0] G3D_START = 32'h1, G3D_RESET = 32'h2, G3D_CLEAR_Z = 32'h3;
    localparam [31:0] G3D_IDLE = 32'h0, G3D_BUSY = 32'h1, G3D_DONE = 32'h2, G3D_FAULT = 32'h4;
    localparam [3:0]  E_PARAM = 1, E_INTERNAL = 2, E_INDEX = 8;
    localparam [3:0]  IDLE = 0, VALIDATE = 1, INDEX = 2, BATCH = 3, EXEC = 4, VCHECK = 5, DIVIDE = 6,
                      FETCH = 7, AREA = 8, TEST = 9, ZREAD = 10, ZWRITE = 11, PWRITE = 12, FINISH = 13,
                      CLEAR = 14;

    // ---------------------------------------------------------------- CPU window
    reg [31:0] status, error_code, fault_pc, cycles, stalls, instructions, transfers, divides, pixels, zfail, culled;
    reg [31:0] vcount, tcount, zbase, limit;
    reg [31:0] consts [0:31];
    reg [31:0] program_mem [0:127];
    reg [31:0] inputs [0:255];          // vertex v slot s at 8v+s
    reg [31:0] tris [0:63];
    reg [3:0]  state;

    wire [12:0] offset = addr[12:0];
    wire const_sel = offset[12:7] == 6'b001000;
    wire program_sel = offset[12:9] == 4'b0100;
    wire vertex_sel = offset[12:10] == 3'b100;
    wire tri_sel = offset[12:8] == 5'b11000;
    wire window_sel = const_sel || program_sel || vertex_sel || tri_sel;
    wire [31:0] off32 = {19'd0, offset};
    wire param_sel = off32 == G3D_VCOUNT || off32 == G3D_TCOUNT || off32 == G3D_ZBASE || off32 == G3D_LIMIT;
    wire counter_sel = off32 >= G3D_STATUS && off32 <= G3D_CULLED;
    wire reset_write = valid && we && strb == 4'b1111 && off32 == G3D_COMMAND && wdata == G3D_RESET;
    wire launch = wdata == G3D_START || wdata == G3D_CLEAR_Z;
    wire start_write = valid && we && strb == 4'b1111 && addr[1:0] == 2'b00 && off32 == G3D_COMMAND &&
                       launch && !busy && !other_busy;
    assign busy = status == G3D_BUSY;
    assign cancel = reset || reset_write;
    assign zbase_out = zbase;
    wire read_ok = (window_sel && !busy) || param_sel || counter_sel;
    wire write_ok = (window_sel && !busy) || (param_sel && !busy) ||
                    (off32 == G3D_COMMAND && (wdata == G3D_RESET || (launch && !busy && !other_busy)));
    assign ready = valid;
    assign error = strb != 4'b1111 || addr[1:0] != 2'b00 || (we ? !write_ok : !read_ok);
    wire window_write = valid && we && !error && window_sel;

    always @* begin
        if (const_sel) rdata = consts[offset[6:2]];
        else if (program_sel) rdata = program_mem[offset[8:2]];
        else if (vertex_sel) rdata = inputs[offset[9:2]];
        else if (tri_sel) rdata = tris[offset[7:2]];
        else case (off32)
            32'h04: rdata = status;       32'h08: rdata = error_code;  32'h0c: rdata = fault_pc;
            32'h10: rdata = cycles;       32'h14: rdata = stalls;      32'h18: rdata = instructions;
            32'h1c: rdata = transfers;    32'h20: rdata = divides;     32'h24: rdata = pixels;
            32'h28: rdata = zfail;        32'h2c: rdata = culled;
            32'h40: rdata = vcount;       32'h44: rdata = tcount;      32'h48: rdata = zbase;
            32'h4c: rdata = limit;
            default: rdata = 32'd0;
        endcase
    end

    // The windows are CPU-owned memory: RESET leaves them, and so does cancel.
    always @(posedge clk) begin
        if (window_write) begin
            if (const_sel) consts[offset[6:2]] <= wdata;
            if (program_sel) program_mem[offset[8:2]] <= wdata;
            if (vertex_sel) inputs[offset[9:2]] <= wdata;
            if (tri_sel) tris[offset[7:2]] <= wdata;
        end
    end

    // ---------------------------------------------------------------- engine state
    reg        clearing;
    reg [15:0] index;                   // triangle in INDEX/FETCH/scan; word in CLEAR
    reg [2:0]  batch;
    reg [1:0]  lane;
    // Projected vertices.
    reg signed [15:0] vsx [0:31];
    reg signed [15:0] vsy [0:31];
    reg [15:0] vz [0:31];
    reg [7:0]  vr [0:31];
    reg [7:0]  vg [0:31];
    reg [7:0]  vb [0:31];
    reg        vvalid [0:31];
    // The triangle being drawn, after the swap that makes its area positive.
    reg signed [15:0] tx0, ty0, tx1, ty1, tx2, ty2;
    reg [15:0] tz0, tz1, tz2;
    reg [7:0]  tr0, tr1, tr2, tg0, tg1, tg2, tb0, tb1, tb2;
    reg [31:0] area;
    reg [8:0]  left, right, top, bottom, x, y;
    // Divider (restoring, 32 ticks) and its sequence: projection k = 0..2, setup k = 0..7.
    reg        div_setup, div_neg, div_sat;
    reg [2:0]  div_k;
    reg [5:0]  div_left;
    reg [31:0] div_den, div_bits, div_q, div_rem;
    // Gradients (Q16 per subpixel), edge functions and attribute accumulators at the
    // current pixel and at the start of the current row.
    reg signed [31:0] gx [0:3];
    reg signed [31:0] gy [0:3];
    reg signed [31:0] edge_now [0:2];
    reg signed [31:0] edge_row [0:2];
    reg signed [47:0] acc_now [0:3];
    reg signed [47:0] acc_row [0:3];
    reg [15:0] pz;
    reg [7:0]  pr, pg, pb;

    // ---------------------------------------------------------------- shader core
    wire [4:0] vid_base = {batch, 2'b00};
    wire [7:0] core_pc;
    wire [31:0] word = program_mem[core_pc[6:0]];
    wire [127:0] input_values;
    genvar gl;
    generate for (gl = 0; gl < 4; gl = gl + 1) begin : lane_inputs
        wire [4:0] vid = vid_base + gl[4:0];
        assign input_values[32*gl +: 32] = inputs[{vid, word[2:0]}];
    end endgenerate
    // VALIDATE has bounded VCOUNT to 1..32, so six bits hold it exactly.
    wire [3:0] valid_lanes = {{1'b0, vid_base} + 6'd3 < vcount[5:0], {1'b0, vid_base} + 6'd2 < vcount[5:0],
                              {1'b0, vid_base} + 6'd1 < vcount[5:0], {1'b0, vid_base} < vcount[5:0]};
    wire core_counted, core_finished, core_faulted;
    wire [3:0] core_fault_code;
    wire [1023:0] core_outputs;
    rv32_g3d_core core (
        .clk(clk), .cancel(cancel), .begin_batch(!cancel && busy && state == BATCH), .valid_lanes(valid_lanes),
        .step(!cancel && busy && state == EXEC), .word(word), .const_value(consts[word[4:0]]),
        .input_values(input_values), .vcount(vcount), .vid_base(vid_base), .limit(limit[15:0]),
        .pc(core_pc), .counted(core_counted), .finished(core_finished), .faulted(core_faulted),
        .fault_code(core_fault_code), .outputs(core_outputs)
    );

    // ---------------------------------------------------------------- projection
    wire [4:0] vid_now = vid_base + {3'd0, lane};
    // A four-way lane mux, then fixed slices. A variable part-select of the
    // 1,024-bit bus (outputs[256*lane + k +: 32]) synthesizes as a 1,024-bit
    // barrel shifter per slot, which is what took synthesis tens of minutes.
    wire [255:0] lane_out = lane == 2'd0 ? core_outputs[255:0] : lane == 2'd1 ? core_outputs[511:256] :
                            lane == 2'd2 ? core_outputs[767:512] : core_outputs[1023:768];
    wire signed [31:0] ox = lane_out[31:0];
    wire signed [31:0] oy = lane_out[63:32];
    wire signed [31:0] oz = lane_out[95:64];
    wire signed [31:0] ow = lane_out[127:96];
    wire signed [31:0] ocr = lane_out[159:128];
    wire signed [31:0] ocg = lane_out[191:160];
    wire signed [31:0] ocb = lane_out[223:192];
    wire signed [34:0] w4 = {ow[31], ow[31], ow, 1'b0} + {ow[31], ow[31], ow, 1'b0};  // 4w
    wire signed [34:0] x35 = {{3{ox[31]}}, ox}, y35 = {{3{oy[31]}}, oy};
    wire vertex_ok = ow >= 32'sd4096 && x35 <= w4 && -x35 <= w4 && y35 <= w4 && -y35 <= w4 &&
                     oz >= 0 && oz <= ow;
    function [7:0] channel;              // integer part (bits 31..16) of a Q16.16 colour, clamped to 0..255
        input [15:0] v;
        begin channel = v[15] ? 8'd0 : v[14:8] != 7'd0 ? 8'd255 : v[7:0]; end
    endfunction

    // ---------------------------------------------------------------- divider
    // Numerator for the divide about to start. Projection: x*2560, y*1920, z*65535
    // over w. Setup: attribute gradients over the area; in AREA the triangle
    // registers still hold the unswapped order, so the swap is applied here too.
    wire load_setup = state == AREA || (state == DIVIDE && div_setup);
    wire [2:0] load_k = state == DIVIDE ? div_k + 3'd1 : 3'd0;
    wire swap_now = state == AREA;
    // Sign-extended screen coordinates of the stored triangle.
    wire signed [16:0] ex_x0 = {tx0[15], tx0}, ex_y0 = {ty0[15], ty0}, ex_x1 = {tx1[15], tx1}, ex_y1 = {ty1[15], ty1};
    wire signed [16:0] ex_x2 = {tx2[15], tx2}, ex_y2 = {ty2[15], ty2};
    wire signed [16:0] ex1_raw = ex_x1 - ex_x0, ey1_raw = ex_y1 - ex_y0, ex2_raw = ex_x2 - ex_x0, ey2_raw = ex_y2 - ex_y0;
    // 17 x 17-bit products: the guard band bounds every screen difference, and the
    // difference of the two products fits 32 bits (docs/rv32-3d.md "Projection").
    wire signed [33:0] area_p = ex1_raw * ey2_raw, area_q = ey1_raw * ex2_raw;
    wire signed [33:0] area_full = area_p - area_q;
    wire signed [31:0] area_raw = area_full[31:0];
    wire signed [16:0] sx0 = ex_x0, sy0 = ex_y0;
    wire signed [16:0] sx1 = swap_now ? ex_x2 : ex_x1, sy1 = swap_now ? ex_y2 : ex_y1;
    wire signed [16:0] sx2 = swap_now ? ex_x1 : ex_x2, sy2 = swap_now ? ex_y1 : ex_y2;
    reg signed [16:0] a0, a1, a2;
    always @* begin
        case (load_k[2:1])
            2'd0: begin a0 = {1'b0, tz0}; a1 = {1'b0, swap_now ? tz2 : tz1}; a2 = {1'b0, swap_now ? tz1 : tz2}; end
            2'd1: begin a0 = {9'd0, tr0}; a1 = {9'd0, swap_now ? tr2 : tr1}; a2 = {9'd0, swap_now ? tr1 : tr2}; end
            2'd2: begin a0 = {9'd0, tg0}; a1 = {9'd0, swap_now ? tg2 : tg1}; a2 = {9'd0, swap_now ? tg1 : tg2}; end
            default: begin a0 = {9'd0, tb0}; a1 = {9'd0, swap_now ? tb2 : tb1}; a2 = {9'd0, swap_now ? tb1 : tb2}; end
        endcase
    end
    wire signed [17:0] d1 = a1 - a0, d2 = a2 - a0;
    wire signed [17:0] ex1 = sx1 - sx0, ey1 = sy1 - sy0, ex2 = sx2 - sx0, ey2 = sy2 - sy0;
    wire signed [35:0] m_p = load_k[0] ? d2 * ex1 : d1 * ey2;
    wire signed [35:0] m_q = load_k[0] ? d1 * ex2 : d2 * ey1;
    wire signed [36:0] setup_diff = {m_p[35], m_p} - {m_q[35], m_q};
    wire signed [63:0] setup_num = {{11{setup_diff[36]}}, setup_diff, 16'd0};
    wire signed [63:0] ox64 = {{32{ox[31]}}, ox}, oy64 = {{32{oy[31]}}, oy}, oz64 = {{32{oz[31]}}, oz};
    wire signed [63:0] project_num = load_k == 3'd0 ? (ox64 <<< 11) + (ox64 <<< 9) :
                                     load_k == 3'd1 ? (oy64 <<< 11) - (oy64 <<< 7) : (oz64 <<< 16) - oz64;
    wire signed [63:0] load_num = load_setup ? setup_num : project_num;
    wire [31:0] load_den = load_setup ? (state == AREA ? 32'd0 - area_raw : area) : ow;
    wire [63:0] load_mag = load_num[63] ? 64'd0 - load_num : load_num;
    // One restoring step; the last one also forms the signed, saturated result.
    wire [32:0] div_trial = {div_rem, div_bits[31]};
    wire div_take = div_trial >= {1'b0, div_den};
    wire [31:0] div_next_q = {div_q[30:0], div_take};
    wire [31:0] div_result = div_sat ? (div_neg ? 32'h8000_0000 : 32'h7fff_ffff) :
                             (div_neg ? 32'd0 - div_next_q : div_next_q);

    // ---------------------------------------------------------------- setup and raster
    function signed [15:0] min3;
        input signed [15:0] p, q, r;
        begin min3 = p < q ? (p < r ? p : r) : (q < r ? q : r); end
    endfunction
    function signed [15:0] max3;
        input signed [15:0] p, q, r;
        begin max3 = p > q ? (p > r ? p : r) : (q > r ? q : r); end
    endfunction
    wire signed [15:0] box_l = min3(tx0, tx1, tx2) >>> 4, box_r = max3(tx0, tx1, tx2) >>> 4;
    wire signed [15:0] box_t = min3(ty0, ty1, ty2) >>> 4, box_b = max3(ty0, ty1, ty2) >>> 4;
    wire [8:0] new_left = box_l < 0 ? 9'd0 : box_l > 16'sd319 ? 9'd320 : box_l[8:0];
    wire [8:0] new_right = box_r > 16'sd319 ? 9'd319 : box_r < 0 ? 9'd0 : box_r[8:0];
    wire [8:0] new_top = box_t < 0 ? 9'd0 : box_t > 16'sd239 ? 9'd240 : box_t[8:0];
    wire [8:0] new_bottom = box_b > 16'sd239 ? 9'd239 : box_b < 0 ? 9'd0 : box_b[8:0];
    wire box_empty = box_l > 16'sd319 || box_r < 0 || box_t > 16'sd239 || box_b < 0 || box_l > box_r || box_t > box_b;
    reg box_empty_r;
    // First pixel centre in subpixels.
    wire signed [17:0] px0 = {5'd0, left, 4'd8}, py0 = {5'd0, top, 4'd8};
    // Edges v0->v1, v1->v2, v2->v0 of the stored (swapped) triangle.
    wire signed [16:0] edx0 = ex_x1 - ex_x0, edy0 = ex_y1 - ex_y0, edx1 = ex_x2 - ex_x1, edy1 = ex_y2 - ex_y1;
    wire signed [16:0] edx2 = ex_x0 - ex_x2, edy2 = ex_y0 - ex_y2;
    // Initial values are formed on divide completions with one pair of multipliers:
    // edge i when setup divide 2i completes, attribute a with its y gradient (2a+1).
    wire [2:0] done_k = div_k;
    wire [1:0] edge_i = done_k[2:1];
    reg signed [31:0] init_p1, init_p2;
    reg signed [17:0] init_q1, init_q2;
    always @* begin
        case (edge_i)
            2'd0: begin init_p1 = edge_dx(0); init_q1 = py0 - {ex_y0[16], ex_y0}; init_p2 = edge_dy(0); init_q2 = px0 - {ex_x0[16], ex_x0}; end
            2'd1: begin init_p1 = edge_dx(1); init_q1 = py0 - {ex_y1[16], ex_y1}; init_p2 = edge_dy(1); init_q2 = px0 - {ex_x1[16], ex_x1}; end
            default: begin init_p1 = edge_dx(2); init_q1 = py0 - {ex_y2[16], ex_y2}; init_p2 = edge_dy(2); init_q2 = px0 - {ex_x2[16], ex_x2}; end
        endcase
        if (done_k[0]) begin
            init_p1 = gx[done_k[2:1]]; init_q1 = px0 - {ex_x0[16], ex_x0};
            init_p2 = div_result; init_q2 = py0 - {ex_y0[16], ex_y0};
        end
    end
    wire signed [49:0] init_m1 = init_p1 * init_q1, init_m2 = init_p2 * init_q2;
    wire signed [49:0] init_value = done_k[0] ? init_m1 + init_m2 : init_m1 - init_m2;

    function covers;
        input signed [31:0] e;
        input signed [16:0] ddx, ddy;
        begin covers = e > 0 || (e == 0 && (ddy < 0 || (ddy == 0 && ddx > 0))); end
    endfunction
    wire covered = covers(edge_now[0], edx0, edy0) && covers(edge_now[1], edx1, edy1) && covers(edge_now[2], edx2, edy2);
    function [15:0] clamp16;
        input signed [47:0] value;
        begin clamp16 = value < 0 ? 16'd0 : value > 48'sd65535 ? 16'hffff : value[15:0]; end
    endfunction
    function [7:0] clamp8;
        input signed [47:0] value;
        begin clamp8 = value < 0 ? 8'd0 : value > 48'sd255 ? 8'hff : value[7:0]; end
    endfunction
    // Every operand is signed: one unsigned operand would make `>>>` a logical shift.
    wire signed [47:0] az = $signed({32'd0, tz0}) + (acc_now[0] >>> 16);
    wire signed [47:0] ar = $signed({40'd0, tr0}) + (acc_now[1] >>> 16);
    wire signed [47:0] ag = $signed({40'd0, tg0}) + (acc_now[2] >>> 16);
    wire signed [47:0] ab = $signed({40'd0, tb0}) + (acc_now[3] >>> 16);
    wire last_column = x == right, last_row = y == bottom;

    // Ordered 4x4 Bayer dither to RGB332.
    reg [3:0] threshold;
    always @* begin
        case ({y[1:0], x[1:0]})
            4'd0: threshold = 0;   4'd1: threshold = 8;   4'd2: threshold = 2;   4'd3: threshold = 10;
            4'd4: threshold = 12;  4'd5: threshold = 4;   4'd6: threshold = 14;  4'd7: threshold = 6;
            4'd8: threshold = 3;   4'd9: threshold = 11;  4'd10: threshold = 1;  4'd11: threshold = 9;
            4'd12: threshold = 15; 4'd13: threshold = 7;  4'd14: threshold = 13; default: threshold = 5;
        endcase
    end
    wire [8:0] dr = {1'b0, pr} + {4'd0, threshold, 1'b0}, dg = {1'b0, pg} + {4'd0, threshold, 1'b0};
    wire [8:0] db = {1'b0, pb} + {3'd0, threshold, 2'b0};
    wire [7:0] cr = dr[8] ? 8'hff : dr[7:0], cg = dg[8] ? 8'hff : dg[7:0], cb = db[8] ? 8'hff : db[7:0];
    wire [7:0] pixel = {cr[7:5], cg[7:5], cb[7:6]};

    // ---------------------------------------------------------------- memory port
    wire [31:0] linear = {15'd0, y, 8'd0} + {17'd0, y, 6'd0} + {23'd0, x};
    wire [31:0] z_addr = zbase + {linear[30:0], 1'b0};
    wire [31:0] fb_addr = 32'h3000_0000 + linear;
    wire [31:0] clear_addr = zbase + {14'd0, index, 2'b00};
    assign memory_valid = !cancel && (state == ZREAD || state == ZWRITE || state == PWRITE || state == CLEAR);
    assign memory_we = state != ZREAD;
    assign memory_addr = state == PWRITE ? fb_addr : state == CLEAR ? clear_addr : {z_addr[31:2], 2'b00};
    assign memory_strb = state == PWRITE ? 4'b0001 << fb_addr[1:0] : state == CLEAR ? 4'b1111 :
                         z_addr[1] ? 4'b1100 : 4'b0011;
    assign memory_wdata = state == PWRITE ? {4{pixel}} : state == CLEAR ? 32'hffff_ffff : {pz, pz};
    wire [15:0] z_old = z_addr[1] ? memory_rdata[31:16] : memory_rdata[15:0];

    // ---------------------------------------------------------------- sequencing
    wire zbase_ok = zbase[1:0] == 2'b00 && zbase >= 32'h8000_0000 &&
                    {32'd0, zbase - 32'h8000_0000} + {32'd0, Z_BYTES} <= RAM_BYTES;
    wire params_ok = zbase_ok && vcount >= 32'd1 && vcount <= 32'd32 && tcount <= 32'd64 &&
                     limit >= 32'd1 && limit <= 32'hffff;
    wire [31:0] tri_word = tris[index[5:0]];
    wire [4:0] i0 = tri_word[4:0], i1 = tri_word[12:8], i2 = tri_word[20:16];
    wire index_bad = {24'd0, tri_word[7:0]} >= vcount || {24'd0, tri_word[15:8]} >= vcount ||
                     {24'd0, tri_word[23:16]} >= vcount;
    wire more_lanes = lane != 2'd3 && {27'd0, vid_now} + 32'd1 < vcount;
    wire more_batches = {27'd0, vid_base} + 32'd4 < vcount;
    wire more_triangles = {16'd0, index} + 32'd1 < tcount;

    integer i;
    always @(posedge clk) begin
        if (cancel) begin
            state <= IDLE; status <= G3D_IDLE; error_code <= 0; fault_pc <= 0; cycles <= 0; stalls <= 0;
            instructions <= 0; transfers <= 0; divides <= 0; pixels <= 0; zfail <= 0; culled <= 0;
            vcount <= 0; tcount <= 0; zbase <= 0; limit <= 0; clearing <= 0; index <= 0; batch <= 0; lane <= 0;
            div_left <= 0; div_k <= 0; div_setup <= 0;
        end else begin
            if (valid && we && !error && param_sel) begin
                if (off32 == G3D_VCOUNT) vcount <= wdata;
                if (off32 == G3D_TCOUNT) tcount <= wdata;
                if (off32 == G3D_ZBASE) zbase <= wdata;
                if (off32 == G3D_LIMIT) limit <= wdata;
            end
            if (start_write) begin
                state <= VALIDATE; status <= G3D_BUSY; clearing <= wdata == G3D_CLEAR_Z;
                error_code <= 0; fault_pc <= 0; cycles <= 0; stalls <= 0; instructions <= 0; transfers <= 0;
                divides <= 0; pixels <= 0; zfail <= 0; culled <= 0;
            end else if (busy) begin
                cycles <= cycles + 1;
                case (state)
                    VALIDATE:
                        if (clearing) begin
                            if (!zbase_ok) begin status <= G3D_FAULT; error_code <= {28'd0, E_PARAM}; state <= IDLE; end
                            else begin index <= 0; state <= CLEAR; end
                        end else if (!params_ok) begin
                            status <= G3D_FAULT; error_code <= {28'd0, E_PARAM}; state <= IDLE;
                        end else begin
                            index <= 0; batch <= 0;
                            state <= tcount != 0 ? INDEX : BATCH;
                        end
                    INDEX:
                        if (index_bad) begin status <= G3D_FAULT; error_code <= {28'd0, E_INDEX}; state <= IDLE; end
                        else begin
                            index <= index + 1;
                            if (!more_triangles) state <= BATCH;
                        end
                    BATCH: state <= EXEC;
                    EXEC: begin
                        if (core_counted) instructions <= instructions + 1;
                        if (core_faulted) begin
                            status <= G3D_FAULT; error_code <= {28'd0, core_fault_code};
                            fault_pc <= {16'd0, 5'd0, batch, core_pc}; state <= IDLE;
                        end else if (core_finished) begin lane <= 0; state <= VCHECK; end
                    end
                    VCHECK: begin
                        vr[vid_now] <= channel(ocr[31:16]); vg[vid_now] <= channel(ocg[31:16]); vb[vid_now] <= channel(ocb[31:16]);
                        vvalid[vid_now] <= vertex_ok;
                        if (vertex_ok) begin
                            div_setup <= 0; div_k <= 0; div_left <= 6'd32; div_den <= load_den;
                            div_neg <= load_num[63]; div_sat <= {31'd0, load_mag[63:31]} >= {32'd0, load_den};
                            div_rem <= load_mag[63:32]; div_bits <= load_mag[31:0]; div_q <= 0;
                            state <= DIVIDE;
                        end else if (more_lanes) lane <= lane + 1;
                        else if (more_batches) begin batch <= batch + 1; state <= BATCH; end
                        else begin index <= 0; state <= tcount != 0 ? FETCH : FINISH; end
                    end
                    DIVIDE: begin
                        div_rem <= div_take ? div_trial[31:0] - div_den : div_trial[31:0];
                        div_bits <= {div_bits[30:0], 1'b0};
                        div_q <= div_next_q;
                        div_left <= div_left - 1;
                        if (div_left == 6'd1) begin
                            divides <= divides + 1;
                            if (!div_setup) begin
                                case (div_k)
                                    3'd0: vsx[vid_now] <= 16'sd2560 + div_result[15:0];
                                    3'd1: vsy[vid_now] <= 16'sd1920 - div_result[15:0];
                                    default: vz[vid_now] <= div_result[15:0];
                                endcase
                            end else begin
                                if (!done_k[0]) begin
                                    gx[done_k[2:1]] <= div_result;
                                    if (done_k != 3'd6) begin
                                        edge_row[edge_i] <= init_value[31:0]; edge_now[edge_i] <= init_value[31:0];
                                    end
                                end else begin
                                    gy[done_k[2:1]] <= div_result;
                                    acc_row[done_k[2:1]] <= init_value[47:0]; acc_now[done_k[2:1]] <= init_value[47:0];
                                end
                            end
                            if ((!div_setup && div_k != 3'd2) || (div_setup && div_k != 3'd7)) begin
                                div_k <= div_k + 1; div_left <= 6'd32; div_den <= load_den;
                                div_neg <= load_num[63]; div_sat <= {31'd0, load_mag[63:31]} >= {32'd0, load_den};
                                div_rem <= load_mag[63:32]; div_bits <= load_mag[31:0]; div_q <= 0;
                            end else if (!div_setup) begin
                                if (more_lanes) begin lane <= lane + 1; state <= VCHECK; end
                                else if (more_batches) begin batch <= batch + 1; state <= BATCH; end
                                else begin index <= 0; state <= tcount != 0 ? FETCH : FINISH; end
                            end else if (box_empty_r) begin
                                if (more_triangles) begin index <= index + 1; state <= FETCH; end
                                else state <= FINISH;
                            end else begin x <= left; y <= top; state <= TEST; end
                        end
                    end
                    FETCH: begin
                        tx0 <= vsx[i0]; ty0 <= vsy[i0]; tz0 <= vz[i0]; tr0 <= vr[i0]; tg0 <= vg[i0]; tb0 <= vb[i0];
                        tx1 <= vsx[i1]; ty1 <= vsy[i1]; tz1 <= vz[i1]; tr1 <= vr[i1]; tg1 <= vg[i1]; tb1 <= vb[i1];
                        tx2 <= vsx[i2]; ty2 <= vsy[i2]; tz2 <= vz[i2]; tr2 <= vr[i2]; tg2 <= vg[i2]; tb2 <= vb[i2];
                        if (!vvalid[i0] || !vvalid[i1] || !vvalid[i2]) begin
                            culled <= culled + 1;
                            if (more_triangles) index <= index + 1; else state <= FINISH;
                        end else state <= AREA;
                    end
                    AREA:
                        if (!area_raw[31]) begin
                            culled <= culled + 1;
                            if (more_triangles) begin index <= index + 1; state <= FETCH; end
                            else state <= FINISH;
                        end else begin
                            tx1 <= tx2; ty1 <= ty2; tz1 <= tz2; tr1 <= tr2; tg1 <= tg2; tb1 <= tb2;
                            tx2 <= tx1; ty2 <= ty1; tz2 <= tz1; tr2 <= tr1; tg2 <= tg1; tb2 <= tb1;
                            area <= 32'd0 - area_raw;
                            left <= new_left; right <= new_right; top <= new_top; bottom <= new_bottom;
                            box_empty_r <= box_empty;
                            div_setup <= 1; div_k <= 0; div_left <= 6'd32; div_den <= load_den;
                            div_neg <= load_num[63]; div_sat <= {31'd0, load_mag[63:31]} >= {32'd0, load_den};
                            div_rem <= load_mag[63:32]; div_bits <= load_mag[31:0]; div_q <= 0;
                            state <= DIVIDE;
                        end
                    TEST, ZREAD, ZWRITE, PWRITE: begin
                        // `advance` below moves to the next pixel, row or triangle.
                        if (state == TEST) begin
                            if (covered) begin
                                pz <= clamp16(az); pr <= clamp8(ar); pg <= clamp8(ag); pb <= clamp8(ab);
                                state <= ZREAD;
                            end
                        end else if (!memory_ready) stalls <= stalls + 1;
                        else begin
                            transfers <= transfers + 1;
                            if (state == ZREAD) begin
                                if (pz < z_old) state <= ZWRITE;
                                else zfail <= zfail + 1;
                            end else if (state == ZWRITE) state <= PWRITE;
                            else pixels <= pixels + 1;
                        end
                        if ((state == TEST && !covered) ||
                            (memory_ready && (state == PWRITE || (state == ZREAD && !(pz < z_old))))) begin
                            if (!last_column) begin
                                x <= x + 1; state <= TEST;
                                for (i = 0; i < 3; i = i + 1) edge_now[i] <= edge_now[i] - (edge_dy(i) <<< 4);
                                for (i = 0; i < 4; i = i + 1) acc_now[i] <= acc_now[i] + ({{16{gx[i][31]}}, gx[i]} <<< 4);
                            end else if (!last_row) begin
                                x <= left; y <= y + 1; state <= TEST;
                                for (i = 0; i < 3; i = i + 1) begin
                                    edge_row[i] <= edge_row[i] + (edge_dx(i) <<< 4);
                                    edge_now[i] <= edge_row[i] + (edge_dx(i) <<< 4);
                                end
                                for (i = 0; i < 4; i = i + 1) begin
                                    acc_row[i] <= acc_row[i] + ({{16{gy[i][31]}}, gy[i]} <<< 4);
                                    acc_now[i] <= acc_row[i] + ({{16{gy[i][31]}}, gy[i]} <<< 4);
                                end
                            end else if (more_triangles) begin index <= index + 1; state <= FETCH; end
                            else state <= FINISH;
                        end
                    end
                    CLEAR:
                        if (!memory_ready) stalls <= stalls + 1;
                        else begin
                            transfers <= transfers + 1; index <= index + 1;
                            if (index == 16'd38399) state <= FINISH;
                        end
                    FINISH: begin status <= G3D_DONE; state <= IDLE; end
                    default: begin status <= G3D_FAULT; error_code <= {28'd0, E_INTERNAL}; state <= IDLE; end
                endcase
            end
        end
    end
    wire unused_ok = &{1'b0, addr[31:13], div_q[31], area_full[33:32], lane_out[255:224], linear[31], box_l[15:9], box_r[15:9], box_t[15:9],
                       box_b[15:9], ocr[15:0], ocg[15:0], ocb[15:0], init_value[49:48], cr[4:0], cg[4:0],
                       cb[5:0], z_addr[0], tri_word[31:24]};
    function signed [31:0] edge_dx;
        input integer e;
        begin edge_dx = e == 0 ? {{15{edx0[16]}}, edx0} : e == 1 ? {{15{edx1[16]}}, edx1} : {{15{edx2[16]}}, edx2}; end
    endfunction
    function signed [31:0] edge_dy;
        input integer e;
        begin edge_dy = e == 0 ? {{15{edy0[16]}}, edy0} : e == 1 ? {{15{edy1[16]}}, edy1} : {{15{edy2[16]}}, edy2}; end
    endfunction
endmodule
