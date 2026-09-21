/* Pong's rules and drawing; see pong_game.h.
 *
 * Fixed point: a position is pixels times 256. The ball crosses the field
 * at 4 pixels a frame (about 1.3 s at 60 frames per second); a paddle moves
 * 3. The only products are by constants and in the checksum. Nothing here
 * reads a device or the timer, so the same script gives the same frames on
 * the host, the emulator, and the RTL.
 */
#include "pong_game.h"

#include "board.h"

struct pong_theme pong_theme = {0x00, 0xFF, 0x92, 0xFC}; /* black, white, grey, yellow */

uint32_t pong_state_size(void)
{
    return (uint32_t)sizeof(struct pong);
}

static void centre_ball(struct pong *p)
{
    p->ball_x = ((PONG_WIDTH - PONG_BALL) / 2) * PONG_ONE;
    p->ball_y = ((PONG_HEIGHT - PONG_BALL) / 2) * PONG_ONE;
    p->ball_vx = 0;
    p->ball_vy = 0;
}

void pong_init(struct pong *p)
{
    /* Field by field: a struct assignment would become a memcpy the guest does not have. */
    centre_ball(p);
    p->left_y = ((PONG_HEIGHT - PONG_PADDLE_H) / 2) * PONG_ONE;
    p->right_y = p->left_y;
    p->score_left = 0;
    p->score_right = 0;
    p->phase = PONG_SERVE;
    p->serve_right = 1;
    p->serves = 0;
    p->frame = 0;
    p->quit = 0;
    p->drawn_ball_x = p->drawn_ball_y = p->drawn_left_y = p->drawn_right_y = 0;
    p->drawn_score_left = p->drawn_score_right = 0;
    p->needs_full_redraw = 1;
}

static void serve(struct pong *p)
{
    p->ball_vx = p->serve_right ? PONG_BALL_SPEED : -PONG_BALL_SPEED;
    p->ball_vy = (p->serves & 1u) ? -PONG_SERVE_VY : PONG_SERVE_VY;
    p->serves++;
    p->phase = PONG_PLAY;
}

void pong_event(struct pong *p, uint32_t event)
{
    if (!(event & RV32_EVENT_VALID) || !(event & RV32_EVENT_PRESS)) {
        return;
    }
    switch (event & RV32_EVENT_CODE_MASK) {
    case RV32_KEY_SPACE:
        if (p->phase == PONG_SERVE) {
            serve(p);
        }
        break;
    case RV32_KEY_P:
        if (p->phase == PONG_PLAY) {
            p->phase = PONG_PAUSED;
        } else if (p->phase == PONG_PAUSED) {
            p->phase = PONG_PLAY;
        }
        break;
    case RV32_KEY_R:
        pong_init(p);
        break;
    case RV32_KEY_Q:
    case RV32_KEY_ESCAPE:
        p->quit = 1;
        break;
    default:
        break;
    }
}

static int32_t clamp_paddle(int32_t y)
{
    if (y < 0) {
        return 0;
    }
    if (y > (PONG_HEIGHT - PONG_PADDLE_H) * PONG_ONE) {
        return (PONG_HEIGHT - PONG_PADDLE_H) * PONG_ONE;
    }
    return y;
}

/* The ball met a paddle whose top edge is `paddle_y`: send it back.
 *
 * Bounce rule. Three candidates were considered: (a) mirror vx and keep vy;
 * (b) mirror vx and take vy from where the ball struck, so the edge of a
 * paddle angles the ball and its centre returns it flat; (c) rule b plus a
 * little more speed at every hit. Rule b is what is implemented and what
 * pong.expected pins; changing it means regenerating that file with
 * tools/rv32_pong_native.py --write. The offset is at most 14 pixels, so
 * the vertical speed is at most 3.5 pixels a frame; the shift is an
 * arithmetic shift on a signed value, which both compilers emit as sra.
 */
static void bounce(struct pong *p, int32_t paddle_y)
{
    int32_t ball_centre = p->ball_y + (PONG_BALL / 2) * PONG_ONE;
    int32_t paddle_centre = paddle_y + (PONG_PADDLE_H / 2) * PONG_ONE;
    p->ball_vx = -p->ball_vx;
    p->ball_vy = (ball_centre - paddle_centre) >> 2;
}

static void point(struct pong *p, uint32_t left_scored)
{
    if (left_scored) {
        p->score_left++;
    } else {
        p->score_right++;
    }
    centre_ball(p);
    p->serve_right = left_scored; /* the next serve goes toward the player who lost the point */
    p->phase = (p->score_left == PONG_WIN || p->score_right == PONG_WIN) ? PONG_OVER : PONG_SERVE;
}

void pong_frame(struct pong *p, uint32_t keys)
{
    p->frame++;
    if (p->phase == PONG_PAUSED || p->phase == PONG_OVER) {
        return;
    }
    if (keys & (1u << RV32_KEY_W)) {
        p->left_y = clamp_paddle(p->left_y - PONG_PADDLE_SPEED);
    }
    if (keys & (1u << RV32_KEY_S)) {
        p->left_y = clamp_paddle(p->left_y + PONG_PADDLE_SPEED);
    }
    if (keys & (1u << RV32_KEY_UP)) {
        p->right_y = clamp_paddle(p->right_y - PONG_PADDLE_SPEED);
    }
    if (keys & (1u << RV32_KEY_DOWN)) {
        p->right_y = clamp_paddle(p->right_y + PONG_PADDLE_SPEED);
    }
    if (p->phase != PONG_PLAY) {
        return;
    }
    p->ball_x += p->ball_vx;
    p->ball_y += p->ball_vy;
    if (p->ball_y < 0) { /* the walls reflect */
        p->ball_y = -p->ball_y;
        p->ball_vy = -p->ball_vy;
    } else if (p->ball_y > (PONG_HEIGHT - PONG_BALL) * PONG_ONE) {
        p->ball_y = 2 * (PONG_HEIGHT - PONG_BALL) * PONG_ONE - p->ball_y;
        p->ball_vy = -p->ball_vy;
    }
    /* A paddle returns the ball when the ball's leading edge has reached the paddle's face and
     * the two overlap vertically; the ball is put back on the face so it never sinks in. */
    int32_t ball_top = p->ball_y, ball_bottom = p->ball_y + PONG_BALL * PONG_ONE;
    if (p->ball_vx < 0 && p->ball_x <= (PONG_LEFT_X + PONG_PADDLE_W) * PONG_ONE &&
        p->ball_x + PONG_BALL * PONG_ONE >= PONG_LEFT_X * PONG_ONE &&
        ball_bottom > p->left_y && ball_top < p->left_y + PONG_PADDLE_H * PONG_ONE) {
        p->ball_x = (PONG_LEFT_X + PONG_PADDLE_W) * PONG_ONE;
        bounce(p, p->left_y);
    } else if (p->ball_vx > 0 && p->ball_x + PONG_BALL * PONG_ONE >= PONG_RIGHT_X * PONG_ONE &&
               p->ball_x <= (PONG_RIGHT_X + PONG_PADDLE_W) * PONG_ONE &&
               ball_bottom > p->right_y && ball_top < p->right_y + PONG_PADDLE_H * PONG_ONE) {
        p->ball_x = (PONG_RIGHT_X - PONG_BALL) * PONG_ONE;
        bounce(p, p->right_y);
    }
    if (p->ball_x + PONG_BALL * PONG_ONE < 0) { /* fully past the left edge */
        point(p, 0);
    } else if (p->ball_x > PONG_WIDTH * PONG_ONE) {
        point(p, 1);
    }
}

static int32_t pixels(int32_t fixed)
{
    return fixed >> PONG_FP; /* arithmetic shift: it floors, so a ball leaving the field rounds outward and fill_rect clips it */
}

static uint32_t overlaps(int32_t ax, int32_t ay, int32_t aw, int32_t ah, int32_t bx, int32_t by, int32_t bw, int32_t bh)
{
    return ax < bx + bw && bx < ax + aw && ay < by + bh && by < ay + ah;
}

static void draw_net(const struct gfx_surface *s, int32_t from_y, int32_t to_y)
{
    for (int32_t y = 0; y < PONG_HEIGHT; y += 2 * PONG_NET_DASH) {
        if (y + PONG_NET_DASH > from_y && y < to_y) {
            gfx_fill_rect(s, PONG_NET_X, y, PONG_NET_W, PONG_NET_DASH, pong_theme.net);
        }
    }
}

static void draw_scores(struct pong *p, const struct gfx_surface *s)
{
    int32_t w = 7 * PONG_SCORE_SCALE, h = (int32_t)GFX_GLYPH_ROWS * PONG_SCORE_SCALE;
    gfx_fill_rect(s, PONG_SCORE_LEFT_X, PONG_SCORE_Y, w, h, pong_theme.background);
    gfx_fill_rect(s, PONG_SCORE_RIGHT_X, PONG_SCORE_Y, w, h, pong_theme.background);
    gfx_draw_number(s, PONG_SCORE_LEFT_X, PONG_SCORE_Y, p->score_left, PONG_SCORE_SCALE, pong_theme.score);
    gfx_draw_number(s, PONG_SCORE_RIGHT_X, PONG_SCORE_Y, p->score_right, PONG_SCORE_SCALE, pong_theme.score);
    p->drawn_score_left = p->score_left;
    p->drawn_score_right = p->score_right;
}

void pong_draw(struct pong *p, const struct gfx_surface *s)
{
    int32_t bx = pixels(p->ball_x), by = pixels(p->ball_y), ly = pixels(p->left_y), ry = pixels(p->right_y);
    if (p->needs_full_redraw) {
        gfx_clear(s, pong_theme.background);
        draw_net(s, 0, PONG_HEIGHT);
        draw_scores(p, s);
        p->needs_full_redraw = 0;
    } else {
        /* Erase what moved, then restore the net and the scores where the ball's old place cut them. */
        int32_t obx = p->drawn_ball_x, oby = p->drawn_ball_y;
        if (obx != bx || oby != by) {
            gfx_fill_rect(s, obx, oby, PONG_BALL, PONG_BALL, pong_theme.background);
            if (overlaps(obx, oby, PONG_BALL, PONG_BALL, PONG_NET_X, 0, PONG_NET_W, PONG_HEIGHT)) {
                draw_net(s, oby, oby + PONG_BALL);
            }
            int32_t w = 7 * PONG_SCORE_SCALE, h = (int32_t)GFX_GLYPH_ROWS * PONG_SCORE_SCALE;
            if (overlaps(obx, oby, PONG_BALL, PONG_BALL, PONG_SCORE_LEFT_X, PONG_SCORE_Y, w, h) ||
                overlaps(obx, oby, PONG_BALL, PONG_BALL, PONG_SCORE_RIGHT_X, PONG_SCORE_Y, w, h)) {
                draw_scores(p, s);
            }
        }
        if (p->drawn_left_y != ly) {
            gfx_fill_rect(s, PONG_LEFT_X, p->drawn_left_y, PONG_PADDLE_W, PONG_PADDLE_H, pong_theme.background);
        }
        if (p->drawn_right_y != ry) {
            gfx_fill_rect(s, PONG_RIGHT_X, p->drawn_right_y, PONG_PADDLE_W, PONG_PADDLE_H, pong_theme.background);
        }
        if (p->drawn_score_left != p->score_left || p->drawn_score_right != p->score_right) {
            draw_scores(p, s);
        }
    }
    gfx_fill_rect(s, PONG_LEFT_X, ly, PONG_PADDLE_W, PONG_PADDLE_H, pong_theme.foreground);
    gfx_fill_rect(s, PONG_RIGHT_X, ry, PONG_PADDLE_W, PONG_PADDLE_H, pong_theme.foreground);
    gfx_fill_rect(s, bx, by, PONG_BALL, PONG_BALL, pong_theme.foreground);
    p->drawn_ball_x = bx;
    p->drawn_ball_y = by;
    p->drawn_left_y = ly;
    p->drawn_right_y = ry;
}

uint32_t pong_checksum(const struct pong *p)
{
    uint32_t values[8] = {p->score_left, p->score_right, (uint32_t)p->ball_x, (uint32_t)p->ball_y,
                          (uint32_t)p->left_y, (uint32_t)p->right_y, p->frame, p->serves};
    uint32_t h = 2166136261u;
    for (uint32_t i = 0; i < 8; i++) {
        h = (h ^ values[i]) * 16777619u; /* FNV-1a; the `*` is __mulsi3 on the guest */
    }
    return h;
}

uint32_t pong_quit(const struct pong *p) { return p->quit; }
uint32_t pong_phase_of(const struct pong *p) { return p->phase; }
uint32_t pong_score(const struct pong *p, uint32_t right_side) { return right_side ? p->score_right : p->score_left; }
int32_t pong_ball_x(const struct pong *p) { return pixels(p->ball_x); }
int32_t pong_ball_y(const struct pong *p) { return pixels(p->ball_y); }
int32_t pong_ball_vx(const struct pong *p) { return p->ball_vx; }
int32_t pong_ball_vy(const struct pong *p) { return p->ball_vy; }
int32_t pong_left_y(const struct pong *p) { return pixels(p->left_y); }
int32_t pong_right_y(const struct pong *p) { return pixels(p->right_y); }

void pong_set_ball(struct pong *p, int32_t x, int32_t y, int32_t vx, int32_t vy)
{
    p->ball_x = x;
    p->ball_y = y;
    p->ball_vx = vx;
    p->ball_vy = vy;
    p->phase = PONG_PLAY;
}
