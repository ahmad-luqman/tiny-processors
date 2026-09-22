/* The hardware boundary: the exact same runtime is driven by native tests. */
#include "runtime.h"
#include "board.h"
#include "mmio.h"
#include "console.h"
#include "digit_hw.h"
static struct runtime app;
static struct gfx_surface screen = {(uint8_t *)RV32_FB_BASE, RV32_DISPLAY_COLUMNS, RV32_DISPLAY_ROWS};
static uint8_t gpu_expected[320*240];
static int hardware_emit(void *context,const struct gpu_command *c,const uint8_t *source)
{
    (void)context;(void)source;
    return gpu_run(c,2000000);
}
static void draw_demo(void)
{
    app.dirty=0; /* Match runtime_draw after either rendering path. */
    if(!app.demo.accelerated){gpu_demo_draw(&app.demo,&screen);return;}
    (void)gpu_scene(app.demo.frame,gpu_software_emit,gpu_expected);
    if(!gpu_scene(app.demo.frame,hardware_emit,0))rv32_exit(80);
    volatile uint8_t *actual=(volatile uint8_t *)RV32_FB_BASE;
    for(uint32_t i=0;i<sizeof gpu_expected;i++)if(actual[i]!=gpu_expected[i]){rv32_puts("G1 pixel mismatch at ");rv32_put_hex32(i);rv32_putc('\n');rv32_exit(81);}
    gpu_demo_labels(&app.demo,&screen);
}
/* The guest classifies twice and compares: the software model is the reference
 * the accelerator has to reproduce exactly, and a difference stops the run
 * instead of being drawn. The host only presents the pixels this produces. */
static int32_t software_logits[DIGIT_CLASSES];
static uint32_t classify(const uint8_t canvas[DIGIT_PIXELS], int32_t logits[DIGIT_CLASSES])
{
    uint8_t x[DIGIT_INPUTS];
    digit_prepare(canvas, x);
    digit_infer_cpu(x, software_logits);
    enum digit_status status = digit_hw_infer(x, logits);
    if (status != DIGIT_OK) {
        rv32_puts("N1 device status ");
        rv32_put_udec((uint32_t)status);
        rv32_putc('\n');
        return (uint32_t)status;
    }
    for (uint32_t c = 0; c < DIGIT_CLASSES; c++) {
        if (logits[c] == software_logits[c]) continue;
        rv32_puts("N1 logit mismatch at ");
        rv32_put_udec(c);
        rv32_putc('\n');
        rv32_exit(82);
    }
    uint32_t margin;
    uint32_t predicted = digit_argmax(logits, &margin);
    rv32_puts("digit ");
    rv32_put_udec(predicted);
    rv32_puts(" margin ");
    rv32_put_udec(margin);
    rv32_putc('\n');
    return 0;
}

int main(void)
{
    runtime_init(&app);
    digit_ui_attach(&app.digit, classify);
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
