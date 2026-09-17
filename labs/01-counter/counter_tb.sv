`timescale 1ns/1ps

module counter_tb;
    reg clk = 0;
    reg reset = 0;
    reg enable = 0;
    wire [7:0] count;
    integer i;
    integer checks = 0;
    reg [1023:0] wave_path;

    counter dut (.clk(clk), .reset(reset), .enable(enable), .count(count));
    always #5 clk = ~clk;

    // Apply inputs away from the active clock edge, then sample after the
    // nonblocking assignments have updated the output.
    task step;
        input next_reset;
        input next_enable;
        input [7:0] expected;
        reg [7:0] previous;
        begin
            @(negedge clk);
            previous = count;
            reset = next_reset;
            enable = next_enable;
            #1;
            if (count !== previous)
                $fatal(1, "Output changed between rising edges");
            @(posedge clk);
            #1;
            if (count !== expected)
                $fatal(1, "Check %0d: reset=%b enable=%b expected=%0d got=%0d",
                       checks, reset, enable, expected, count);
            checks = checks + 1;
        end
    endtask

    initial begin
        if ($value$plusargs("wave=%s", wave_path)) begin
            $dumpfile(wave_path);
            $dumpvars(0, counter_tb);
        end
        step(1, 1, 0); // Reset has priority over enable.
        step(0, 0, 0); // Hold after reset.
        for (i = 1; i <= 255; i = i + 1)
            step(0, 1, i[7:0]);
        step(0, 0, 255); // Hold at the boundary.
        step(0, 1, 0);   // Eight-bit arithmetic wraps: 255 + 1 = 0.
        step(0, 1, 1);
        step(0, 0, 1);   // Hold a nonzero value.
        step(1, 1, 0);   // Synchronous reset from nonzero, enable asserted.
        step(0, 1, 1);
        step(1, 0, 0);   // Reset also works with enable disabled.
        $display("PASS: counter (%0d checked clock edges)", checks);
        $finish;
    end

    initial begin
        #10000;
        $fatal(1, "Simulation timed out");
    end
endmodule
