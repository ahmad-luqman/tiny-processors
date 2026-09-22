`timescale 1ns/1ps

// The RV32 machine as one synthesizable unit: the core, the bus decoder, and
// every memory and device of docs/rv32.md. The core's memory port and its
// retirement port are passed through unchanged so the testbench can check
// the handshake and print the trace exactly as it did when it was the
// memory itself; `mem_hold` lets it defer every acceptance to model a slow
// memory. Device side effects that need a host (console bytes, the done
// word, a present) leave as strobes and the host's key events come in
// through the input push port; the host decides what they mean.
module rv32_soc #(
    parameter integer RAM_WORDS = 1048576,
    parameter integer FB_WORDS = 19200, // 320 x 240 one-byte pixels, as 32-bit words
    parameter integer CONSOLE_BUSY = 0
) (
    input  wire        clk,
    input  wire        reset,
    input  wire        mem_hold,
    input  wire        simd_memory_hold,
    input  wire        gpu_memory_hold,
    // Core memory port, observed.
    output wire        mem_valid,
    output wire [31:0] mem_addr,
    output wire        mem_we,
    output wire [3:0]  mem_strb,
    output wire [31:0] mem_wdata,
    output wire        mem_ready,
    output wire [31:0] mem_rdata,
    output wire        mem_error,
    output wire        mem_fetch,
    // Retirement port, from the core.
    output wire        retire,
    output wire [31:0] retire_pc,
    output wire [31:0] retire_insn,
    output wire        retire_rd_we,
    output wire [4:0]  retire_rd,
    output wire [31:0] retire_rd_value,
    output wire        retire_fd_we,
    output wire [4:0]  retire_fd,
    output wire [31:0] retire_fd_value,
    output wire        retire_fcsr_we,
    output wire [7:0]  retire_fcsr,
    output wire        trap,
    output wire [3:0]  trap_cause,
    output wire [31:0] trap_value,
    output wire        halted,
    output wire [2:0]  state,
    output wire [31:0] pc,
    output wire [31:0] mtvec,
    output wire [31:0] mepc,
    output wire [31:0] mcause,
    output wire [31:0] mtval,
    // Device side effects for the host.
    output wire        console_valid,
    output wire [7:0]  console_byte,
    output wire        done_valid,
    output wire [31:0] done_wdata,
    output wire        display_present,
    output wire [31:0] display_frames,
    input  wire        in_push,
    input  wire [31:0] in_event,
    output wire        in_full
);
    wire gpu_valid, gpu_ready, gpu_error;
    wire [31:0] gpu_rdata;
    wire g3d_valid, g3d_ready, g3d_error;
    wire [31:0] g3d_rdata;
    wire simd_valid, simd_ready, simd_error;
    wire [31:0] simd_rdata;
    wire ram_valid, ram_ready, ram_error;
    wire [31:0] ram_rdata;
    wire con_valid, con_ready, con_error;
    wire [31:0] con_rdata;
    wire dn_valid, dn_ready, dn_error;
    wire [31:0] dn_rdata;
    wire tm_valid, tm_ready, tm_error;
    wire [31:0] tm_rdata;
    wire in_valid, in_ready, in_error;
    wire [31:0] in_rdata;
    wire dp_valid, dp_ready, dp_error;
    wire [31:0] dp_rdata;
    wire fb_valid, fb_ready, fb_error;
    wire [31:0] fb_rdata;

    rv32 core (
        .clk(clk), .reset(reset),
        .mem_valid(mem_valid), .mem_addr(mem_addr), .mem_we(mem_we), .mem_strb(mem_strb),
        .mem_wdata(mem_wdata), .mem_ready(mem_ready), .mem_rdata(mem_rdata), .mem_error(mem_error),
        .mem_fetch(mem_fetch),
        .retire(retire), .retire_pc(retire_pc), .retire_insn(retire_insn), .retire_rd_we(retire_rd_we),
        .retire_rd(retire_rd), .retire_rd_value(retire_rd_value), .trap(trap), .trap_cause(trap_cause),
        .retire_fd_we(retire_fd_we), .retire_fd(retire_fd), .retire_fd_value(retire_fd_value),
        .retire_fcsr_we(retire_fcsr_we), .retire_fcsr(retire_fcsr),
        .trap_value(trap_value), .halted(halted), .state(state), .pc(pc),
        .mtvec(mtvec), .mepc(mepc), .mcause(mcause), .mtval(mtval)
    );

    rv32_bus #(.RAM_WORDS(RAM_WORDS), .FB_WORDS(FB_WORDS)) bus (
        .mem_valid(mem_valid), .mem_we(mem_we), .mem_fetch(mem_fetch), .mem_hold(mem_hold),
        .mem_addr(mem_addr), .mem_ready(mem_ready), .mem_error(mem_error), .mem_rdata(mem_rdata),
        .ram_valid(ram_valid), .ram_ready(ram_ready), .ram_error(ram_error), .ram_rdata(ram_rdata),
        .console_valid(con_valid), .console_ready(con_ready), .console_error(con_error), .console_rdata(con_rdata),
        .done_valid(dn_valid), .done_ready(dn_ready), .done_error(dn_error), .done_rdata(dn_rdata),
        .timer_valid(tm_valid), .timer_ready(tm_ready), .timer_error(tm_error), .timer_rdata(tm_rdata),
        .input_valid(in_valid), .input_ready(in_ready), .input_error(in_error), .input_rdata(in_rdata),
        .display_valid(dp_valid), .display_ready(dp_ready), .display_error(dp_error), .display_rdata(dp_rdata),
        .fb_valid(fb_valid), .fb_ready(fb_ready), .fb_error(fb_error), .fb_rdata(fb_rdata),
        .gpu_valid(gpu_valid), .gpu_ready(gpu_ready), .gpu_error(gpu_error), .gpu_rdata(gpu_rdata),
        .simd_valid(simd_valid), .simd_ready(simd_ready), .simd_error(simd_error), .simd_rdata(simd_rdata),
        .g3d_valid(g3d_valid), .g3d_ready(g3d_ready), .g3d_error(g3d_error), .g3d_rdata(g3d_rdata)
    );

    rv32_simd4 accelerator (
        .clk(clk), .reset(reset), .valid(simd_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(simd_rdata), .ready(simd_ready), .error(simd_error),
        .memory_hold(simd_memory_hold)
    );

    // G1 and G2 share one engine memory port (em_*): whichever is busy owns it,
    // and each refuses START while the other runs, so the port never has two
    // masters. The G1 module itself is unchanged; its START is refused here.
    wire gpu_busy, gpu_cancel, gpu_source_lock, gm_valid, gm_we, gm_ready;
    wire [31:0] gpu_source_begin, gpu_source_end, gm_addr;
    wire [7:0] gm_wdata, gm_rdata;
    wire g3d_busy, g3d_cancel, g3m_valid, g3m_we;
    wire [31:0] g3d_zbase, g3m_addr, g3m_wdata;
    wire [3:0] g3m_strb;
    wire gpu_start_blocked = gpu_valid && mem_we && mem_addr[6:0] == 7'd0 && mem_wdata == 32'd1 && g3d_busy;
    wire gpu_device_ready, gpu_device_error;
    assign gpu_ready = gpu_start_blocked ? gpu_valid : gpu_device_ready;
    assign gpu_error = gpu_start_blocked || gpu_device_error;
    rv32_gpu #(.RAM_WORDS(RAM_WORDS)) graphics (
        .clk(clk), .reset(reset), .valid(gpu_valid && !gpu_start_blocked), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .ready(gpu_device_ready), .error(gpu_device_error), .rdata(gpu_rdata),
        .busy(gpu_busy), .cancel(gpu_cancel), .source_lock(gpu_source_lock),
        .source_begin(gpu_source_begin), .source_end(gpu_source_end),
        .memory_valid(gm_valid), .memory_we(gm_we), .memory_addr(gm_addr), .memory_wdata(gm_wdata),
        .memory_ready(gm_ready), .memory_rdata(gm_rdata)
    );
    wire em_ready;
    wire [31:0] em_read_word;
    rv32_g3d #(.RAM_WORDS(RAM_WORDS)) shader (
        .clk(clk), .reset(reset), .valid(g3d_valid), .we(mem_we), .addr(mem_addr), .wdata(mem_wdata), .strb(mem_strb),
        .other_busy(gpu_busy), .ready(g3d_ready), .error(g3d_error), .rdata(g3d_rdata), .busy(g3d_busy),
        .cancel(g3d_cancel), .zbase_out(g3d_zbase), .memory_valid(g3m_valid), .memory_we(g3m_we),
        .memory_addr(g3m_addr), .memory_strb(g3m_strb), .memory_wdata(g3m_wdata),
        .memory_ready(g3d_busy && em_ready), .memory_rdata(em_read_word)
    );
    wire engine_busy = gpu_busy || g3d_busy;
    wire em_cancel = gpu_cancel || g3d_cancel;
    wire em_valid = g3d_busy ? g3m_valid : gm_valid;
    wire em_we = g3d_busy ? g3m_we : gm_we;
    wire [31:0] em_addr = g3d_busy ? g3m_addr : gm_addr;
    wire [31:0] em_wdata = g3d_busy ? g3m_wdata : {4{gm_wdata}};
    // G1 reads RAM words and writes single framebuffer bytes.
    wire [3:0] em_strb = g3d_busy ? g3m_strb : gm_we ? 4'b0001 << gm_addr[1:0] : 4'b1111;
    wire gm_ram=em_valid && em_addr[31];
    wire gm_fb=em_valid && em_addr>=32'h3000_0000 && em_addr<32'h3001_2c00;
    // RAM is immediate once granted. A held graphics request retains its grant;
    // after an acceptance simultaneous requests alternate, so neither starves.
    reg prefer_gpu, gpu_grant_held;
    wire grant_gpu=gm_ram && (gpu_grant_held || !ram_valid || prefer_gpu);
    // CPU writes fault inside G1's blit source or G2's depth buffer while that engine runs.
    wire [31:0] lock_begin = g3d_busy ? g3d_zbase : gpu_source_begin;
    wire [31:0] lock_end = g3d_busy ? g3d_zbase + 32'd153600 : gpu_source_end;
    wire [3:0] source_byte_locked;
    genvar lane;
    generate for(lane=0;lane<4;lane=lane+1)begin: source_lanes
        wire [31:0] byte_addr={mem_addr[31:2],2'b00}+lane;
        assign source_byte_locked[lane]=mem_strb[lane] && byte_addr>=lock_begin && byte_addr<lock_end;
    end endgenerate
    wire ram_cpu_fault=ram_valid && mem_we && (gpu_source_lock || g3d_busy) && |source_byte_locked;
    wire ram_physical_valid=grant_gpu ? !gpu_memory_hold : ram_valid && !ram_cpu_fault;
    wire ram_physical_ready, ram_physical_error;
    wire [31:0] ram_physical_rdata;
    assign ram_ready=ram_valid && (ram_cpu_fault || (!grant_gpu && ram_physical_ready));
    assign ram_error=ram_cpu_fault || ram_physical_error;
    assign ram_rdata=ram_physical_rdata;
    always @(posedge clk) begin
        if(reset || em_cancel)begin prefer_gpu<=0;gpu_grant_held<=0;end
        else begin
            gpu_grant_held<=grant_gpu && gpu_memory_hold;
            if(grant_gpu)begin
                if(!gpu_memory_hold)prefer_gpu<=0;
            end else if(ram_valid && ram_ready)prefer_gpu<=1;
        end
    end
    // Memory BASE values repeat the bus decode; cross-language tests pin both.
    rv32_ram #(.WORDS(RAM_WORDS), .BASE(32'h8000_0000)) ram (
        .clk(clk), .reset(reset), .valid(ram_physical_valid), .we(grant_gpu ? em_we : mem_we),
        .addr(grant_gpu?em_addr:mem_addr), .strb(grant_gpu?em_strb:mem_strb), .wdata(grant_gpu?em_wdata:mem_wdata),
        .rdata(ram_physical_rdata), .ready(ram_physical_ready), .error(ram_physical_error)
    );

    rv32_console #(.BUSY_CYCLES(CONSOLE_BUSY)) console (
        .clk(clk), .reset(reset), .valid(con_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(con_rdata), .ready(con_ready), .error(con_error),
        .tx_valid(console_valid), .tx_byte(console_byte)
    );

    rv32_done done (
        .clk(clk), .reset(reset), .valid(dn_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(dn_rdata), .ready(dn_ready), .error(dn_error),
        .done_valid(done_valid), .done_word(done_wdata)
    );

    rv32_timer timer (
        .clk(clk), .reset(reset), .valid(tm_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(tm_rdata), .ready(tm_ready), .error(tm_error)
    );

    rv32_input input_device (
        .clk(clk), .reset(reset), .valid(in_valid), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(in_rdata), .ready(in_ready), .error(in_error),
        .push(in_push), .push_event(in_event), .full(in_full)
    );

    wire display_device_ready, display_device_error;
    wire present_locked=engine_busy && mem_we && mem_addr[3:0]==0;
    assign dp_ready=present_locked?dp_valid:display_device_ready;
    assign dp_error=present_locked || display_device_error;
    rv32_display display (
        .clk(clk), .reset(reset), .valid(dp_valid && !present_locked), .we(mem_we), .addr(mem_addr), .strb(mem_strb),
        .wdata(mem_wdata), .rdata(dp_rdata), .ready(display_device_ready), .error(display_device_error),
        .present(display_present), .frames(display_frames)
    );

    // The pixels: ordinary memory behind its own window (docs/rv32.md, "framebuffer").
    wire fb_physical_ready, fb_physical_error;
    wire [31:0] fb_physical_rdata;
    wire fb_engine_accept=gm_fb && !gpu_memory_hold;
    assign fb_ready=fb_valid && (engine_busy || fb_physical_ready);
    assign fb_error=engine_busy || fb_physical_error;
    assign fb_rdata=fb_physical_rdata;
    assign em_ready=gm_ram ? grant_gpu && !gpu_memory_hold && ram_physical_ready :
                            gm_fb && !gpu_memory_hold && fb_physical_ready;
    assign gm_ready=!g3d_busy && em_ready;
    assign em_read_word=gm_ram?ram_physical_rdata:fb_physical_rdata;
    assign gm_rdata=em_read_word[8*gm_addr[1:0]+:8];
    rv32_ram #(.WORDS(FB_WORDS), .BASE(32'h3000_0000)) fb (
        .clk(clk), .reset(reset), .valid(engine_busy?fb_engine_accept:fb_valid),
        .we(engine_busy?em_we:mem_we), .addr(gm_fb?em_addr:(fb_valid?mem_addr:32'h3000_0000)),
        .strb(engine_busy?em_strb:mem_strb),
        .wdata(engine_busy?em_wdata:mem_wdata),
        .rdata(fb_physical_rdata), .ready(fb_physical_ready), .error(fb_physical_error)
    );
endmodule
