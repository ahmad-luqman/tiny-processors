/* bars: an animation in the left half of the screen (Track 2, O4).
 * Twenty-four frames of coloured bands scrolling at different speeds; every colour comes
 * from a little pseudo-random arithmetic, so each frame is some work between
 * presents and the timer has something to preempt. The sum folds every
 * colour drawn, so it is the same however the frames interleave with
 * another program's. */
#include "report.h"
#include "ulib.h"

#define FRAMES 24
#define WIDTH 160
#define ROWS 240
#define STRIDE 320

int main(void)
{
    uint8_t *pixels = sys_display();
    uint32_t sum = 2166136261u;
    for (uint32_t frame = 0; frame < FRAMES; frame++) {
        for (uint32_t y = 0; y < ROWS; y++) {
            uint32_t seed = ((y >> 4) + (frame << ((y >> 6) & 3))) | 0x100u;
            for (uint32_t k = 0; k < 8; k++) { /* a little work per row: xorshift, no multiply */
                seed ^= seed << 13;
                seed ^= seed >> 17;
                seed ^= seed << 5;
            }
            uint32_t colour = (seed >> 24) | 0x03u;
            sum = (sum ^ colour) * 16777619u;
            if (pixels) {
                uint32_t word = colour * 0x01010101u;
                uint32_t *row = (uint32_t *)(pixels + y * STRIDE);
                for (uint32_t x = 0; x < WIDTH / 4; x++) {
                    row[x] = word;
                }
            }
        }
        if (pixels) {
            sys_present();
        }
    }
    return report("bars", "24 frames", sum);
}
