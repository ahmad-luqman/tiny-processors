`timescale 1ns/1ps

// Input device (docs/rv32.md, "Input"): a queue of up to 16 key events that
// the host pushes one per cycle through `push`/`push_event`, and three
// read-only words: EVENT at +0 pops the oldest event (0 when empty), COUNT
// at +4, and KEYS at +8, one bit per key code, set by a press and cleared
// by a release as they arrive. While the host is pushing, the device holds
// `ready` low, so a burst of events lands whole before the guest can see
// any of it: the emulator queues a frame's events in one step, and this is
// how the sequence the guest reads stays identical. A push into a full
// queue is ignored; `full` lets the host report the drop.
module rv32_input (
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
    input  wire        push,
    input  wire [31:0] push_event,
    output wire        full
);
    reg [31:0] queue [0:15];
    reg [3:0] head, tail;
    reg [4:0] count;
    reg [31:0] keys;

    wire word = (strb == 4'b1111);
    wire [1:0] which = addr[3:2];
    wire read_ok = !we && word && (which != 2'd3);
    wire empty = (count == 5'd0);

    assign full = (count == 5'd16);
    assign ready = valid && !push;
    assign error = !read_ok;
    assign rdata = (which == 2'd0) ? (empty ? 32'd0 : queue[head]) :
                   (which == 2'd1) ? {27'd0, count} :
                   (which == 2'd2) ? keys : 32'd0;

    wire pop = valid && ready && read_ok && (which == 2'd0) && !empty;
    wire take = push && !full;

    always @(posedge clk) begin
        if (reset) begin
            head <= 4'd0;
            tail <= 4'd0;
            count <= 5'd0;
            keys <= 32'd0;
        end else begin
            if (take) begin
                queue[tail] <= push_event;
                tail <= tail + 4'd1;
                keys[push_event[4:0]] <= push_event[8];
            end
            if (pop)
                head <= head + 4'd1;
            count <= count + {4'd0, take} - {4'd0, pop};
        end
    end

    wire unused_ok = &{1'b0, wdata, addr[31:4], addr[1:0]};
endmodule
