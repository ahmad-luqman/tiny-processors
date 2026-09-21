/* Polling application runtime, independent of MMIO for native testing. */
#ifndef RV32_RUNTIME_H
#define RV32_RUNTIME_H
#include "pong_game.h"
#include "tetris_game.h"
enum runtime_screen { RUNTIME_MENU, RUNTIME_PONG, RUNTIME_TETRIS };
struct runtime {
    struct pong pong;
    struct tetris tetris;
    uint32_t screen, selected, quit, blocked, transition, dirty, over_frames, frames;
    uint32_t history; /* checksums folded on screen changes (0 from menu) and R presses */
};
void runtime_init(struct runtime *r);
void runtime_event(struct runtime *r, uint32_t event);
void runtime_frame(struct runtime *r, uint32_t keys);
void runtime_draw(struct runtime *r, const struct gfx_surface *s);
uint32_t runtime_checksum(const struct runtime *r);
#endif
