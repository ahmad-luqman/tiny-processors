/* Deterministic device-free scene; the firmware boundary supplies a hardware emitter. */
#include "gpu_demo.h"
#include "board.h"
static uint8_t sprite[16*16];
void gpu_demo_init(struct gpu_demo *d){d->frame=0;d->accelerated=1;d->paused=0;}
void gpu_demo_event(struct gpu_demo *d,uint32_t code)
{
    if(code==RV32_KEY_SPACE)d->accelerated^=1;
    if(code==RV32_KEY_P)d->paused^=1;
    if(code==RV32_KEY_R)gpu_demo_init(d);
}
int gpu_software_emit(void *context,const struct gpu_command *c,const uint8_t *source)
{
    uint8_t *fb=context;
    gpu_reference(fb,c->p[GP_SRC]==RV32_FB_BASE?fb:source,c);return 1;
}
int gpu_scene(uint32_t frame,gpu_emit emit,void *context)
{
    struct gpu_command c;
    int32_t t=(int32_t)(frame&63u);
    gpu_command_init(&c,GPU_FILL,0x01);c.p[GP_W]=320;c.p[GP_H]=240;
    if(!emit(context,&c,0))return 0;
    gpu_command_init(&c,GPU_FILL,0x12);c.p[GP_X0]=0;c.p[GP_Y0]=35;c.p[GP_W]=320;c.p[GP_H]=164;
    if(!emit(context,&c,0))return 0;
    gpu_command_init(&c,GPU_FILL,0xe0);c.p[GP_X0]=(uint32_t)(t-20);c.p[GP_Y0]=60;c.p[GP_W]=60;c.p[GP_H]=35;
    if(!emit(context,&c,0))return 0;
    gpu_command_init(&c,GPU_TRIANGLE,0xfc);c.p[GP_X0]=110;c.p[GP_Y0]=55;c.p[GP_X1]=175;c.p[GP_Y1]=130;c.p[GP_X2]=85;c.p[GP_Y2]=140;
    if(!emit(context,&c,0))return 0;
    gpu_command_init(&c,GPU_TRIANGLE,0x1f);c.p[GP_X0]=175;c.p[GP_Y0]=130;c.p[GP_X1]=110;c.p[GP_Y1]=55;c.p[GP_X2]=205;c.p[GP_Y2]=65;
    if(!emit(context,&c,0))return 0;
    gpu_command_init(&c,GPU_LINE,0xff);c.p[GP_X0]=(uint32_t)-10;c.p[GP_Y0]=180;c.p[GP_X1]=330;c.p[GP_Y1]=(uint32_t)(90+t);
    if(!emit(context,&c,0))return 0;
    for(uint32_t y=0;y<16;y++)for(uint32_t x=0;x<16;x++)sprite[y*16+x]=(x==y || x+y==15)?0xff:0xe3;
    gpu_command_init(&c,GPU_BLIT,0);c.p[GP_X0]=(uint32_t)(240+t);c.p[GP_Y0]=80;c.p[GP_W]=16;c.p[GP_H]=16;
    c.p[GP_SRC]=(uint32_t)(uintptr_t)sprite;c.p[GP_STRIDE]=16;c.p[GP_SW]=16;c.p[GP_SH]=16;
    if(!emit(context,&c,sprite))return 0;
    gpu_command_init(&c,GPU_BLIT,0);c.p[GP_X0]=112;c.p[GP_Y0]=155;c.p[GP_W]=75;c.p[GP_H]=30;
    c.p[GP_SRC]=RV32_FB_BASE;c.p[GP_STRIDE]=320;c.p[GP_SW]=320;c.p[GP_SH]=240;c.p[GP_SX]=100;c.p[GP_SY]=145;
    return emit(context,&c,0);
}
void gpu_demo_labels(const struct gpu_demo *d,const struct gfx_surface *s)
{
    gfx_draw_text(s,12,12,d->accelerated?"2D ACCELERATOR":"2D CPU DRAWING",3,0xff);
    gfx_draw_text(s,12,207,"SPACE CPU GPU  P PAUSE  R RESTART",1,0xff);
    gfx_draw_text(s,12,222,"ESC MENU  Q QUIT",1,0x92);
    if(d->paused)gfx_draw_text(s,245,12,"PAUSED",2,0xfc);
}
void gpu_demo_draw(const struct gpu_demo *d,const struct gfx_surface *s)
{
    (void)gpu_scene(d->frame,gpu_software_emit,s->pixels);
    gpu_demo_labels(d,s);
}
