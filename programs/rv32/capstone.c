/* The hardware boundary: the exact same runtime is driven by native tests. */
#include "runtime.h"
#include "board.h"
#include "mmio.h"
#include "console.h"
static struct runtime app;
static struct gfx_surface screen = {(uint8_t *)RV32_FB_BASE, RV32_DISPLAY_COLUMNS, RV32_DISPLAY_ROWS};
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
        runtime_draw(&app, &screen);
        mmio_write32(RV32_DISPLAY_BASE + RV32_DISPLAY_PRESENT, 1);
    }
}
