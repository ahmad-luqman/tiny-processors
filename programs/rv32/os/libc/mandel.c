/* mandel: the Mandelbrot set in hardware floating point (issue #33).
 *
 * A C library program built for the hard-float ABI (rv32imf, ilp32f), so its arithmetic is the F
 * extension's single-precision instructions and its floats travel in f registers. It draws the
 * set into the 320x240 RGB332 framebuffer, colouring each point outside by how many iterations it
 * took to escape, and folds the frame's hash as it goes (the checkpoint hash of docs/rv32.md:
 * h = h*33 ^ word over the little-endian words, from 5381). It needs no frame of its own, so with
 * no display (QEMU) it computes the same image and prints the same hash.
 *
 * Our FPU is a serial baseline (docs/fp32.md): an add or a multiply takes about 560 cycles, so on
 * the RTL the picture costs hundreds of millions of cycles. `mandel [BLOCK]` therefore samples one
 * point per BLOCK x BLOCK pixels (1, 2, 4 or 8; 2 unless given) and draws it as a block.
 */
#include <stdint.h>
#include <stdio.h>

#include "ulib.h"

#define WIDTH 320
#define HEIGHT 240
#define MAX_ITERATIONS 64
#define EARLY 8 /* iterations before the cardioid and bulb test: most points have escaped by then */

/* The view: x from -2.2 to 0.8, y from -1.125 to 1.125, square pixels 3/320 wide. */
static const float left = -2.2f, top = -1.125f, pixel_size = 3.0f / WIDTH;

/* RGB332 colours by escape time, dark blue through white to orange; the set itself is black. */
static const uint8_t palette[16] = {
    0x01, 0x02, 0x03, 0x0b, 0x13, 0x1b, 0x3f, 0x7f,
    0xbf, 0xff, 0xfe, 0xfc, 0xf8, 0xf4, 0xf0, 0xe0,
};

static uint32_t iterations; /* in all, for the report */

/* Whether (cr, ci) lies in the main cardioid or the period-2 bulb, both inside the set. */
static int inside_bulbs(float cr, float ci2)
{
    float xq = cr - 0.25f, q = xq * xq + ci2;
    return q * (q + xq) <= 0.25f * ci2 || (cr + 1.0f) * (cr + 1.0f) + ci2 <= 0.0625f;
}

/* The number of iterations before (cr, ci) escapes |z| > 2, or MAX_ITERATIONS if it does not. */
static uint32_t escape_time(float cr, float ci, float ci2)
{
    float zr = 0.0f, zi = 0.0f;
    uint32_t n = 0;
    while (n < MAX_ITERATIONS) {
        if (n == EARLY && inside_bulbs(cr, ci2)) {
            n = MAX_ITERATIONS;
            break;
        }
        float zr2 = zr * zr, zi2 = zi * zi;
        if (zr2 + zi2 > 4.0f) {
            break;
        }
        zi = 2.0f * zr * zi + ci;
        zr = zr2 - zi2 + cr;
        n++;
        iterations++;
    }
    return n;
}

static uint8_t colour(uint32_t n)
{
    return n == MAX_ITERATIONS ? 0 : palette[n % 16u];
}

int main(int argc, char **argv)
{
    uint32_t block = 2;
    if (argc > 1) {
        block = argv[1][0] && !argv[1][1] ? (uint32_t)(argv[1][0] - '0') : 0; /* one digit */
    }
    if (argc > 2 || (block != 1 && block != 2 && block != 4 && block != 8)) {
        printf("usage: mandel [1|2|4|8]\n");
        return 2;
    }
    uint8_t *frame = sys_display();
    uint32_t hash = 5381;
    uint8_t row[WIDTH];
    float step = pixel_size * (float)block;
    for (uint32_t y = 0; y < HEIGHT; y += block) {
        float ci = top + (float)(y / block) * step, ci2 = ci * ci;
        for (uint32_t x = 0; x < WIDTH; x += block) {
            uint8_t c = colour(escape_time(left + (float)(x / block) * step, ci, ci2));
            for (uint32_t k = 0; k < block; k++) {
                row[x + k] = c;
            }
        }
        for (uint32_t copy = 0; copy < block; copy++) {
            for (uint32_t x = 0; x < WIDTH; x += 4) {
                uint32_t word = (uint32_t)row[x] | (uint32_t)row[x + 1] << 8 | (uint32_t)row[x + 2] << 16 |
                                (uint32_t)row[x + 3] << 24;
                hash = ((hash << 5) + hash) ^ word;
            }
            if (frame) {
                for (uint32_t x = 0; x < WIDTH; x++) {
                    frame[(y + copy) * WIDTH + x] = row[x];
                }
            }
        }
    }
    sys_present();
    printf("mandel: %u by %u blocks, %u iterations, frame %08x\n", (unsigned)block, (unsigned)block,
           (unsigned)iterations, (unsigned)hash);
    return 0;
}
