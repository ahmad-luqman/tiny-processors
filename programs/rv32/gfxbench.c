#include "gpu.h"
#include "board.h"
#include "mmio.h"
#include "console.h"
static uint8_t source[32*32]={1};
static volatile uint32_t seed=7;
static void number(uint32_t v){rv32_putc(' ');rv32_put_udec(v);}
int main(void)
{
    struct gpu_command c;
    for(uint32_t i=0;i<sizeof source;i++)source[i]=(uint8_t)(i*13+seed);
    for(uint32_t op=1;op<=4;op++) {
        gpu_command_init(&c,op,op==1?0x12:0xfc);
        c.p[GP_X0]=op==1?0:20;c.p[GP_Y0]=op==1?0:20;
        c.p[GP_W]=op==1?320:32;c.p[GP_H]=op==1?240:32;
        c.p[GP_X1]=70;c.p[GP_Y1]=25;c.p[GP_X2]=30;c.p[GP_Y2]=60;
        c.p[GP_SRC]=(uint32_t)(uintptr_t)source;c.p[GP_SW]=32;c.p[GP_SH]=32;c.p[GP_STRIDE]=32;
        uint32_t start=mmio_read32(RV32_TIMER_BASE);
#ifdef G1_CPU
        gpu_reference((uint8_t *)RV32_FB_BASE,source,&c);
#else
        if(!gpu_submit(&c) || !gpu_wait(1000000))return 1;
#endif
        uint32_t elapsed=mmio_read32(RV32_TIMER_BASE)-start;
        rv32_puts("BENCH");number(op);number(elapsed);
#ifndef G1_CPU
        number(mmio_read32(GPU_BASE+GPU_CYCLES));number(mmio_read32(GPU_BASE+GPU_STALLS));
        number(mmio_read32(GPU_BASE+GPU_READS));number(mmio_read32(GPU_BASE+GPU_WRITES));
#endif
        rv32_putc('\n');mmio_write32(RV32_DISPLAY_BASE,1);
    }
    rv32_puts("PASS BENCH\n");return 0;
}
