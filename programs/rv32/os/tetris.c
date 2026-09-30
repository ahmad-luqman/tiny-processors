/* tetris as a program (Track 2, O2): the M7 game on system calls. Q ends it
 * with `PASS <checksum>`, as Pong does. */
#include "board.h"
#include "tetris_game.h"
#include "ulib.h"

static struct tetris game;

int main(void)
{
    uint8_t *pixels = sys_display();
    if (!pixels) {
        u_puts("tetris: no display\n");
        return 1;
    }
    struct gfx_surface screen = {pixels, RV32_DISPLAY_COLUMNS, RV32_DISPLAY_ROWS};
    tetris_init(&game, 1);
    for (;;) {
        uint32_t quit = 0;
        for (uint32_t event = sys_event(); event; event = sys_event()) {
            if ((event & RV32_EVENT_PRESS) && (event & RV32_EVENT_CODE_MASK) == RV32_KEY_Q) {
                quit = 1;
            } else {
                tetris_event(&game, event);
            }
        }
        if (quit) {
            u_puts("PASS ");
            u_puthex(tetris_checksum(&game));
            u_puts("\n");
            return 0;
        }
        tetris_frame(&game, sys_keys());
        tetris_draw(&game, &screen);
        sys_present();
    }
}
