/* Text extensions for M7; the standalone M6 drawing object is preserved. */
#include "gfx.h"

/* Original letter drawings: bit 2 is the left pixel; five rows per glyph.
 * Text supports A-Z, 0-9, >, - and :. Other characters advance one blank cell. */
static const uint8_t LETTERS[26][5] = {
    {2,5,7,5,5}, {6,5,6,5,6}, {3,4,4,4,3}, {6,5,5,5,6}, /* ABCD */
    {7,4,6,4,7}, {7,4,6,4,4}, {3,4,5,5,3}, {5,5,7,5,5}, /* EFGH */
    {7,2,2,2,7}, {1,1,1,5,2}, {5,5,6,5,5}, {4,4,4,4,7}, /* IJKL */
    {5,7,7,5,5}, {5,7,7,7,5}, {2,5,5,5,2}, {6,5,6,4,4}, /* MNOP */
    {2,5,5,3,1}, {6,5,6,5,5}, {3,4,2,1,6}, {7,2,2,2,2}, /* QRST */
    {5,5,5,5,7}, {5,5,5,5,2}, {5,5,7,7,5}, {5,5,2,5,5}, /* UVWX */
    {5,5,2,2,2}, {7,1,2,4,7},                             /* YZ */
};

uint8_t gfx_char_row(uint32_t character, uint32_t row)
{
    if (row >= 5) return 0;
    if (character >= 'A' && character <= 'Z') return LETTERS[character - 'A'][row];
    if (character >= '0' && character <= '9') return gfx_glyph_row(character - '0', row);
    if (character == '>') { static const uint8_t arrow[5] = {4,2,1,2,4}; return arrow[row]; }
    if (character == '-') return row == 2 ? 7 : 0;
    if (character == ':') return row == 1 || row == 3 ? 2 : 0;
    return 0;
}

void gfx_draw_text(const struct gfx_surface *s, int32_t x, int32_t y, const char *text, uint32_t scale, uint8_t color)
{
    for (; *text; text++, x += (int32_t)(4 * scale)) {
        for (uint32_t row = 0; row < 5; row++) {
            uint32_t bits = gfx_char_row((uint8_t)*text, row);
            for (uint32_t col = 0; col < 3; col++) {
                if (bits & (4u >> col))
                    gfx_fill_rect(s, x + (int32_t)(col * scale), y + (int32_t)(row * scale),
                                  (int32_t)scale, (int32_t)scale, color);
            }
        }
    }
}

void gfx_draw_uint(const struct gfx_surface *s, int32_t x, int32_t y, uint32_t value, uint32_t scale, uint8_t color)
{
    static const uint32_t places[10] = {1000000000,100000000,10000000,1000000,100000,10000,1000,100,10,1};
    uint32_t started = 0;
    for (uint32_t i = 0; i < 10; i++) {
        uint32_t digit = 0;
        while (value >= places[i]) { value -= places[i]; digit++; }
        if (digit || started || i == 9) {
            gfx_draw_digit(s, x, y, digit, scale, color);
            x += (int32_t)(4 * scale);
            started = 1;
        }
    }
}
