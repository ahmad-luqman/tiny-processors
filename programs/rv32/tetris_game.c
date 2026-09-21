/* Tetris uses only bounded integer operations; no devices or wall clock. */
#include "tetris_game.h"
#include "board.h"

static const uint16_t masks[7][4] = {
    {0x00f0, 0x4444, 0x0f00, 0x2222},
    {0x0066, 0x0066, 0x0066, 0x0066},
    {0x0072, 0x0262, 0x0270, 0x0232},
    {0x0036, 0x0462, 0x0360, 0x0231},
    {0x0063, 0x0264, 0x0630, 0x0132},
    {0x0071, 0x0226, 0x0470, 0x0322},
    {0x0074, 0x0622, 0x0170, 0x0223},
};
static const uint8_t colors[8] = {0x00, 0x1f, 0xfc, 0xe3, 0x1c, 0xe0, 0x03, 0xf0};

uint32_t tetris_mask(uint32_t piece, uint32_t rotation)
{
    return piece < 7 ? masks[piece][rotation & 3] : 0;
}

uint32_t tetris_fits(const struct tetris *t, int32_t x, int32_t y, uint32_t rotation)
{
    uint32_t mask = tetris_mask(t->piece, rotation);
    for (int32_t row = 0; row < 4; row++) {
        for (int32_t col = 0; col < 4; col++) {
            if (mask & (1u << (row * 4 + col))) {
                int32_t xx = x + col, yy = y + row;
                if (xx < 0 || xx >= TETRIS_W || yy < 0 || yy >= TETRIS_H || t->board[yy][xx]) {
                    return 0;
                }
            }
        }
    }
    return mask != 0;
}

static uint32_t random_word(struct tetris *t)
{
    uint32_t x = t->random;
    x ^= x << 13;
    x ^= x >> 17;
    x ^= x << 5;
    t->random = x;
    return x;
}

static uint32_t take_piece(struct tetris *t)
{
    if (t->bag_pos == 7) {
        for (uint32_t i = 0; i < 7; i++) {
            t->bag[i] = i;
        }
        for (uint32_t i = 6; i > 0; i--) {
            uint32_t j = random_word(t) % (i + 1);
            uint32_t swap = t->bag[i];
            t->bag[i] = t->bag[j];
            t->bag[j] = swap;
        }
        t->bag_pos = 0;
    }
    return t->bag[t->bag_pos++];
}

static void spawn(struct tetris *t)
{
    t->piece = t->next;
    t->next = take_piece(t);
    t->rotation = 0;
    t->x = 3;
    t->y = 0;
    t->gravity = t->repeat = 0;
    t->direction = 0;
    if (!tetris_fits(t, t->x, t->y, 0)) {
        t->phase = TETRIS_OVER;
    }
}

void tetris_init(struct tetris *t, uint32_t seed)
{
    /* Explicit initialization avoids relying on guest libc/memset. */
    for (uint32_t y = 0; y < TETRIS_H; y++) {
        for (uint32_t x = 0; x < TETRIS_W; x++) {
            t->board[y][x] = 0;
            t->drawn[y][x] = 0;
        }
    }
    t->seed = seed ? seed : 1;
    t->random = t->seed;
    t->bag_pos = 7;
    t->score = t->lines = t->frame = 0;
    t->phase = TETRIS_PLAY;
    t->dirty = 1;
    t->drawn_score = t->drawn_lines = t->drawn_next = t->drawn_phase = UINT32_MAX;
    t->next = take_piece(t);
    spawn(t);
}

static uint32_t add_saturated(uint32_t a, uint32_t b)
{
    return UINT32_MAX - a < b ? UINT32_MAX : a + b;
}

static void lock_piece(struct tetris *t)
{
    uint32_t mask = tetris_mask(t->piece, t->rotation);
    for (int32_t y = 0; y < 4; y++) {
        for (int32_t x = 0; x < 4; x++) {
            if (mask & (1u << (y * 4 + x))) {
                t->board[t->y + y][t->x + x] = (uint8_t)(t->piece + 1);
            }
        }
    }
    int32_t dest = TETRIS_H - 1;
    uint32_t cleared = 0;
    for (int32_t src = TETRIS_H - 1; src >= 0; src--) {
        uint32_t full = 1;
        for (uint32_t x = 0; x < TETRIS_W; x++) {
            if (!t->board[src][x]) full = 0;
        }
        if (full) {
            cleared++;
        } else {
            for (uint32_t x = 0; x < TETRIS_W; x++) t->board[dest][x] = t->board[src][x];
            dest--;
        }
    }
    while (dest >= 0) {
        for (uint32_t x = 0; x < TETRIS_W; x++) t->board[dest][x] = 0;
        dest--;
    }
    static const uint32_t points[5] = {0, 100, 300, 500, 800};
    t->score = add_saturated(t->score, points[cleared]);
    t->lines = add_saturated(t->lines, cleared);
    spawn(t);
}

void tetris_event(struct tetris *t, uint32_t event)
{
    if (!(event & RV32_EVENT_VALID) || !(event & RV32_EVENT_PRESS)) return;
    uint32_t key = event & RV32_EVENT_CODE_MASK;
    if (key == RV32_KEY_R) {
        tetris_init(t, t->seed);
        return;
    }
    if (key == RV32_KEY_P && t->phase != TETRIS_OVER) {
        t->phase = t->phase == TETRIS_PLAY ? TETRIS_PAUSED : TETRIS_PLAY;
    } else if (t->phase == TETRIS_PLAY) {
        if (key == RV32_KEY_UP) {
            uint32_t r = (t->rotation + 1) & 3;
            if (tetris_fits(t, t->x, t->y, r)) t->rotation = r;
        } else if (key == RV32_KEY_SPACE) {
            while (tetris_fits(t, t->x, t->y + 1, t->rotation)) t->y++;
            lock_piece(t);
        }
    }
}

void tetris_frame(struct tetris *t, uint32_t keys)
{
    if (t->phase != TETRIS_PLAY) return;
    t->frame++;
    int32_t dir = (int32_t)((keys >> RV32_KEY_RIGHT) & 1) - (int32_t)((keys >> RV32_KEY_LEFT) & 1);
    uint32_t move = 0;
    if (dir != t->direction) {
        t->direction = dir;
        t->repeat = 0;
        move = 1;
    } else if (dir) {
        t->repeat++;
        if (t->repeat == 12) { move = 1; t->repeat = 8; }
    }
    if (move && dir && tetris_fits(t, t->x + dir, t->y, t->rotation)) t->x += dir;
    uint32_t interval = keys & (1u << RV32_KEY_DOWN) ? 3 : 30;
    if (++t->gravity >= interval) {
        t->gravity = 0;
        if (tetris_fits(t, t->x, t->y + 1, t->rotation)) t->y++;
        else lock_piece(t);
    }
}

void tetris_draw(struct tetris *t, const struct gfx_surface *s)
{
    if (t->dirty) {
        gfx_clear(s, 0);
        gfx_fill_rect(s, 9, 19, 102, 202, 0x92);
        gfx_draw_text(s, 130, 20, "TETRIS", 3, 0xff);
        gfx_draw_text(s, 130, 50, "SCORE", 2, 0xff);
        gfx_draw_text(s, 130, 84, "LINES", 2, 0xff);
        gfx_draw_text(s, 130, 118, "NEXT", 2, 0xff);
        gfx_draw_text(s, 130, 180, "ARROWS MOVE", 1, 0xff);
        gfx_draw_text(s, 130, 190, "UP ROTATE SPACE DROP", 1, 0xff);
        gfx_draw_text(s, 130, 200, "P PAUSE R RESTART", 1, 0xff);
        gfx_draw_text(s, 130, 210, "ESC MENU Q QUIT", 1, 0xff);
    }
    uint32_t mask = tetris_mask(t->piece, t->rotation);
    for (int32_t y = 0; y < TETRIS_H; y++) {
        for (int32_t x = 0; x < TETRIS_W; x++) {
            uint8_t cell = t->board[y][x];
            int32_t dx = x - t->x, dy = y - t->y;
            if (t->phase != TETRIS_OVER && dx >= 0 && dx < 4 && dy >= 0 && dy < 4 &&
                (mask & (1u << (dy * 4 + dx)))) cell = (uint8_t)(t->piece + 1);
            if (t->dirty || t->drawn[y][x] != cell) {
                gfx_fill_rect(s, 10 + x * 10, 20 + y * 10, 10, 10, 0);
                if (cell) gfx_fill_rect(s, 10 + x * 10, 20 + y * 10, 9, 9, colors[cell]);
                t->drawn[y][x] = cell;
            }
        }
    }
    if (t->dirty || t->score != t->drawn_score) {
        gfx_fill_rect(s, 130, 64, 180, 10, 0);
        gfx_draw_uint(s, 130, 64, t->score, 2, 0xfc);
    }
    if (t->dirty || t->lines != t->drawn_lines) {
        gfx_fill_rect(s, 130, 98, 180, 10, 0);
        gfx_draw_uint(s, 130, 98, t->lines, 2, 0xfc);
    }
    if (t->dirty || t->next != t->drawn_next) {
        gfx_fill_rect(s, 180, 116, 40, 40, 0);
        uint32_t next_mask = tetris_mask(t->next, 0);
        for (int32_t y = 0; y < 4; y++) {
            for (int32_t x = 0; x < 4; x++) {
                if (next_mask & (1u << (y * 4 + x)))
                    gfx_fill_rect(s, 180 + x * 10, 116 + y * 10, 9, 9, colors[t->next + 1]);
            }
        }
    }
    if (t->dirty || t->phase != t->drawn_phase) {
        gfx_fill_rect(s, 130, 162, 180, 10, 0);
        gfx_draw_text(s, 130, 162, t->phase == TETRIS_PAUSED ? "PAUSED" :
                      t->phase == TETRIS_OVER ? "GAME OVER" : "PLAY", 2, 0xff);
    }
    t->drawn_score = t->score; t->drawn_lines = t->lines;
    t->drawn_next = t->next; t->drawn_phase = t->phase;
    t->dirty = 0;
}

uint32_t tetris_checksum(const struct tetris *t)
{
    uint32_t h = 2166136261u;
#define HASH(v) h = (h ^ (uint32_t)(v)) * 16777619u
    for (uint32_t y = 0; y < TETRIS_H; y++)
        for (uint32_t x = 0; x < TETRIS_W; x++) { HASH(t->board[y][x]); }
    HASH(t->seed); HASH(t->random); HASH(t->bag_pos);
    for (uint32_t i = 0; i < 7; i++) { HASH(t->bag[i]); }
    HASH(t->piece); HASH(t->next); HASH(t->rotation); HASH(t->x); HASH(t->y);
    HASH(t->phase); HASH(t->score); HASH(t->lines); HASH(t->gravity);
    HASH(t->repeat); HASH(t->direction); HASH(t->frame);
#undef HASH
    return h;
}
