`timescale 1ns/1ps

// Palette (docs/rv32.md, "Display"; issue #35): 256 words at 0x1100_3000, word
// N the colour of pixel value N as 0x00RRGGBB. A word read returns it with the
// top byte zero; a word write keeps the low 24 bits. Any other width is
// refused. Its power-on contents are the fixed RGB332 mapping the framebuffer
// had before (bits 7:5 red, 4:2 green, 1:0 blue, each scaled to 0..255), so a
// program that never writes it looks as it always did; a reset leaves it as it
// is, like the framebuffer. The RTL itself colours nothing: the testbench's
// checkpoints hash pixel indices, and only the emulator's PPM writer and the
// window colour a frame, through the emulator's copy of this table. The same
// power-on table is in tools/rv32emu_core.c, tools/rv32_devices.py and
// programs/rv32/os/palcheck.c; tests compare them. ENTRIES is 256 except in
// synthesis, which shrinks it as it shrinks RAM; the index then wraps.
module rv32_palette #(
    parameter integer ENTRIES = 256
) (
    input  wire        clk,
    input  wire        valid,
    input  wire        we,
    input  wire [31:0] addr,
    input  wire [3:0]  strb,
    input  wire [31:0] wdata,
    output wire [31:0] rdata,
    output wire        ready,
    output wire        error
);
    reg [23:0] colours [0:ENTRIES-1];

    wire word = (strb == 4'b1111);
    localparam integer MASK = ENTRIES - 1;  // ENTRIES is a power of two
    wire [7:0] slot = addr[9:2] & MASK[7:0];

    assign ready = valid;
    assign error = !word;
    assign rdata = {8'd0, colours[slot]};

    always @(posedge clk) begin
        if (valid && we && word)
            colours[slot] <= wdata[23:0];
    end

    // A 3-bit field scaled to 0..255 (field * 255 / 7, truncated), and a 2-bit one (field * 255 / 3).
    function [7:0] level7;
        input [2:0] field;
        case (field)
            3'd0: level7 = 8'd0;   3'd1: level7 = 8'd36;  3'd2: level7 = 8'd72;  3'd3: level7 = 8'd109;
            3'd4: level7 = 8'd145; 3'd5: level7 = 8'd182; 3'd6: level7 = 8'd218; default: level7 = 8'd255;
        endcase
    endfunction

    function [7:0] level3;
        input [1:0] field;
        case (field)
            2'd0: level3 = 8'd0; 2'd1: level3 = 8'd85; 2'd2: level3 = 8'd170; default: level3 = 8'd255;
        endcase
    endfunction

`ifndef SYNTHESIS
    initial if (ENTRIES < 1 || ENTRIES > 256 || (ENTRIES & (ENTRIES - 1)) != 0)
        begin $display("rv32_palette: ENTRIES must be a power of two from 1 to 256, not %0d", ENTRIES); $stop; end
`endif

    // The levels are tables, not field * 255 / 7, so no wide arithmetic leaves unused bits for lint.
    integer i;
    initial begin
        for (i = 0; i < ENTRIES; i = i + 1)
            colours[i] = {level7(i[7:5]), level7(i[4:2]), level3(i[1:0])};
    end

    wire unused_ok = &{1'b0, wdata[31:24], addr[31:10], addr[1:0]};
endmodule
