#include "gpu.h"
#include "gfx.h"

void gpu_command_init(struct gpu_command *c, uint32_t op, uint32_t color)
{
    for (uint32_t i=0;i<GP_COUNT;i++) c->p[i]=0;
    c->p[GP_OP]=op; c->p[GP_COLOR]=color;
}
static void pixel(uint8_t *fb, int32_t x, int32_t y, uint8_t c)
{
    if (x>=0 && x<320 && y>=0 && y<240) fb[(uint32_t)y*320u+(uint32_t)x]=c;
}
static int32_t edge(int32_t ax,int32_t ay,int32_t bx,int32_t by,int32_t x,int32_t y)
{ return (bx-ax)*(2*y+1-2*ay)-(by-ay)*(2*x+1-2*ax); }
static int inside(int32_t ax,int32_t ay,int32_t bx,int32_t by,int32_t x,int32_t y)
{
    int32_t e=edge(ax,ay,bx,by,x,y);
    return e>0 || (e==0 && (by<ay || (by==ay && bx>ax)));
}
void gpu_reference(uint8_t *fb, const uint8_t *source, const struct gpu_command *c)
{
    const uint32_t *p=c->p;
    int32_t x0=(int32_t)p[GP_X0],y0=(int32_t)p[GP_Y0];
    int32_t x1=(int32_t)p[GP_X1],y1=(int32_t)p[GP_Y1];
    int32_t x2=(int32_t)p[GP_X2],y2=(int32_t)p[GP_Y2];
    uint8_t color=(uint8_t)p[GP_COLOR];
    if (p[GP_OP]==GPU_FILL) {
        struct gfx_surface surface;surface.pixels=fb;surface.width=320;surface.height=240;
        gfx_fill_rect(&surface,x0,y0,(int32_t)p[GP_W],(int32_t)p[GP_H],color);
        return;
    }
    if (p[GP_OP]==GPU_LINE) {
        if (x0>x1 || (x0==x1 && y0>y1)) {
            int32_t t=x0; x0=x1;x1=t;t=y0;y0=y1;y1=t;
        }
        int32_t dx=x1-x0,dy=y1-y0,sy=dy<0?-1:1;
        if (dy<0) dy=-dy;
        int32_t err=dx-dy;
        for (;;) {
            pixel(fb,x0,y0,color);
            if(x0==x1 && y0==y1) break;
            int32_t e2=2*err;
            if(e2>=-dy){err-=dy;x0++;}
            if(e2<=dx){err+=dx;y0+=sy;}
        }
    } else if (p[GP_OP]==GPU_TRIANGLE) {
        int32_t area=(x1-x0)*(y2-y0)-(y1-y0)*(x2-x0);
        if (!area) return;
        if(area<0){int32_t t=x1;x1=x2;x2=t;t=y1;y1=y2;y2=t;}
        int32_t left=x0<x1?x0:x1,right=x0>x1?x0:x1,top=y0<y1?y0:y1,bottom=y0>y1?y0:y1;
        if(x2<left)left=x2;
        if(x2>right)right=x2;
        if(y2<top)top=y2;
        if(y2>bottom)bottom=y2;
        if(left<0)left=0;
        if(right>320)right=320;
        if(top<0)top=0;
        if(bottom>240)bottom=240;
        /* Evaluate each center directly, without device state or transfers. */
        for(int32_t y=top;y<bottom;y++) for(int32_t x=left;x<right;x++)
            if(inside(x0,y0,x1,y1,x,y) && inside(x1,y1,x2,y2,x,y) &&
               inside(x2,y2,x0,y0,x,y)) pixel(fb,x,y,color);
    } else {
        int32_t w=(int32_t)p[GP_W],h=(int32_t)p[GP_H];
        int32_t sx=(int32_t)p[GP_SX],sy=(int32_t)p[GP_SY];
        int reverse=p[GP_OP]==GPU_BLIT && p[GP_SRC]==0x30000000u && y0*320+x0>sy*320+sx;
        for(int32_t j=0;j<h;j++) for(int32_t i=0;i<w;i++) {
            int32_t xx=reverse?w-1-i:i,yy=reverse?h-1-j:j;
            int32_t x=x0+xx,y=y0+yy;
            if(x<0 || x>=320 || y<0 || y>=240) continue;
            if(p[GP_OP]==GPU_BLIT) {
                int32_t u=sx+xx,v=sy+yy;
                if(u<0 || v<0 || u>=(int32_t)p[GP_SW] || v>=(int32_t)p[GP_SH]) continue;
                color=source[(uint32_t)v*p[GP_STRIDE]+(uint32_t)u];
            }
            pixel(fb,x,y,color);
        }
    }
}
