/* Pong: the rules and the drawing, with no device in sight (docs/rv32-window.md).
 *
 * The game is a struct and three functions: events change its phase, a
 * frame moves the paddles and the ball from the held-key mask, and a draw
 * updates a surface. The guest glue (pong.c) feeds it the input queue and
 * the framebuffer; the host tests (tests/test_rv32_pong.py) feed it a
 * script and a buffer, so the same rules run in both places. Positions
 * are fixed point with PONG_FP fraction bits; every product stays in 32
 * bits and there is no division and no randomness, so a replay is exact.
 */
#ifndef RV32_PONG_GAME_H
#define RV32_PONG_GAME_H

#include <stdint.h>

#include "gfx.h"

#define PONG_WIDTH 320
#define PONG_HEIGHT 240
#define PONG_FP 8                          /* 1/256 pixel */
#define PONG_ONE (1 << PONG_FP)
#define PONG_PADDLE_W 4
#define PONG_PADDLE_H 24
#define PONG_LEFT_X 8                      /* the left paddle's column; both are word aligned */
#define PONG_RIGHT_X (PONG_WIDTH - PONG_LEFT_X - PONG_PADDLE_W)
#define PONG_BALL 4
#define PONG_BALL_SPEED (4 * PONG_ONE)     /* pixels per frame, horizontal */
#define PONG_SERVE_VY (1 * PONG_ONE)       /* the serve's vertical speed, alternating sign */
#define PONG_PADDLE_SPEED (3 * PONG_ONE)
#define PONG_WIN 9
#define PONG_SCORE_SCALE 4
#define PONG_SCORE_Y 8
#define PONG_SCORE_LEFT_X 116              /* two digits are 7 cells wide: 28 pixels at scale 4 */
#define PONG_SCORE_RIGHT_X 176
#define PONG_NET_X 158                     /* the centre line: 4 wide, dashes 8 on, 8 off */
#define PONG_NET_W 4
#define PONG_NET_DASH 8

enum pong_phase { PONG_SERVE, PONG_PLAY, PONG_PAUSED, PONG_OVER };

/* Colours, as writable data so a later menu could restyle the game; RGB332 values. */
struct pong_theme {
    uint8_t background, foreground, net, score;
};
extern struct pong_theme pong_theme;

struct pong {
    int32_t ball_x, ball_y, ball_vx, ball_vy; /* the ball's top-left corner and velocity, fixed point */
    int32_t left_y, right_y;                  /* each paddle's top edge, fixed point */
    uint32_t score_left, score_right;
    uint32_t phase;                           /* enum pong_phase */
    uint32_t serve_right;                     /* the next serve travels right */
    uint32_t serves;                          /* serves so far; the serve's vertical direction alternates */
    uint32_t frame;                           /* frames simulated */
    uint32_t quit;
    /* What the surface shows, in pixels, so a draw touches only what changed. */
    int32_t drawn_ball_x, drawn_ball_y, drawn_left_y, drawn_right_y;
    uint32_t drawn_score_left, drawn_score_right;
    uint32_t needs_full_redraw;
};

uint32_t pong_state_size(void);
void pong_init(struct pong *p);
/* One popped event word (docs/rv32.md, "Input"): presses of SPACE, P, R, Q, ESCAPE act; releases and other keys do not. */
void pong_event(struct pong *p, uint32_t event);
/* One frame from the held-key mask: W/S move the left paddle, UP/DOWN the right one; the ball moves in play. */
void pong_frame(struct pong *p, uint32_t keys);
/* Update the surface: everything after init or restart, otherwise only the rectangles that changed. */
void pong_draw(struct pong *p, const struct gfx_surface *s);
/* FNV-1a over the scores, positions, and counters: the PASS word of a session. */
uint32_t pong_checksum(const struct pong *p);

/* Read-only views for the host tests, in pixels. */
uint32_t pong_quit(const struct pong *p);
uint32_t pong_phase_of(const struct pong *p);
uint32_t pong_score(const struct pong *p, uint32_t right_side);
int32_t pong_ball_x(const struct pong *p);
int32_t pong_ball_y(const struct pong *p);
int32_t pong_ball_vx(const struct pong *p);
int32_t pong_ball_vy(const struct pong *p);
int32_t pong_left_y(const struct pong *p);
int32_t pong_right_y(const struct pong *p);
void pong_set_ball(struct pong *p, int32_t x, int32_t y, int32_t vx, int32_t vy); /* fixed point; the tests place the ball */

#endif
