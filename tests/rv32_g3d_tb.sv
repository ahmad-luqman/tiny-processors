`timescale 1ns/1ps
// G2 device on its own: every corpus job (tools/rv32_g3d_corpus.py) goes in
// through the CPU window and must reproduce the oracle's status, error,
// FAULT_PC, counters, CYCLES - STALLS and both image hashes. Odd hold seeds
// withhold memory acceptance on a fixed pattern; +cancel=1 also resets some
// jobs mid-transfer and checks that RESET leaves an idle device.
module rv32_g3d_tb;
    reg clk = 0;
    always #5 clk = ~clk;
    reg reset = 1, valid = 0, we = 0, other_busy = 0;
    reg [31:0] addr = 0, wdata = 0;
    reg [3:0] strb = 4'hf;
    wire ready, error, busy, cancel, memory_valid, memory_we;
    wire [31:0] rdata, zbase_out, memory_addr, memory_wdata;
    wire [3:0] memory_strb;
    reg memory_ready = 1;
    localparam [31:0] ZBASE = 32'h8004_0000;
    reg [7:0] zmem [0:153599];
    reg [7:0] fb [0:76799];
    wire [31:0] z_off = memory_addr - ZBASE, fb_off = memory_addr - 32'h3000_0000;
    wire in_z = memory_addr >= ZBASE && z_off < 32'd153600;
    wire in_fb = memory_addr >= 32'h3000_0000 && fb_off < 32'd76800;
    wire [31:0] memory_rdata = in_z ? {zmem[z_off+3], zmem[z_off+2], zmem[z_off+1], zmem[z_off]} : 32'd0;
    rv32_g3d #(.RAM_WORDS(1048576)) dut (.*);

    reg saw_held_read = 0, saw_held_write = 0, cancelled = 0;
    reg held = 0, held_we;
    reg [31:0] held_addr, held_wdata;
    reg [3:0] held_strb;
    integer k;
    always @(posedge clk) begin
        if (memory_valid && !memory_ready && !cancel) begin
            if (memory_we) saw_held_write <= 1; else saw_held_read <= 1;
        end
        // RESET gates memory_valid on its own edge, so the dropped request is seen in the state.
        if ((dut.state == 10 || dut.state == 11 || dut.state == 12 || dut.state == 14) && !memory_ready && cancel) cancelled <= 1;
        if (memory_valid && memory_ready) begin
            // Z accesses are word aligned; pixel writes carry their byte address and one strobe, as G1's do.
            if (!(in_z || in_fb) || (in_z && memory_addr[1:0] != 2'b00)) $fatal(1, "access outside the Z buffer and framebuffer");
            if (in_fb && memory_strb != 4'b0001 << memory_addr[1:0]) $fatal(1, "pixel strobe does not match its byte");
            if (in_fb && !memory_we) $fatal(1, "framebuffer read");
            if (memory_we)
                for (k = 0; k < 4; k = k + 1)
                    if (memory_strb[k]) begin
                        if (in_z) zmem[z_off + k] <= memory_wdata[8*k +: 8];
                        else fb[{fb_off[31:2], 2'b00} + k] <= memory_wdata[8*k +: 8];
                    end
        end
        if (held && !cancel && (!memory_valid || memory_addr !== held_addr || memory_we !== held_we ||
                                memory_strb !== held_strb || memory_wdata !== held_wdata))
            $fatal(1, "request changed while held");
        held <= memory_valid && !memory_ready;
        held_addr <= memory_addr; held_we <= memory_we; held_strb <= memory_strb; held_wdata <= memory_wdata;
    end

`ifdef G3D_PROBE
    integer shown = 0;
    always @(posedge clk)
        if (dut.state == 10 && memory_ready && !(dut.pz < dut.z_old) && shown < 6) begin
            $display("x=%0d y=%0d pz=%0d old=%0d tz0=%0d acc0=%0d gx0=%0d gy0=%0d area=%0d t0=(%0d,%0d)", dut.x, dut.y,
                     dut.pz, dut.z_old, dut.tz0, $signed(dut.acc_now[0]), $signed(dut.gx[0]), $signed(dut.gy[0]),
                     dut.area, dut.tx0, dut.ty0);
            shown = shown + 1;
        end
`endif
    task access_write(input [31:0] a, input [31:0] v);
        begin
            @(negedge clk); valid = 1; we = 1; addr = a; wdata = v; #1;
            if (!ready || error) $fatal(1, "refused write %08x", a);
            @(negedge clk); valid = 0; we = 0;
        end
    endtask
    task access_read(input [31:0] a, output [31:0] v);
        begin
            @(negedge clk); valid = 1; we = 0; addr = a; #1;
            if (!ready || error) $fatal(1, "refused read %08x", a);
            v = rdata;
            @(negedge clk); valid = 0;
        end
    endtask

    function [31:0] next_hash(input [31:0] h, input [31:0] w);
        begin next_hash = ((h << 5) + h) ^ w; end
    endfunction

    integer file, ret, n, i, job = 0, ticks, cancel_at, use_cancel = 0;
    reg [31:0] head [0:4];
    reg [31:0] body [0:511];
    reg [31:0] want [0:11];
    reg [31:0] got [0:11];
    reg [31:0] h, value;
    string input_path, wave_path;
    initial begin
        if (!$value$plusargs("input=%s", input_path)) $fatal(1, "missing +input");
        if ($value$plusargs("wave=%s", wave_path)) begin $dumpfile(wave_path); $dumpvars(0, rv32_g3d_tb); end
        if (!$value$plusargs("cancel=%d", use_cancel)) use_cancel = 0;
        file = $fopen(input_path, "r");
        if (file == 0) $fatal(1, "cannot open input");
        repeat (2) @(negedge clk);
        reset = 0;
        ret = $fscanf(file, "%h", head[0]);
        while (ret == 1) begin
            for (n = 1; n < 5; n = n + 1) if ($fscanf(file, "%h", head[n]) != 1) $fatal(1, "short header");
            // program 128, consts 32, inputs vcount*8, triangles tcount
            for (n = 0; n < 160 + 8 * head[0] + head[1]; n = n + 1)
                if ($fscanf(file, "%h", body[n]) != 1) $fatal(1, "short job");
            for (n = 0; n < 12; n = n + 1) if ($fscanf(file, "%h", want[n]) != 1) $fatal(1, "short expectation");
            for (i = 0; i < 153600; i = i + 1) zmem[i] = 8'hff;
            for (i = 0; i < 76800; i = i + 1) fb[i] = 8'h00;
            for (n = 0; n < 128; n = n + 1) access_write(32'h800 + 4 * n, body[n]);
            for (n = 0; n < 32; n = n + 1) access_write(32'h400 + 4 * n, body[128 + n]);
            for (n = 0; n < 8 * head[0]; n = n + 1) access_write(32'h1000 + 4 * n, body[160 + n]);
            for (n = 0; n < head[1]; n = n + 1) access_write(32'h1800 + 4 * n, body[160 + 8 * head[0] + n]);
            access_write(32'h40, head[0]); access_write(32'h44, head[1]);
            access_write(32'h48, head[3]); access_write(32'h4c, head[2]);
            // A launch while G1 is busy is refused without effect.
            @(negedge clk); other_busy = 1; valid = 1; we = 1; addr = 0; wdata = 1; #1;
            if (!error) $fatal(1, "START accepted while the other engine is busy");
            @(negedge clk); valid = 0; we = 0; other_busy = 0;
            if (busy) $fatal(1, "refused START launched");
            access_write(32'h0, 32'h1);
            ticks = 0;
            // Selected jobs with holds are reset while a transfer is held, after 2000 ticks.
            cancel_at = use_cancel != 0 && job % 5 == 4 && head[4][0] && want[4] > 100 ? 2000 : -1;
            while (busy) begin
                memory_ready = !(head[4][0] && (ticks % 7 == 2 || ticks % 7 == 4));
                #1;
                if (cancel_at >= 0 && ticks >= cancel_at && memory_valid && !memory_ready) begin
                    valid = 1; we = 1; addr = 0; wdata = 2;
                end
                @(negedge clk);
                valid = 0; we = 0;
                ticks = ticks + 1;
                if (ticks > 40000000) $fatal(1, "job %0d timeout", job);
            end
            memory_ready = 1;
            if (cancel_at >= 0 && ticks > cancel_at) begin
                access_read(32'h04, value);
                if (value !== 0) $fatal(1, "RESET left status %08x", value);
                access_read(32'h40, value);
                if (value !== 0) $fatal(1, "RESET kept VCOUNT");
                access_read(32'h800, value);
                if (value !== body[0]) $fatal(1, "RESET cleared the program window");
            end else begin
                for (n = 0; n < 3; n = n + 1) access_read(32'h04 + 4 * n, got[n]);
                access_read(32'h18, got[3]); access_read(32'h1c, got[4]); access_read(32'h20, got[5]);
                access_read(32'h24, got[6]); access_read(32'h28, got[7]); access_read(32'h2c, got[8]);
                access_read(32'h10, got[9]); access_read(32'h14, value);
                got[9] = got[9] - value;
                if (head[4][0] && got[4] > 2 && value == 0) $fatal(1, "job %0d: holds never stalled", job);
                h = 5381;
                for (i = 0; i < 76800; i = i + 4) h = next_hash(h, {fb[i + 3], fb[i + 2], fb[i + 1], fb[i]});
                got[10] = h;
                h = 5381;
                for (i = 0; i < 153600; i = i + 4) h = next_hash(h, {zmem[i + 3], zmem[i + 2], zmem[i + 1], zmem[i]});
                got[11] = h;
                value = 0;
                for (n = 0; n < 12; n = n + 1)
                    if (got[n] !== want[n]) begin
                        $display("job %0d field %0d got %08x want %08x", job, n, got[n], want[n]);
                        value = 1;
                    end
                if (value != 0) $fatal(1, "job %0d disagrees with the oracle", job);
            end
            job = job + 1;
            ret = $fscanf(file, "%h", head[0]);
        end
        if (job == 0) $fatal(1, "empty corpus");
        // +waves-only dumps a job without holds, so the coverage requirement does not apply.
        if (!$test$plusargs("waves-only") && !(saw_held_read && saw_held_write)) $fatal(1, "no held read and write observed");
        if (use_cancel != 0 && !cancelled) $fatal(1, "no transfer cancelled by RESET");
        $display("PASS %0d", job);
        $finish;
    end
endmodule
