/* Software drawing on an 8-bit surface; see gfx.h.
 *
 * Written for this project, including the digit glyphs. The routines use
 * only shifts, adds, and compares plus one multiply per call for the first
 * row's address, so they cost the same on the guest (where `*` is a loop in
 * rt/muldiv.c) and on the host. A row is filled a word at a time where its
 * address is aligned: four pixels per store is what makes a full clear
 * affordable in RTL cycles.
 */
#include "gfx.h"

/* The ten digits, one row per byte, three bits per row with bit 2 the left column.
 * Drawn for this project on graph paper; no font file is involved. */
static const uint8_t GLYPHS[10][GFX_GLYPH_ROWS] = {
    {07, 05, 05, 05, 07}, /* 0 */
    {02, 06, 02, 02, 07}, /* 1 */
    {07, 01, 07, 04, 07}, /* 2 */
    {07, 01, 07, 01, 07}, /* 3 */
    {05, 05, 07, 01, 01}, /* 4 */
    {07, 04, 07, 01, 07}, /* 5 */
    {07, 04, 07, 05, 07}, /* 6 */
    {07, 01, 01, 01, 01}, /* 7 */
    {07, 05, 07, 05, 07}, /* 8 */
    {07, 05, 07, 01, 07}, /* 9 */
};

uint8_t gfx_glyph_row(uint32_t digit, uint32_t row)
{
    return (digit < 10 && row < GFX_GLYPH_ROWS) ? GLYPHS[digit][row] : 0;
}

/* Fill `count` pixels from `p`: bytes up to the first word boundary, words, then bytes. */
static void fill_span(uint8_t *p, uint32_t count, uint8_t color)
{
    uint32_t word = (uint32_t)color | ((uint32_t)color << 8) | ((uint32_t)color << 16) | ((uint32_t)color << 24);
    while (count > 0 && ((uintptr_t)p & 3u) != 0) {
        *p++ = color;
        count--;
    }
    while (count >= 4) {
        *(uint32_t *)p = word;
        p += 4;
        count -= 4;
    }
    while (count > 0) {
        *p++ = color;
        count--;
    }
}

void gfx_clear(const struct gfx_surface *s, uint8_t color)
{
    fill_span(s->pixels, s->width * s->height, color);
}

void gfx_fill_rect(const struct gfx_surface *s, int32_t x, int32_t y, int32_t w, int32_t h, uint8_t color)
{
    int32_t x1 = x + w, y1 = y + h;
    if (x < 0) {
        x = 0;
    }
    if (y < 0) {
        y = 0;
    }
    if (x1 > (int32_t)s->width) {
        x1 = (int32_t)s->width;
    }
    if (y1 > (int32_t)s->height) {
        y1 = (int32_t)s->height;
    }
    if (x >= x1 || y >= y1) {
        return;
    }
    uint8_t *row = s->pixels + (uint32_t)y * s->width + (uint32_t)x; /* one multiply per call */
    uint32_t count = (uint32_t)(x1 - x);
    for (int32_t yy = y; yy < y1; yy++) {
        fill_span(row, count, color);
        row += s->width;
    }
}

void gfx_draw_digit(const struct gfx_surface *s, int32_t x, int32_t y, uint32_t digit, uint32_t scale, uint8_t color)
{
    int32_t cell = (int32_t)scale;
    int32_t cy = y;
    for (uint32_t row = 0; row < GFX_GLYPH_ROWS; row++) {
        uint8_t bits = gfx_glyph_row(digit, row);
        int32_t cx = x;
        for (uint32_t column = 0; column < GFX_GLYPH_COLUMNS; column++) {
            if (bits & (4u >> column)) {
                gfx_fill_rect(s, cx, cy, cell, cell, color);
            }
            cx += cell;
        }
        cy += cell;
    }
}

void gfx_draw_number(const struct gfx_surface *s, int32_t x, int32_t y, uint32_t value, uint32_t scale, uint8_t color)
{
    uint32_t tens = 0, ones = value;
    while (ones >= 10) { /* no division: the guest has no divider and this is at most ten steps for a score */
        ones -= 10;
        tens++;
    }
    while (tens >= 10) {
        tens -= 10;
    }
    gfx_draw_digit(s, x, y, tens, scale, color);
    gfx_draw_digit(s, x + (int32_t)(4u * scale), y, ones, scale, color);
}
