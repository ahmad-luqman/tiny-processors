/* life: Conway's Game of Life in the right half of the screen (Track 2,
 * O4). A 20 x 30 torus of 8-pixel cells starts from an R-pentomino, a
 * glider and a blinker and runs 16 generations, presenting each. The sum
 * folds the final board, so it is the same however the generations
 * interleave with another program's frames. */
#include "report.h"
#include "ulib.h"

#define W 20
#define H 30
#define GENERATIONS 16
#define LEFT 160
#define STRIDE 320

static uint8_t board[2][H][W];

static void set(uint32_t x, uint32_t y)
{
    board[0][y][x] = 1;
}

int main(void)
{
    uint8_t *pixels = sys_display();
    /* An R-pentomino, a glider and a blinker. */
    set(10, 14); set(11, 14); set(9, 15); set(10, 15); set(10, 16);
    set(2, 2); set(3, 3); set(1, 4); set(2, 4); set(3, 4);
    set(15, 25); set(16, 25); set(17, 25);
    uint32_t now = 0;
    for (uint32_t generation = 0; generation < GENERATIONS; generation++) {
        for (uint32_t y = 0; y < H; y++) {
            /* The torus, without a divide: RV32I would call the software routine for each. */
            uint32_t rows[3] = {y ? y - 1 : H - 1, y, y + 1 < H ? y + 1 : 0};
            for (uint32_t x = 0; x < W; x++) {
                uint32_t columns[3] = {x ? x - 1 : W - 1, x, x + 1 < W ? x + 1 : 0};
                uint32_t neighbours = 0;
                for (uint32_t r = 0; r < 3; r++) {
                    for (uint32_t c = 0; c < 3; c++) {
                        neighbours += board[now][rows[r]][columns[c]];
                    }
                }
                neighbours -= board[now][y][x];
                uint8_t alive = board[now][y][x];
                board[now ^ 1][y][x] = neighbours == 3 || (alive && neighbours == 2);
                if (pixels) {
                    uint32_t word = alive ? 0x1c1c1c1cu : 0x00000000u;
                    for (uint32_t row = 0; row < 8; row++) {
                        uint32_t *at = (uint32_t *)(pixels + (8 * y + row) * STRIDE + LEFT + 8 * x);
                        at[0] = word;
                        at[1] = word;
                    }
                }
            }
        }
        now ^= 1;
        if (pixels) {
            sys_present();
        }
    }
    uint32_t sum = 2166136261u, population = 0;
    for (uint32_t y = 0; y < H; y++) {
        for (uint32_t x = 0; x < W; x++) {
            sum = (sum ^ board[now][y][x]) * 16777619u;
            population += board[now][y][x];
        }
    }
    char what[32] = "16 generations, population ";
    uint32_t n = 27;
    char digits[11];
    uint32_t d = 10;
    digits[d] = 0;
    do {
        digits[--d] = (char)('0' + population % 10u);
        population /= 10u;
    } while (population);
    while (digits[d] && n + 1 < sizeof what) {
        what[n++] = digits[d++];
    }
    what[n] = 0;
    report("life", what, sum);
    return 0;
}
