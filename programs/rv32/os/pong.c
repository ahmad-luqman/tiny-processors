/* pong as a program (Track 2, O2): programs/rv32/pong.c on system calls.
 * The loop is the standalone image's, event for event and present for
 * present, so a session gives the same checkpoints and PASS word. */
#include "pong_game.h"
#include "ulib.h"

static struct pong game;

int main(void)
{
    uint8_t *pixels = sys_display();
    if (!pixels) {
        u_puts("pong: no display\n");
        return 1;
    }
    struct gfx_surface screen = {pixels, PONG_WIDTH, PONG_HEIGHT};
    pong_init(&game);
    for (;;) {
        for (uint32_t event = sys_event(); event; event = sys_event()) {
            pong_event(&game, event);
        }
        if (pong_quit(&game)) {
            u_puts("PASS ");
            u_puthex(pong_checksum(&game));
            u_puts("\n");
            return 0;
        }
        pong_frame(&game, sys_keys());
        pong_draw(&game, &screen);
        sys_present();
    }
}
