/* Device-free Tetris rules and rendering. See docs/rv32-runtime.md. */
#ifndef RV32_TETRIS_GAME_H
#define RV32_TETRIS_GAME_H
#include <stdint.h>
#include "gfx.h"
#define TETRIS_W 10
#define TETRIS_H 20
enum tetris_phase { TETRIS_PLAY, TETRIS_PAUSED, TETRIS_OVER };
struct tetris {
    uint8_t board[TETRIS_H][TETRIS_W]; /* 0 empty, piece ID + 1 otherwise */
    uint8_t drawn[TETRIS_H][TETRIS_W];
    uint32_t seed, random, bag[7], bag_pos, piece, next, rotation;
    int32_t x, y, direction;
    uint32_t phase, score, lines, gravity, repeat, frame;
    uint32_t dirty, drawn_score, drawn_lines, drawn_next, drawn_phase;
};
void tetris_init(struct tetris *t, uint32_t seed);
void tetris_event(struct tetris *t, uint32_t event);
void tetris_frame(struct tetris *t, uint32_t keys);
void tetris_draw(struct tetris *t, const struct gfx_surface *s);
uint32_t tetris_checksum(const struct tetris *t);
/* Also used by native directed tests to check table geometry and collisions. */
uint32_t tetris_mask(uint32_t piece, uint32_t rotation);
uint32_t tetris_fits(const struct tetris *t, int32_t x, int32_t y, uint32_t rotation);
#endif
