`timescale 1ns/1ps

// PLIC (docs/rv32.md, "PLIC"; Track 2, O1): the platform-level interrupt
// controller at QEMU virt's address, in the SiFive/virt register layout, with
// one context (hart 0 in machine mode). Word access only:
//   +0x000004..0x00007c  priority of sources 1..31, 3 bits (source 0 reads 0)
//   +0x001000            pending word, read-only
//   +0x002000            context 0's enable word
//   +0x200000            context 0's threshold, 3 bits
//   +0x200004            claim (read) / complete (write)
// Only the WIRED sources hold a priority and an enable; the others read 0
// and ignore writes. Each wired source has a gateway, as in the PLIC
// specification and QEMU: its pending bit is set on a clock where its line is
// high and it is not claimed, and then stays set, whatever the line does,
// until a claim clears it. A claim returns the pending, enabled source with
// the highest priority above the threshold, ties to the lowest number, or 0,
// and marks it claimed; writing its number back while it is enabled completes
// it, and a line still high then sets the pending bit again. `meip` is set while a claim would
// return nonzero. Every other offset and every byte or halfword access is
// refused.
module rv32_plic #(
    parameter [31:0] WIRED = 32'h0000_1000  // source 12, the input queue
) (
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
    input  wire [31:0] lines,
    output wire        meip
);
    // Offsets within the window (tests/test_rv32_tools.py pins them to board.h).
    localparam [22:0] PENDING = 23'h00_1000, ENABLE = 23'h00_2000, THRESHOLD = 23'h20_0000, CLAIM = 23'h20_0004;

    reg [2:0] prio [0:31];
    reg [31:0] enable, claimed, pending;
    reg [2:0] threshold;

    wire [22:0] offset = addr[22:0];
    wire word = strb == 4'b1111;
    wire at_priority = offset[22:7] == 16'd0;
    wire at_pending = offset == PENDING, at_enable = offset == ENABLE;
    wire at_threshold = offset == THRESHOLD, at_claim = offset == CLAIM;
    wire known = at_priority || (at_pending && !we) || at_enable || at_threshold || at_claim;

    wire [31:0] candidates = pending & enable;

    // The claim: a priority encoder over the enabled pending sources, strictly
    // greater wins, so the lowest number keeps a tie.
    reg [4:0] best;
    reg [2:0] best_priority;
    integer id;
    always @* begin
        best = 5'd0;
        best_priority = threshold;
        for (id = 1; id < 32; id = id + 1)
            if (candidates[id] && prio[id] > best_priority) begin
                best = id[4:0];
                best_priority = prio[id];
            end
    end

    assign meip = best != 5'd0;
    assign ready = valid;
    assign error = !(known && word);
    assign rdata = at_priority ? {29'd0, prio[offset[6:2]]} :
                   at_pending ? pending :
                   at_enable ? enable :
                   at_threshold ? {29'd0, threshold} :
                   at_claim ? {27'd0, best} : 32'd0;

    wire accept = valid && word && known;
    integer k;
    always @(posedge clk) begin
        if (reset) begin
            for (k = 0; k < 32; k = k + 1) prio[k] <= 3'd0;
            enable <= 32'd0;
            claimed <= 32'd0;
            pending <= 32'd0;
            threshold <= 3'd0;
        end else begin
            // The gateways: a request latches while the line is high and the source is not claimed.
            // A claim below clears its bit in the same clock, which wins over the latch.
            pending <= pending | (lines & WIRED & ~claimed);
            if (accept) begin
                if (we && at_priority && WIRED[offset[6:2]]) prio[offset[6:2]] <= wdata[2:0];
                if (we && at_enable) enable <= wdata & WIRED;
                if (we && at_threshold) threshold <= wdata[2:0];
                if (we && at_claim && wdata[31:5] == 27'd0 && enable[wdata[4:0]]) claimed[wdata[4:0]] <= 1'b0;
                if (!we && at_claim && best != 5'd0) begin
                    claimed[best] <= 1'b1;
                    pending[best] <= 1'b0;
                end
            end
        end
    end

    wire unused_ok = &{1'b0, addr[31:23]};
endmodule
