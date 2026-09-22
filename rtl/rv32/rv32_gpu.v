`timescale 1ns/1ps
// G1 bounded integer rasterizer. Byte transfers; one outstanding request.
module rv32_gpu #(
    parameter integer RAM_WORDS=1048576
)(
    input wire clk, reset, valid, we,
    input wire [31:0] addr, wdata,
    input wire [3:0] strb,
    output wire ready, error,
    output reg [31:0] rdata,
    output wire busy, cancel,
    output wire source_lock,
    // Meaningful only while source_lock: half-open byte range [begin,end).
    output wire [31:0] source_begin, source_end,
    output wire memory_valid, memory_we,
    output wire [31:0] memory_addr,
    output wire [7:0] memory_wdata,
    input wire memory_ready,
    input wire [7:0] memory_rdata
);
    localparam [63:0] RAM_BYTES=RAM_WORDS*64'd4;
    localparam [2:0] IDLE=0, SETUP=1, SCAN=2, READ=3, WRITE=4, ADVANCE=5;
    localparam [31:0] GPU_COMMAND = 32'h00000000;
    localparam [31:0] GPU_STATUS = 32'h00000004;
    localparam [31:0] GPU_ERROR = 32'h00000008;
    localparam [31:0] GPU_CYCLES = 32'h0000000c;
    localparam [31:0] GPU_STALLS = 32'h00000010;
    localparam [31:0] GPU_READS = 32'h00000014;
    localparam [31:0] GPU_WRITES = 32'h00000018;
    localparam [31:0] GPU_PARAMS = 32'h00000040;
    localparam [31:0] GPU_START = 32'h00000001;
    localparam [31:0] GPU_RESET = 32'h00000002;
    localparam [31:0] GPU_BUSY = 32'h00000001;
    localparam [31:0] GPU_DONE = 32'h00000002;
    localparam [31:0] GPU_FAULT = 32'h00000004;
    localparam [31:0] GPU_FILL = 32'h00000001;
    localparam [31:0] GPU_BLIT = 32'h00000002;
    localparam [31:0] GPU_LINE = 32'h00000003;
    localparam [31:0] GPU_TRIANGLE = 32'h00000004;
    localparam [31:0] GPU_IDLE = 32'h00000000;
    localparam [31:0] GPU_INVALID = 32'h00000001;
    localparam [31:0] GPU_INTERNAL = 32'h00000002;
    localparam integer OP=0,COLOR=1,X0=2,Y0=3,X1=4,Y1=5,X2=6,Y2=7,
                       W=8,H=9,SRC=10,STRIDE=11,SW=12,SH=13,SX=14,SY=15;
    reg [31:0] p[0:15];
    reg [2:0] state;
    // status==GPU_BUSY iff state!=IDLE.
    reg [31:0] status, reason, cycles, stalls, reads, writes;
    reg signed [31:0] x,y,lo,top,hi,bottom,step,dx,dy,sy,err;
    reg signed [31:0] ax,ay,bx,by,cx,cy;
    reg [7:0] pixel;
    wire [31:0] offset={25'd0,addr[6:0]};
    wire param_sel=offset>=GPU_PARAMS;
    wire [3:0] index=addr[5:2];
    wire reset_write=valid && we && strb==4'b1111 && offset==GPU_COMMAND && wdata==GPU_RESET;
    wire start_write=valid && we && strb==4'b1111 && offset==GPU_COMMAND && wdata==GPU_START && !busy;
    assign busy=status==GPU_BUSY;
    assign cancel=reset || reset_write;
    wire read_ok=param_sel || (offset>=GPU_STATUS && offset<=GPU_WRITES);
    wire write_ok=(param_sel && !busy) || (offset==GPU_COMMAND && (wdata==GPU_RESET || (wdata==GPU_START && !busy)));
    assign ready=valid;
    assign error=strb!=4'b1111 || addr[1:0]!=0 || (we?!write_ok:!read_ok);
    always @* begin
        if(param_sel) rdata=p[index];
        else case(offset)
            GPU_STATUS:rdata=status;GPU_ERROR:rdata=reason;GPU_CYCLES:rdata=cycles;GPU_STALLS:rdata=stalls;
            GPU_READS:rdata=reads;GPU_WRITES:rdata=writes;default:rdata=0;
        endcase
    end
    function signed [31:0] min2;
        input signed [31:0] a,b;begin min2=a<b?a:b;end
    endfunction
    function signed [31:0] max2;
        input signed [31:0] a,b;begin max2=a>b?a:b;end
    endfunction
    function coord;
        input signed [31:0] a;begin coord=a>=-1024 && a<=1023;end
    endfunction
    function edge_included;
        input signed [31:0] edge_ax,edge_ay,edge_bx,edge_by,px,py;
        reg signed [31:0] e;
        begin e=(edge_bx-edge_ax)*(2*py+1-2*edge_ay)-(edge_by-edge_ay)*(2*px+1-2*edge_ax);
            edge_included=e>0 || (e==0 && (edge_by<edge_ay || (edge_by==edge_ay && edge_bx>edge_ax))); end
    endfunction
    // Full-width source validation happens before narrowing any address.
    // Algebraically identical modulo 2^64, including invalid SH=0, but both
    // multiplicands now have only 32 live bits; retain the full 64-bit result.
    wire [63:0] extent={32'd0,p[SH]}*{32'd0,p[STRIDE]}+{32'd0,p[SW]}-{32'd0,p[STRIDE]};
    wire [63:0] end_address={32'd0,p[SRC]}+extent;
    wire ram_source=p[SRC]>=32'h8000_0000 && end_address<=64'h8000_0000+RAM_BYTES;
    assign source_lock=busy && p[OP]==GPU_BLIT && p[SRC]>=32'h8000_0000 && p[SH]!=0 && end_address<=64'hffff_ffff;
    assign source_begin=p[SRC];
    assign source_end=end_address[31:0];
    wire fb_source=p[SRC]==32'h3000_0000;
    wire dest_after_source=$signed(p[Y0])*320+$signed(p[X0])>$signed(p[SY])*320+$signed(p[SX]);
    reg good, empty;
    reg signed [31:0] na,nb,nc,nd,ne,nf,nlo,ntop,nhi,nbottom,nstep,area,tmp;
    always @* begin
        good=p[OP]>=GPU_FILL && p[OP]<=GPU_TRIANGLE && p[COLOR]<=255 && coord(p[X0]) && coord(p[Y0]);
        if(p[OP]==GPU_LINE || p[OP]==GPU_TRIANGLE)good=good && coord(p[X1]) && coord(p[Y1]);
        if(p[OP]==GPU_TRIANGLE)good=good && coord(p[X2]) && coord(p[Y2]);
        if(p[OP]==GPU_FILL || p[OP]==GPU_BLIT)good=good && p[W]<=2048 && p[H]<=2048;
        if(p[OP]==GPU_BLIT)good=good && coord(p[SX]) && coord(p[SY]) && p[SW]>0 && p[SW]<=2048 &&
            p[SH]>0 && p[SH]<=2048 && p[STRIDE]>=p[SW] && p[STRIDE]<=65535 &&
            ((fb_source && p[SW]==320 && p[SH]==240 && p[STRIDE]==320) || ram_source);
        na=$signed(p[X0]);nb=$signed(p[Y0]);nc=$signed(p[X1]);nd=$signed(p[Y1]);ne=$signed(p[X2]);nf=$signed(p[Y2]);
        tmp=0;area=0;nstep=1;empty=0;
        nlo=max2(0,na);ntop=max2(0,nb);nhi=min2(320,na+$signed(p[W]));nbottom=min2(240,nb+$signed(p[H]));
        if(p[OP]==GPU_LINE && (na>nc || (na==nc && nb>nd)))begin
            tmp=na;na=nc;nc=tmp;tmp=nb;nb=nd;nd=tmp;
        end
        if(p[OP]==GPU_BLIT)begin
            nlo=max2(nlo,na-$signed(p[SX]));ntop=max2(ntop,nb-$signed(p[SY]));
            nhi=min2(nhi,na+$signed(p[SW])-$signed(p[SX]));nbottom=min2(nbottom,nb+$signed(p[SH])-$signed(p[SY]));
            if(fb_source && dest_after_source)nstep=-1;
        end
        if(p[OP]==GPU_TRIANGLE)begin
            area=(nc-na)*(nf-nb)-(nd-nb)*(ne-na);
            if(area<0)begin tmp=nc;nc=ne;ne=tmp;tmp=nd;nd=nf;nf=tmp;end
            nlo=max2(0,min2(na,min2(nc,ne)));ntop=max2(0,min2(nb,min2(nd,nf)));
            nhi=min2(320,max2(na,max2(nc,ne)));nbottom=min2(240,max2(nb,max2(nd,nf)));
            if(area==0)empty=1;
        end
        if(p[OP]!=GPU_LINE && (nlo>=nhi || ntop>=nbottom))empty=1;
    end
    wire covered=x>=0 && x<320 && y>=0 && y<240 && (p[OP]!=GPU_TRIANGLE ||
        (edge_included(ax,ay,bx,by,x,y) && edge_included(bx,by,cx,cy,x,y) && edge_included(cx,cy,ax,ay,x,y)));
    wire [31:0] source_offset=($signed(p[SY])+y-ay)*$signed(p[STRIDE])+$signed(p[SX])+x-ax;
    assign memory_valid=(state==READ || state==WRITE) && !cancel;
    assign memory_we=state==WRITE;
    assign memory_addr=state==READ?p[SRC]+source_offset:32'h3000_0000+$unsigned(y)*320+$unsigned(x);
    assign memory_wdata=pixel;
    wire signed [31:0] twice_err=err*2;
    wire step_x=twice_err>=-dy, step_y=twice_err<=dx;
    wire signed [31:0] abs_dy=nd<nb?nb-nd:nd-nb;
    wire last_line=x==bx && y==by;
    wire last_column=x==(step>0?hi-1:lo);
    wire last_row=y==(step>0?bottom-1:top);
    integer i;
    always @(posedge clk)begin
        if(cancel)begin
            state<=IDLE;status<=GPU_IDLE;reason<=0;cycles<=0;stalls<=0;reads<=0;writes<=0;
            x<=0;y<=0;lo<=0;top<=0;hi<=0;bottom<=0;step<=0;dx<=0;dy<=0;sy<=0;err<=0;
            ax<=0;ay<=0;bx<=0;by<=0;cx<=0;cy<=0;pixel<=0;
            for(i=0;i<16;i=i+1)p[i]<=0;
        end else begin
            if(valid && we && !error && param_sel)p[index]<=wdata;
            if(start_write)begin state<=SETUP;status<=GPU_BUSY;reason<=0;cycles<=0;stalls<=0;reads<=0;writes<=0;end
            else if(busy)begin
                cycles<=cycles+1;
                case(state)
                    SETUP:begin
                        if(!good)begin status<=GPU_FAULT;reason<=GPU_INVALID;state<=IDLE;end
                        else if(empty)begin status<=GPU_DONE;state<=IDLE;end
                        else begin
                            ax<=na;ay<=nb;bx<=nc;by<=nd;cx<=ne;cy<=nf;
                            lo<=nlo;top<=ntop;hi<=nhi;bottom<=nbottom;step<=nstep;
                            if(p[OP]==GPU_LINE)begin
                                x<=na;y<=nb;dx<=nc-na;dy<=abs_dy;sy<=nd<nb?-1:1;
                                err<=nc-na-abs_dy;
                            end else begin x<=nstep>0?nlo:nhi-1;y<=nstep>0?ntop:nbottom-1;end
                            state<=SCAN;
                        end
                    end
                    SCAN:begin pixel<=p[COLOR][7:0];state<=covered?(p[OP]==GPU_BLIT?READ:WRITE):ADVANCE;end
                    READ,WRITE:begin
                        if(!memory_ready)stalls<=stalls+1;
                        else if(state==READ)begin pixel<=memory_rdata;reads<=reads+1;state<=WRITE;end
                        else begin writes<=writes+1;state<=ADVANCE;end
                    end
                    ADVANCE:begin
                        state<=SCAN;
                        if(p[OP]==GPU_LINE)begin
                            if(last_line)begin status<=GPU_DONE;state<=IDLE;end
                            if(step_x)x<=x+1;
                            if(step_y)y<=y+sy;
                            err<=err-(step_x?dy:0)+(step_y?dx:0);
                        end else if(last_column)begin
                            if(last_row)begin status<=GPU_DONE;state<=IDLE;end
                            x<=step>0?lo:hi-1;y<=y+step;
                        end else x<=x+step;
                    end
                    default:begin state<=IDLE;status<=GPU_FAULT;reason<=GPU_INTERNAL;end
                endcase
            end
        end
    end
    wire unused_ok=&{1'b0,addr[31:7],extent[63:32]};
endmodule
