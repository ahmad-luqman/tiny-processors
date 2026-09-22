#include "rv32_gpu.h"
#include <string.h>
static int32_t min(int32_t a,int32_t b){return a<b?a:b;}
static int32_t max(int32_t a,int32_t b){return a>b?a:b;}
static bool coord(uint32_t a){return (int32_t)a>=-1024 && (int32_t)a<=1023;}
static uint64_t source_end(const uint32_t *p)
{return (uint64_t)p[GP_SRC]+(uint64_t)(p[GP_SH]?p[GP_SH]-1:0)*p[GP_STRIDE]+p[GP_SW];}
void gpu_device_reset(gpu_device *g){memset(g,0,sizeof *g);}
bool gpu_access(gpu_device *g,uint32_t off,int width,bool write,uint32_t *v)
{
    if(width!=4 || (off&3)) return false;
    if(off>=GPU_PARAMS && off<128) {
        if(write){if(gpu_busy(g))return false;g->p[(off-GPU_PARAMS)/4]=*v;}
        else *v=g->p[(off-GPU_PARAMS)/4];
        return true;
    }
    if(write) {
        if(off!=GPU_COMMAND)return false;
        if(*v==GPU_RESET){gpu_device_reset(g);g->command_tick=true;return true;}
        if(*v!=GPU_START || gpu_busy(g))return false;
        g->status=GPU_BUSY;g->phase=SETUP;g->error=g->cycles=g->stalls=g->reads=g->writes=0;
        g->command_tick=true;return true;
    }
    switch(off){
    case GPU_STATUS:*v=g->status;break;case GPU_ERROR:*v=g->error;break;
    case GPU_CYCLES:*v=g->cycles;break;case GPU_STALLS:*v=g->stalls;break;
    case GPU_READS:*v=g->reads;break;case GPU_WRITES:*v=g->writes;break;
    default:return false;
    }return true;
}
bool gpu_source_locked(const gpu_device *g,uint32_t addr,int width)
{
    const uint32_t *p=g->p;
    uint64_t end=source_end(p);
    return gpu_busy(g) && p[GP_OP]==GPU_BLIT && p[GP_SRC]>=0x80000000u &&
           p[GP_SH]!=0 && end<=UINT32_MAX &&
           (uint64_t)addr+width>p[GP_SRC] && addr<end;
}
static int32_t edge(int32_t ax,int32_t ay,int32_t bx,int32_t by,int32_t x,int32_t y)
{return (bx-ax)*(2*y+1-2*ay)-(by-ay)*(2*x+1-2*ax);}
static bool inside(int32_t ax,int32_t ay,int32_t bx,int32_t by,int32_t x,int32_t y)
{int32_t e=edge(ax,ay,bx,by,x,y);return e>0 || (!e && (by<ay || (by==ay && bx>ax)));}
void gpu_tick(gpu_device *g,uint8_t *ram,uint32_t ram_size,uint8_t *fb,bool hold)
{
    if(g->command_tick){g->command_tick=false;return;}
    if(!gpu_busy(g))return;
    uint32_t *p=g->p,op=p[GP_OP];
    g->cycles++;
    if(g->phase==SETUP){
        bool valid=op>=GPU_FILL && op<=GPU_TRIANGLE && p[GP_COLOR]<=255 && coord(p[GP_X0]) && coord(p[GP_Y0]);
        if(op==GPU_LINE || op==GPU_TRIANGLE)valid=valid&&coord(p[GP_X1])&&coord(p[GP_Y1]);
        if(op==GPU_TRIANGLE)valid=valid&&coord(p[GP_X2])&&coord(p[GP_Y2]);
        if(op==GPU_FILL || op==GPU_BLIT)valid=valid&&p[GP_W]<=2048&&p[GP_H]<=2048;
        if(op==GPU_BLIT){
            uint64_t end=source_end(p);
            bool source=p[GP_SRC]==0x30000000u ? p[GP_SW]==320&&p[GP_SH]==240&&p[GP_STRIDE]==320 :
                p[GP_SRC]>=0x80000000u && end<=(uint64_t)0x80000000u+ram_size;
            valid=valid&&coord(p[GP_SX])&&coord(p[GP_SY])&&p[GP_SW]>0&&p[GP_SW]<=2048&&
                p[GP_SH]>0&&p[GP_SH]<=2048&&p[GP_STRIDE]>=p[GP_SW]&&p[GP_STRIDE]<=65535&&source;
        }
        if(!valid){g->status=GPU_FAULT;g->error=GPU_INVALID;g->phase=IDLE;return;}
        g->ax=(int32_t)p[GP_X0];g->ay=(int32_t)p[GP_Y0];
        g->bx=(int32_t)p[GP_X1];g->by=(int32_t)p[GP_Y1];
        g->cx=(int32_t)p[GP_X2];g->cy=(int32_t)p[GP_Y2];
        if(op==GPU_LINE){
            if(g->ax>g->bx || (g->ax==g->bx&&g->ay>g->by)){
                int32_t t=g->ax;g->ax=g->bx;g->bx=t;t=g->ay;g->ay=g->by;g->by=t;
            }
            g->x=g->ax;g->y=g->ay;g->dx=g->bx-g->ax;g->dy=g->by-g->ay;
            g->sy=g->dy<0?-1:1;if(g->dy<0)g->dy=-g->dy;g->err=g->dx-g->dy;
        }else{
            if(op==GPU_FILL || op==GPU_BLIT){
                g->lo=max(0,g->ax);g->top=max(0,g->ay);
                g->hi=min(320,g->ax+(int32_t)p[GP_W]);g->bottom=min(240,g->ay+(int32_t)p[GP_H]);
            }
            g->step=1;
            if(op==GPU_BLIT){
                int32_t sx=(int32_t)p[GP_SX],sy=(int32_t)p[GP_SY];
                g->lo=max(g->lo,g->ax-sx);g->top=max(g->top,g->ay-sy);
                g->hi=min(g->hi,g->ax+(int32_t)p[GP_SW]-sx);g->bottom=min(g->bottom,g->ay+(int32_t)p[GP_SH]-sy);
                if(p[GP_SRC]==0x30000000u && g->ay*320+g->ax>sy*320+sx)g->step=-1;
            }
            if(op==GPU_TRIANGLE){
                int32_t area=(g->bx-g->ax)*(g->cy-g->ay)-(g->by-g->ay)*(g->cx-g->ax);
                if(!area){g->status=GPU_DONE;g->phase=IDLE;return;}
                if(area<0){int32_t t=g->bx;g->bx=g->cx;g->cx=t;t=g->by;g->by=g->cy;g->cy=t;}
                g->lo=max(0,min(g->ax,min(g->bx,g->cx)));g->top=max(0,min(g->ay,min(g->by,g->cy)));
                g->hi=min(320,max(g->ax,max(g->bx,g->cx)));g->bottom=min(240,max(g->ay,max(g->by,g->cy)));
            }
            if(g->lo>=g->hi || g->top>=g->bottom){g->status=GPU_DONE;g->phase=IDLE;return;}
            g->x=g->step>0?g->lo:g->hi-1;g->y=g->step>0?g->top:g->bottom-1;
        }
        g->phase=SCAN;
    }else if(g->phase==SCAN){
        bool covered=g->x>=0&&g->x<320&&g->y>=0&&g->y<240;
        if(op==GPU_TRIANGLE) covered=covered&&inside(g->ax,g->ay,g->bx,g->by,g->x,g->y)&&
            inside(g->bx,g->by,g->cx,g->cy,g->x,g->y)&&inside(g->cx,g->cy,g->ax,g->ay,g->x,g->y);
        g->pixel=(uint8_t)p[GP_COLOR];g->phase=covered?(op==GPU_BLIT?READ:WRITE):ADVANCE;
    }else if(g->phase==READ || g->phase==WRITE){
        if(hold){g->stalls++;return;}
        if(g->phase==READ){
            uint32_t off=(uint32_t)((int32_t)p[GP_SY]+g->y-g->ay)*p[GP_STRIDE]+(uint32_t)((int32_t)p[GP_SX]+g->x-g->ax);
            g->pixel=p[GP_SRC]==0x30000000u?fb[off]:ram[p[GP_SRC]-0x80000000u+off];
            g->reads++;g->phase=WRITE;
        }else{fb[g->y*320+g->x]=g->pixel;g->writes++;g->phase=ADVANCE;}
    }else if(g->phase==ADVANCE){
        bool done=false;
        if(op==GPU_LINE){
            done=g->x==g->bx&&g->y==g->by;int32_t e2=2*g->err;
            if(e2>=-g->dy){g->err-=g->dy;g->x++;}if(e2<=g->dx){g->err+=g->dx;g->y+=g->sy;}
        }else if(g->x==(g->step>0?g->hi-1:g->lo)){
            done=g->y==(g->step>0?g->bottom-1:g->top);g->x=g->step>0?g->lo:g->hi-1;g->y+=g->step;
        }else g->x+=g->step;
        g->status=done?GPU_DONE:GPU_BUSY;g->phase=done?IDLE:SCAN;
    }else{g->status=GPU_FAULT;g->error=GPU_INTERNAL;g->phase=IDLE;}
}
