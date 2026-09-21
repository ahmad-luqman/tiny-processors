`timescale 1ns/1ps
module fp32_tb;
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
    integer file, count=0, scan, read_count, latency, stall, max_latency=0, total_latency=0;
    integer op_in, rm_in, err_in, flags_in, k;
    integer latency_min[0:31], latency_max[0:31], samples[0:31];
    reg [31:0] a_in,b_in,c_in,expected;
    reg [4:0] expected_flags;
    string vectors, wave;
    string line_buffer;
    integer token_index;
    reg in_token;
    reg [7:0] token_char;
    string t_op,t_rm,t_a,t_b,t_c,t_result,t_flags,t_error,t_extra;
    task tick;
        begin @(posedge clk); #1; end
    endtask
    task drive;
        begin @(negedge clk); end
    endtask
    // Validate the token before accumulating it; %h into a 32-bit variable
    // would silently truncate over-width values and admit x/z digits.
    task parse_number;
        input string token;
        input integer base, max_digits;
        output reg [31:0] value;
        integer j, digit;
        reg [7:0] ch;
        begin
            if (token.len() == 0 || token.len() > max_digits)
                $fatal(1,"malformed vector %0d (numeric field width)",count);
            value=0;
            for(j=0;j<token.len();j=j+1) begin
                ch=token[j]; digit=-1;
                if(ch>="0" && ch<="9") digit=32'(ch)-48;
                else if(base==16 && ch>="a" && ch<="f") digit=32'(ch)-87;
                else if(base==16 && ch>="A" && ch<="F") digit=32'(ch)-55;
                if(digit<0 || digit>=base) $fatal(1,"malformed vector %0d (numeric field character)",count);
                value=value*32'(base)+32'(digit);
            end
        end
    endtask
    task send_request;
        begin
            drive();
            if (!req_ready) $fatal(1,"not ready before request %0d",count);
            op=5'(op_in); rm=3'(rm_in); a=a_in; b=b_in; c=c_in; req_valid=1;
            tick(); drive(); req_valid=0;
            // Change every source after acceptance: the unit must use its latches.
            op=31; rm=7; a=32'hdeadbeef; b=0; c=32'hffffffff;
        end
    endtask
    initial begin
        for(k=0;k<32;k=k+1) begin latency_min[k]=2048; latency_max[k]=0; samples[k]=0; end
        if ($value$plusargs("wave=%s",wave)) begin $dumpfile(wave); $dumpvars(0,fp32_tb); end
        if (!$value$plusargs("vectors=%s",vectors)) $fatal(1,"missing vectors");
        file=$fopen(vectors,"r");
        if (file == 0) $fatal(1,"cannot open vectors");
        tick(); drive(); reset=0;
        read_count=$fgets(line_buffer,file);
        while (read_count != 0) begin
            if(line_buffer.len() > 256 || line_buffer[line_buffer.len()-1] != 8'd10) $fatal(1,"malformed vector %0d (unterminated or overlong line)",count);
            // As in rv32_tb, split tokens ourselves: Icarus and Verilator
            // disagree on sscanf's count when an optional ninth %s hits EOF.
            scan=0; in_token=0;
            t_op=""; t_rm=""; t_a=""; t_b=""; t_c="";
            t_result=""; t_flags=""; t_error=""; t_extra="";
            for(token_index=0;token_index<line_buffer.len();token_index=token_index+1) begin
                token_char=line_buffer[token_index];
                if(token_char==8'h20 || token_char==8'h09 || token_char==8'h0a || token_char==8'h0d) in_token=0;
                else begin
                    if(!in_token) begin scan=scan+1; in_token=1; end
                    case(scan)
                        1: t_op={t_op,string'(token_char)};
                        2: t_rm={t_rm,string'(token_char)};
                        3: t_a={t_a,string'(token_char)};
                        4: t_b={t_b,string'(token_char)};
                        5: t_c={t_c,string'(token_char)};
                        6: t_result={t_result,string'(token_char)};
                        7: t_flags={t_flags,string'(token_char)};
                        8: t_error={t_error,string'(token_char)};
                        default: t_extra={t_extra,string'(token_char)};
                    endcase
                end
            end
            if (scan != 8) $fatal(1,"malformed vector %0d (%0d fields)",count,scan);
            parse_number(t_op,10,2,op_in); parse_number(t_rm,10,1,rm_in);
            parse_number(t_a,16,8,a_in); parse_number(t_b,16,8,b_in); parse_number(t_c,16,8,c_in);
            parse_number(t_result,16,8,expected); parse_number(t_flags,16,2,flags_in);
            parse_number(t_error,10,1,err_in);
            if (op_in<0 || op_in>31 || rm_in<0 || rm_in>7 || err_in<0 || err_in>1 || flags_in<0 || flags_in>31)
                $fatal(1,"out-of-range vector");
            expected_flags=5'(flags_in);
            send_request(); latency=0;
            while (!resp_valid) begin
                if (req_ready) $fatal(1,"ready while executing");
                tick(); latency=latency+1;
                if (latency>2048) $fatal(1,"timeout vector %0d",count);
            end
            if (result !== expected || flags !== expected_flags || error !== 1'(err_in))
                $fatal(1,"vector %0d op=%0d rm=%0d a=%h b=%h c=%h expected=%h/%h/%0d got=%h/%h/%b",count,op_in,rm_in,a_in,b_in,c_in,expected,expected_flags,err_in,result,flags,error);
            if (latency>max_latency) max_latency=latency;
            if (latency>latency_max[op_in]) latency_max[op_in]=latency;
            if (latency<latency_min[op_in]) latency_min[op_in]=latency;
            samples[op_in]=samples[op_in]+1;
            total_latency=total_latency+latency;
            // Hold another request while the response is stalled. It must not
            // overwrite the current result or be accepted before it is ready.
            drive(); req_valid=1;
            for(stall=0;stall<(count%5)+1;stall=stall+1) begin
                tick();
                if (!resp_valid || req_ready || result !== expected || flags !== expected_flags || error !== 1'(err_in))
                    $fatal(1,"unstable held response");
            end
            drive(); req_valid=0; resp_ready=1;
            tick();
            if (resp_valid || !req_ready) $fatal(1,"response did not consume exactly once");
            drive(); resp_ready=0;
            count=count+1;
            read_count=$fgets(line_buffer,file);
        end
        if (!$feof(file)) $fatal(1,"cannot read vectors");
        $fclose(file);
        if (count == 0) $fatal(1,"empty vector file");
        if ($test$plusargs("stats")) begin
            for(k=0;k<32;k=k+1) if(samples[k]>0)
                $display("LATENCY op=%0d samples=%0d min=%0d max=%0d",k,samples[k],latency_min[k],latency_max[k]);
        end
        $display("PASS fp32 vectors=%0d max_latency=%0d total_latency=%0d",count,max_latency,total_latency);
        $finish;
    end
endmodule
