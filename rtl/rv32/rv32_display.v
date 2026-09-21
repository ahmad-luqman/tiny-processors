`timescale 1ns/1ps

// Display controller (docs/rv32.md, "Display"): a word write to PRESENT at
// +0 raises `present` for the accepting cycle and counts a frame; FRAMES at
// +4, WIDTH at +8, and HEIGHT at +12 are read-only words. Every other
// access is refused. The pixels are not here: they are an rv32_ram instance
// behind the framebuffer window, and the host snapshots that memory when it
// sees `present`, which is all "presenting" means in M5.
module rv32_display #(
    parameter integer COLUMNS = 320,
    parameter integer ROWS = 240
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
    output wire        present,
    output reg  [31:0] frames
);
    localparam [31:0] WIDTH = COLUMNS;
    localparam [31:0] HEIGHT = ROWS;

    wire word = (strb == 4'b1111);
    wire [1:0] which = addr[3:2];
    wire present_ok = we && word && (which == 2'd0);
    wire read_ok = !we && word && (which != 2'd0);

    assign ready = valid;
    assign error = !(present_ok || read_ok);
    assign rdata = (which == 2'd1) ? frames : (which == 2'd2) ? WIDTH : (which == 2'd3) ? HEIGHT : 32'd0;
    assign present = valid && present_ok;

    always @(posedge clk) begin
        if (reset)
            frames <= 32'd0;
        else if (present)
            frames <= frames + 32'd1;
    end

    wire unused_ok = &{1'b0, wdata, addr[31:4], addr[1:0]};
endmodule
