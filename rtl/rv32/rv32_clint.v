`timescale 1ns/1ps

// CLINT (docs/rv32.md, "CLINT"): the core-local interruptor at QEMU virt's
// address, for one hart. Three registers, word access only:
//   +0x0000  msip      bit 0 read/write, other bits read 0
//   +0x4000  mtimecmp  low word, +0x4004 high word; resets to all ones
//   +0xbff8  mtime     low word, +0xbffc high word
// mtime is the count of the current cycle, starting at 1 in the first cycle
// after reset release (the M5 timer's rule, widened to 64 bits); a write to
// one half makes the count of its own cycle the old count with that half
// replaced, so a read accepted n cycles later returns it plus n. The count
// also leaves on `mtime` for the core's `time` CSR. Every other offset and
// every byte or halfword access is refused. msip and mtimecmp are plain
// registers until the core takes interrupts (O1): nothing reads them yet.
module rv32_clint (
    input  wire        clk,
    input  wire        reset,
    input  wire        valid,
    input  wire        we,
    input  wire [31:0] addr,
    input  wire [3:0]  strb,
    input  wire [31:0] wdata,
    output wire [31:0] rdata,
    output wire        ready,
    output wire        error,
    output wire [63:0] mtime
);
    // Register offsets within the 64 KiB window (tests/test_rv32_tools.py pins them to board.h).
    localparam [15:0] MSIP = 16'h0000, MTIMECMP = 16'h4000, MTIME = 16'hbff8;

    reg [63:0] elapsed, mtimecmp;
    reg msip;
    wire [15:0] offset = addr[15:0];
    wire word = strb == 4'b1111;
    wire at_msip = offset == MSIP, at_cmp_lo = offset == MTIMECMP, at_cmp_hi = offset == MTIMECMP + 16'd4;
    wire at_time_lo = offset == MTIME, at_time_hi = offset == MTIME + 16'd4;
    wire known = at_msip || at_cmp_lo || at_cmp_hi || at_time_lo || at_time_hi;
    wire write = valid && we && word;

    assign mtime = elapsed + 64'd1;
    assign ready = valid;
    // Like every slave's, `error` describes the presented address whether or not `valid` is set:
    // the bus reads it only through `clint_sel`, and only when it also sees `ready`.
    assign error = !(known && word);
    assign rdata = at_msip ? {31'd0, msip} :
                   at_cmp_lo ? mtimecmp[31:0] :
                   at_cmp_hi ? mtimecmp[63:32] :
                   at_time_lo ? mtime[31:0] : mtime[63:32];

    always @(posedge clk) begin
        if (reset) begin
            elapsed <= 64'd0;
            mtimecmp <= {64{1'b1}};
            msip <= 1'b0;
        end else begin
            if (write && at_time_lo)
                elapsed <= {mtime[63:32], wdata};
            else if (write && at_time_hi)
                elapsed <= {wdata, mtime[31:0]};
            else
                elapsed <= elapsed + 64'd1;
            if (write && at_cmp_lo) mtimecmp[31:0] <= wdata;
            if (write && at_cmp_hi) mtimecmp[63:32] <= wdata;
            if (write && at_msip) msip <= wdata[0];
        end
    end

    wire unused_ok = &{1'b0, addr[31:16], wdata[31:1]};
endmodule
