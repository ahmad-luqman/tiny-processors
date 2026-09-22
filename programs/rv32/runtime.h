/* Polling application runtime, independent of MMIO for native testing. */
#ifndef RV32_RUNTIME_H
#define RV32_RUNTIME_H
#include "digit_ui.h"
#include "pong_game.h"
#include "gpu_demo.h"
#include "tetris_game.h"
/* The order of this enum is the order of the menu: runtime_event selects with
 * RUNTIME_PONG + selected, so a new screen must keep the range contiguous.
 * Selection wraps with a mask, which is correct only while RUNTIME_ENTRIES is a
 * power of two; a fifth entry needs a modulo or an explicit compare. */
enum runtime_screen { RUNTIME_MENU, RUNTIME_PONG, RUNTIME_TETRIS, RUNTIME_DIGIT, RUNTIME_GPU };
#define RUNTIME_ENTRIES 4u
struct runtime {
    struct gpu_demo demo;
    struct digit_ui digit;
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
