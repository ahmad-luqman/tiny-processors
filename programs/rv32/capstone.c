/* The hardware boundary: the exact same runtime is driven by native tests. */
#include "runtime.h"
#include "board.h"
#include "mmio.h"
#include "console.h"
static struct runtime app;
static struct gfx_surface screen = {(uint8_t *)RV32_FB_BASE, RV32_DISPLAY_COLUMNS, RV32_DISPLAY_ROWS};
static uint8_t gpu_expected[320*240];
static int hardware_emit(void *context,const struct gpu_command *c,const uint8_t *source)
{
    (void)context;(void)source;
    return gpu_submit(c) && gpu_wait(2000000);
}
static void draw_demo(void)
{
    app.dirty=0; /* Match runtime_draw after either rendering path. */
    if(!app.demo.accelerated){gpu_demo_draw(&app.demo,&screen);return;}
    (void)gpu_scene(app.demo.frame,gpu_software_emit,gpu_expected);
    if(!gpu_scene(app.demo.frame,hardware_emit,0))rv32_exit(80);
    volatile uint8_t *actual=(volatile uint8_t *)RV32_FB_BASE;
    for(uint32_t i=0;i<sizeof gpu_expected;i++)if(actual[i]!=gpu_expected[i])rv32_exit(81);
    gpu_demo_labels(&app.demo,&screen);
}
int main(void)
{
    runtime_init(&app);
    for (;;) {
        for (uint32_t event = mmio_read32(RV32_INPUT_BASE + RV32_INPUT_EVENT); event;
             event = mmio_read32(RV32_INPUT_BASE + RV32_INPUT_EVENT)) runtime_event(&app, event);
        if (app.quit) {
            rv32_puts("PASS "); rv32_put_hex32(runtime_checksum(&app)); rv32_putc('\n');
            return 0;
        }
        runtime_frame(&app, mmio_read32(RV32_INPUT_BASE + RV32_INPUT_KEYS));
        if(app.screen==RUNTIME_GPU)draw_demo();
        else runtime_draw(&app, &screen);
        mmio_write32(RV32_DISPLAY_BASE + RV32_DISPLAY_PRESENT, 1);
    }
}
