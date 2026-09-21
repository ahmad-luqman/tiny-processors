`timescale 1ns/1ps

// Done register (docs/rv32.md, "Done register"): a word store presents
// `done_valid` with the word for one cycle; the host decides what the word
// means and ends the run after the storing instruction retires. Any other
// width and every read is refused. Stateless: the bus selects it on its
// exact address, so the address is not needed here.
module rv32_done (
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
    output wire        done_valid,
    output wire [31:0] done_word
);
    wire word_write = we && (strb == 4'b1111);

    assign ready = valid;
    assign error = !word_write;
    assign rdata = 32'd0;
    assign done_valid = valid && word_write;
    assign done_word = wdata;

    wire unused_ok = &{1'b0, clk, reset, addr};
endmodule
