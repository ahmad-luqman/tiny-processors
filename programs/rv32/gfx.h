/* Software drawing on an 8-bit surface (docs/rv32-window.md).
 *
 * A surface is any array of one-byte pixels, row-major: the guest passes the
 * framebuffer window, the host tests pass a buffer of their own. Nothing here
 * touches a device register or knows what a pixel value looks like on screen.
 */
#ifndef RV32_GFX_H
#define RV32_GFX_H

#include <stdint.h>

struct gfx_surface {
    uint8_t *pixels;
    uint32_t width, height;
};

#define GFX_GLYPH_COLUMNS 3u
#define GFX_GLYPH_ROWS 5u

void gfx_clear(const struct gfx_surface *s, uint8_t color);
/* Fill the rectangle clipped to the surface; an empty or fully outside one draws nothing. */
void gfx_fill_rect(const struct gfx_surface *s, int32_t x, int32_t y, int32_t w, int32_t h, uint8_t color);
/* One decimal digit from the 3x5 glyph table, each cell `scale` pixels square; the background is left alone, and a value above 9 draws nothing. */
void gfx_draw_digit(const struct gfx_surface *s, int32_t x, int32_t y, uint32_t digit, uint32_t scale, uint8_t color);
/* Two digits, 00..99, one cell apart; wider values show their last two digits. */
void gfx_draw_number(const struct gfx_surface *s, int32_t x, int32_t y, uint32_t value, uint32_t scale, uint8_t color);
/* The glyph's row `row` (0 is the top) as three bits, bit 2 the left column. */
uint8_t gfx_glyph_row(uint32_t digit, uint32_t row);

/* Original uppercase 3x5 font; digits reuse the M6 table; unsupported characters are blank. */
uint8_t gfx_char_row(uint32_t character, uint32_t row);
void gfx_draw_text(const struct gfx_surface *s, int32_t x, int32_t y, const char *text, uint32_t scale, uint8_t color);
/* Full unsigned decimal range, using fixed decimal places rather than repeated subtraction of ten. */
void gfx_draw_uint(const struct gfx_surface *s, int32_t x, int32_t y, uint32_t value, uint32_t scale, uint8_t color);
#endif
