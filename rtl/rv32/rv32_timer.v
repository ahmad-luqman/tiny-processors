`timescale 1ns/1ps

// Timer (docs/rv32.md, "Timer"): TICKS at +0 is the count of the current
// cycle, starting at 1 in the first cycle after reset release; a word write
// makes the count of its own cycle the written value, so a read accepted n
// cycles later returns value + n. `elapsed` holds the completed cycles and
// the read adds the one in progress. Every other offset and every byte or
// halfword access is refused. Nothing here knows about instructions: on the
// emulator the same register counts executed instructions (device time,
// docs/rv32.md).
module rv32_timer (
    input  wire        clk,
    input  wire        reset,
    input  wire        valid,
    input  wire        we,
    input  wire [31:0] addr,
    input  wire [3:0]  strb,
    input  wire [31:0] wdata,
    output wire [31:0] rdata,
    output wire        ready,
    output wire        error
);
    reg [31:0] elapsed;
    wire ticks_ok = (addr[3:2] == 2'd0) && (strb == 4'b1111);
    wire load = valid && we && ticks_ok;

    assign ready = valid;
    assign error = !ticks_ok;
    assign rdata = elapsed + 32'd1;

    always @(posedge clk) begin
        if (reset)
            elapsed <= 32'd0;
        else if (load)
            elapsed <= wdata;
        else
            elapsed <= elapsed + 32'd1;
    end

    wire unused_ok = &{1'b0, addr[31:4], addr[1:0]};
endmodule
