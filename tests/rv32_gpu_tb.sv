`timescale 1ns/1ps
module rv32_gpu_tb;
    reg clk=0;always #5 clk=~clk;
    reg reset=1,valid=0,we=0;
    reg [31:0] addr=0,wdata=0;
    reg [3:0] strb=15;
    wire ready,error,busy,cancel,source_lock,memory_valid,memory_we;
    wire [31:0] rdata,source_begin,source_end,memory_addr;
    wire [7:0] memory_wdata;
    reg memory_ready=0;
    reg [7:0] ram[0:262143],fb[0:76799];
    wire [7:0] memory_rdata=memory_addr[31]?ram[memory_addr-32'h80000000]:fb[memory_addr-32'h30000000];
    rv32_gpu #(.RAM_WORDS(65536)) dut(.*);
    integer file,ret,n,i,mode,abort_tick,ticks,case_count=0;
    reg [31:0] words[0:15],hash;
    string input_path,wave_path,token;
    integer j;
    function valid_hex(input string t);
        integer k;reg [7:0] ch;begin
            valid_hex=t.len()==8;
            for(k=0;k<t.len();k=k+1)begin ch=t[k];
                if(!((ch>=48 && ch<=57)||(ch>=65 && ch<=70)||(ch>=97 && ch<=102)))valid_hex=0;
            end
        end
    endfunction
    reg held=0;reg [31:0] held_addr;reg held_we;reg [7:0] held_data;
    always @(posedge clk)begin
        if(memory_valid && memory_ready)begin
            if(memory_addr[31])begin
                if(memory_we || memory_addr<32'h80000000 || memory_addr>=32'h80040000)$fatal(1,"bad RAM access");
            end else if(memory_addr<32'h30000000 || memory_addr>=32'h30012c00)$fatal(1,"bad framebuffer access");
            if(memory_we)fb[memory_addr-32'h30000000]<=memory_wdata;
        end
        if(held && !cancel && (!memory_valid || memory_addr!==held_addr || memory_we!==held_we || memory_wdata!==held_data))$fatal(1,"unstable held request");
        held<=memory_valid && !memory_ready;
        held_addr<=memory_addr;held_we<=memory_we;held_data<=memory_wdata;
    end
    task access_write(input [31:0] a,input [31:0] v);
        begin @(negedge clk);valid=1;we=1;addr=a;wdata=v;#1;
            if(!ready || error)$fatal(1,"refused legal write");
            @(negedge clk);valid=0;we=0;
        end
    endtask
    initial begin
        if(!$value$plusargs("input=%s",input_path))$fatal(1,"missing input");
        if($value$plusargs("wave=%s",wave_path))begin $dumpfile(wave_path);$dumpvars(0,rv32_gpu_tb);end
        file=$fopen(input_path,"r");if(file==0)$fatal(1,"cannot open input");
        for(i=0;i<262144;i=i+1)ram[i]=8'(i*17+3);
        for(i=0;i<76800;i=i+1)fb[i]=8'(i*13+7);
        repeat(2)@(negedge clk);reset=0;
        ret=$fscanf(file,"%d %d",mode,abort_tick);
        while(ret==2)begin
            if(mode<0 || mode>3 || abort_tick < -1 || abort_tick>4000000)$fatal(1,"bad mode/reset tick");
            for(n=0;n<16;n=n+1)begin
                ret=$fscanf(file,"%s",token);if(ret!=1)$fatal(1,"short command");
                if(!valid_hex(token))$fatal(1,"invalid parameter token");
                j=$sscanf(token,"%h",words[n]);if(j!=1)$fatal(1,"invalid parameter token");
            end
            for(n=0;n<16;n=n+1)access_write(64+4*n,words[n]);
            // START accepting edge is followed by SETUP as tick one.
            access_write(0,1);ticks=0;
            while(busy)begin
                memory_ready=!((mode&1)!=0 && (ticks%7)<2);
                if(abort_tick>=0 && ticks==abort_tick)begin if((mode&2)!=0)reset=1;else begin valid=1;we=1;addr=0;wdata=2;end end
                @(negedge clk);ticks=ticks+1;
                valid=0;we=0;reset=0;
                if(ticks>4000000)$fatal(1,"timeout");
            end
            hash=32'h811c9dc5;
            for(i=0;i<76800;i=i+1)hash=(hash^{24'd0,fb[i]})*32'h01000193;
            $display("RESULT %0d %08x %08x %08x %08x %08x %08x %08x",case_count,hash,dut.status,dut.reason,dut.cycles,dut.stalls,dut.reads,dut.writes);
            case_count=case_count+1;
            ret=$fscanf(file,"%d %d",mode,abort_tick);
        end
        if(!$feof(file) || case_count==0)$fatal(1,"malformed/empty input");
        $display("PASS %0d",case_count);$finish;
    end
endmodule
