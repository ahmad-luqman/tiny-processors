`timescale 1ns/1ps
module fp32_protocol_tb;
    reg clk=0;
    always #5 clk=~clk;
    reg reset=1, req_valid=0, resp_ready=0;
    wire req_ready, resp_valid;
    reg [4:0] op=0;
    reg [2:0] rm=0;
    reg [31:0] a=0,b=0,c=0;
    wire [31:0] result;
    wire [4:0] flags;
    wire error;
    fp32 dut(.*);
    integer checks=0, cycles, target, variant, i;
    reg [11:0] reset_states=0;
    string wave;
    task tick;
        begin @(posedge clk); #1; end
    endtask
    task drive;
        begin @(negedge clk); end
    endtask
    task start;
        input [4:0] opcode;
        input [31:0] av,bv,cv;
        begin
            drive();
            if (!req_ready) $fatal(1,"not ready at launch");
            op=opcode; rm=0; a=av; b=bv; c=cv; req_valid=1;
            tick(); drive(); req_valid=0;
        end
    endtask
    task check_response;
        input [31:0] value;
        input [4:0] exceptions;
        begin
            cycles=0;
            while (!resp_valid) begin
                if (req_ready) $fatal(1,"request ready before completion");
                tick(); cycles=cycles+1;
                if(cycles>2048) $fatal(1,"response timeout");
            end
            if (result !== value || flags !== exceptions || error)
                $fatal(1,"wrong protocol-test response %h/%h",result,flags);
            checks=checks+1;
        end
    endtask
    task consume;
        begin drive(); resp_ready=1; tick(); drive(); resp_ready=0; end
    endtask
    task reset_now;
        begin
            reset_states[dut.state]=1;
            drive(); reset=1; req_valid=1; resp_ready=1;
            #1;
            if(req_ready || resp_valid) $fatal(1,"transfer visible during reset");
            tick(); drive(); reset=0; req_valid=0; resp_ready=0; #1;
            if (!req_ready || resp_valid) $fatal(1,"reset did not clear interface");
            // Restart immediately; canceled work must neither complete nor
            // overwrite this distinguishable operation's flags or result.
            start(7,32'h3f800000,32'h40400000,0);
            check_response(32'h3eaaaaab,5'h01); consume();
            repeat(2049) begin tick(); if(resp_valid || !req_ready) $fatal(1,"stale completion after reset"); end
            checks=checks+1;
        end
    endtask
    initial begin
        if ($value$plusargs("wave=%s",wave)) begin $dumpfile(wave); $dumpvars(0,fp32_protocol_tb); end
        tick(); drive(); reset=0;
        reset_now(); // Idle, with valid on both interfaces during reset.
        // Reach and cancel every live controller state. Internal state is used
        // only to position reset; correctness is checked at the public port.
        for(target=1;target<=11;target=target+1) begin
            case(target)
                5,6: start(2,32'h00000001,32'h3f000000,0);
                7: start(7,32'h3f800000,32'h40400000,0);
                8: start(8,32'h40000000,0,0);
                9,10: start(11,32'h40200000,0,0);
                default: start(3,32'h3f800001,32'h3f7ffffe,32'hbf800000);
            endcase
            cycles=0;
            while(dut.state != 4'(target)) begin
                tick(); cycles=cycles+1;
                if(cycles>2048) $fatal(1,"unreachable reset target %0d",target);
            end
            reset_now();
        end
        // First, middle and final divide/sqrt iterations; these catch an
        // off-by-one completion that could survive a reset on the last step.
        for(target=7;target<=8;target=target+1) begin
            for(variant=1;variant<=27;variant=variant+13) begin
                start(5'(target),32'h40000000,32'h40400000,0);
                cycles=0;
                while(dut.state != 4'(target) || dut.iteration != 6'(variant)) begin
                    tick(); cycles=cycles+1;
                    if(cycles>2048) $fatal(1,"iteration reset target timeout");
                end
                reset_now();
            end
        end
        if(reset_states !== 12'hfff) $fatal(1,"missing reset state coverage %h",reset_states);
        // Keep a second legal request stable while the first runs/stalls.
        start(8,32'h40800000,0,0);
        drive(); op=0; rm=0; a=32'h3f800000; b=32'h3f800000; c=0; req_valid=1;
        check_response(32'h40000000,0);
        for(i=0;i<8;i=i+1) begin
            tick();
            if(req_ready || !resp_valid || result !== 32'h40000000 || flags !== 0 || error)
                $fatal(1,"backpressure lost first response");
        end
        drive(); resp_ready=1; tick();
        if(!req_ready || resp_valid) $fatal(1,"response handshake");
        // req_valid is still high: accept the waiting request on this edge.
        tick(); drive(); req_valid=0;
        check_response(32'h40000000,0);
        tick(); drive(); resp_ready=0;
        repeat(2049) begin tick(); if(resp_valid || !req_ready) $fatal(1,"duplicate completion"); end
        $display("PASS fp32 protocol checks=%0d reset_states=%h",checks,reset_states);
        $finish;
    end
endmodule
