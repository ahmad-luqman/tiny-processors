/* Pong on the RV32 machine: the glue between the game and the devices.
 *
 * Each iteration pops every queued event, quits with `PASS <checksum>` if
 * the game asked to, reads the held-key mask, simulates one frame, draws
 * what changed, and presents. The present is frame k of iteration k, and
 * the scripted events of frame k arrive with it, so iteration k + 1 pops
 * them: a Q scheduled at frame N ends the run with exactly N checkpoints.
 * tools/rv32_pong_native.py drives the same game the same way on the host,
 * which is where pong.expected comes from. The timer is never read.
 */
#include <stdint.h>

#include "board.h"
#include "console.h"
#include "mmio.h"
#include "pong_game.h"

static struct pong game;
static struct gfx_surface screen = {(uint8_t *)RV32_FB_BASE, RV32_DISPLAY_COLUMNS, RV32_DISPLAY_ROWS};

int main(void)
{
    pong_init(&game);
    for (;;) {
        for (uint32_t event = mmio_read32(RV32_INPUT_BASE + RV32_INPUT_EVENT); event != 0;
             event = mmio_read32(RV32_INPUT_BASE + RV32_INPUT_EVENT)) {
            pong_event(&game, event);
        }
        if (pong_quit(&game)) {
            rv32_puts("PASS ");
            rv32_put_hex32(pong_checksum(&game));
            rv32_putc('\n');
            return 0;
        }
        pong_frame(&game, mmio_read32(RV32_INPUT_BASE + RV32_INPUT_KEYS));
        pong_draw(&game, &screen);
        mmio_write32(RV32_DISPLAY_BASE + RV32_DISPLAY_PRESENT, 1);
    }
}
