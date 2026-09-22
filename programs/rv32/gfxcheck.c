#include "gpu.h"
#include "board.h"
#include "mmio.h"
#include "console.h"
static uint8_t expected[320*240];
static uint8_t sprite[1024]={1};
static int check(struct gpu_command *c,const uint8_t *source)
{
    gpu_reference(expected,c->p[GP_SRC]==RV32_FB_BASE?expected:source,c);
    if(!gpu_submit(c) || !gpu_wait(1000000))return 0;
    volatile uint8_t *fb=(volatile uint8_t *)RV32_FB_BASE;
    for(uint32_t i=0;i<sizeof expected;i++)if(fb[i]!=expected[i])return 0;
    return 1;
}
int main(void)
{
    struct gpu_command c;
    gpu_reset();
    gpu_command_init(&c,GPU_FILL,0x12);c.p[GP_W]=320;c.p[GP_H]=240;
    if(!check(&c,0))return 1;
    c.p[GP_X0]=(uint32_t)-4;c.p[GP_Y0]=238;c.p[GP_W]=330;c.p[GP_H]=10;c.p[GP_COLOR]=0xe0;
    if(!check(&c,0))return 2;
    gpu_command_init(&c,GPU_LINE,0xff);c.p[GP_X0]=(uint32_t)-20;c.p[GP_Y0]=5;c.p[GP_X1]=340;c.p[GP_Y1]=200;
    if(!check(&c,0))return 3;
    gpu_command_init(&c,GPU_TRIANGLE,0x1f);c.p[GP_X0]=20;c.p[GP_Y0]=20;c.p[GP_X1]=70;c.p[GP_Y1]=25;c.p[GP_X2]=30;c.p[GP_Y2]=60;
    if(!check(&c,0))return 4;
    for(uint32_t i=0;i<sizeof sprite;i++)sprite[i]=(uint8_t)(i*13+7);
    gpu_command_init(&c,GPU_BLIT,0);c.p[GP_X0]=270;c.p[GP_Y0]=210;c.p[GP_W]=32;c.p[GP_H]=32;
    c.p[GP_SRC]=(uint32_t)(uintptr_t)sprite;c.p[GP_STRIDE]=32;c.p[GP_SW]=32;c.p[GP_SH]=32;
    if(!check(&c,sprite))return 5;
    c.p[GP_SRC]=RV32_FB_BASE;c.p[GP_SW]=320;c.p[GP_SH]=240;c.p[GP_STRIDE]=320;c.p[GP_SX]=270;c.p[GP_SY]=210;
    c.p[GP_X0]=275;c.p[GP_Y0]=215;
    if(!check(&c,0))return 6;
    mmio_write32(RV32_DISPLAY_BASE,1);
    /* Invalid command: no writes; reset then a valid zero-sized launch. */
    gpu_command_init(&c,9,0);
    if(!gpu_submit(&c) || gpu_wait(100) || mmio_read32(GPU_BASE+GPU_STATUS)!=GPU_FAULT || mmio_read32(GPU_BASE+GPU_WRITES)!=0)return 7;
    gpu_reset();gpu_command_init(&c,GPU_FILL,0);c.p[GP_W]=320;c.p[GP_H]=240;
    if(!gpu_submit(&c) || gpu_submit(&c) || gpu_wait(0) || mmio_read32(GPU_BASE+GPU_STATUS)!=0)return 8;
    c.p[GP_W]=0;
    if(!gpu_submit(&c) || !gpu_wait(100) || mmio_read32(GPU_BASE+GPU_WRITES)!=0)return 9;
    rv32_puts("PASS G1\n");return 0;
}
