/* M1 firmware self-check.
 *
 * Exercises what a linked C image depends on: initialized and zeroed
 * globals, the stack, calls and recursion, signed/unsigned arithmetic, the
 * multiply/divide helpers, sub-word loads and stores, and byte order. Every
 * observed value is compared with a hand-computed constant and folded into a
 * checksum. The run ends with exactly one console line:
 *
 *   PASS <8 hex digits>   and a pass write to the done register, or
 *   FAIL <n>              and a fail write carrying n as the exit code.
 *
 * SELFCHECK_EXPECTED is the FNV-1a fold of the expected values in order; it
 * is recomputed independently by tests/test_rv32_tools.py.
 */
#include <stdint.h>

#include "board.h"
#include "console.h"
#include "rt/muldiv.h"

#define SELFCHECK_EXPECTED 0x807d9fadu /* FNV-1a fold of the 28 expected values */

uint32_t g_init = 0x12345678u;                  /* .data */
uint32_t g_zero[8];                             /* .bss */
volatile int8_t g_s8 = -5;                      /* lb  */
volatile uint8_t g_u8 = 0xFB;                   /* lbu */
volatile int16_t g_s16 = -1234;                 /* lh  */
volatile uint16_t g_u16 = 0xBEEF;               /* lhu */

static uint32_t checksum = 2166136261u;

/* Hide a constant from the optimizer so the instruction sequence really runs. */
static uint32_t opaque(uint32_t x)
{
    __asm__ volatile("" : "+r"(x));
    return x;
}

static void fold(uint32_t value)
{
    checksum = (checksum ^ value) * 16777619u;  /* the `*` calls __mulsi3 */
}

static int failed(uint32_t number)
{
    rv32_puts("FAIL ");
    rv32_put_udec(number);
    rv32_putc('\n');
    return (int)number;
}

#define CHECK(number, observed, expected)                         \
    do {                                                          \
        uint32_t observed_ = (uint32_t)(observed);                \
        if (observed_ != (uint32_t)(expected)) {                  \
            return failed(number);                                \
        }                                                         \
        fold(observed_);                                          \
    } while (0)

static uint32_t fib(uint32_t n)
{
    return n < 2 ? n : fib(n - 1) + fib(n - 2);
}

static uint32_t gcd(uint32_t a, uint32_t b)
{
    while (b != 0) {
        uint32_t t = a % b;                     /* calls __umodsi3 */
        a = b;
        b = t;
    }
    return a;
}

/* Taking the array's address in another function keeps it on the stack. */
static __attribute__((noinline)) void fill(uint8_t *buffer, uint32_t count, uint32_t step)
{
    for (uint32_t i = 0; i < count; i++) {
        buffer[i] = (uint8_t)(i * step);
    }
}

int main(void)
{
    /* 1. Initialized and zero-initialized globals. */
    CHECK(1, *(volatile uint32_t *)&g_init, 0x12345678u);
    uint32_t sum = 0;
    for (uint32_t i = 0; i < 8; i++) {
        sum += g_zero[i];
    }
    CHECK(2, sum, 0);
    g_zero[3] = opaque(77);
    CHECK(3, g_zero[3] + g_zero[4], 77);

    /* 2. Stack array written through a pointer in another function. */
    uint8_t buffer[16];
    fill(buffer, 16, opaque(3));
    sum = 0;
    for (uint32_t i = 0; i < 16; i++) {
        sum += buffer[i];
    }
    CHECK(4, sum, 360);

    /* 3. Recursion and a loop with remainder. */
    CHECK(5, fib(opaque(15)), 610);
    CHECK(6, gcd(opaque(1071), opaque(462)), 21);

    /* 4. Signed versus unsigned. */
    uint32_t top = opaque(0x80000000u);
    CHECK(7, (uint32_t)((int32_t)top >> 4), 0xF8000000u);
    CHECK(8, top >> 4, 0x08000000u);
    CHECK(9, (int32_t)top < 0 ? 1u : 0u, 1);
    CHECK(10, (uint32_t)((int32_t)opaque(0xFFFFFFFBu) % (int32_t)opaque(3)), 0xFFFFFFFEu); /* -5 % 3 */
    CHECK(11, opaque(0xFFFFFFFFu) / opaque(2), 0x7FFFFFFFu);

    /* 5. Multiply and divide through the compiler's libcalls and directly. */
    CHECK(12, opaque(123456) * opaque(789), 97406784u);
    CHECK(13, opaque(0xFFFFFFFFu) * opaque(3), 0xFFFFFFFDu);
    CHECK(14, (uint32_t)((int32_t)opaque(0xFFFFFFF9u) / (int32_t)opaque(2)), 0xFFFFFFFDu); /* -7 / 2 */
    CHECK(15, (uint32_t)((int32_t)opaque(0xFFFFFFF9u) % (int32_t)opaque(2)), 0xFFFFFFFFu); /* -7 % 2 */
    CHECK(16, (uint32_t)((int32_t)opaque(7) / (int32_t)opaque(0xFFFFFFFEu)), 0xFFFFFFFDu);  /*  7 / -2 */
    CHECK(17, (uint32_t)rv32_div(INT32_MIN, -1), 0x80000000u);
    CHECK(18, (uint32_t)rv32_div((int32_t)opaque(42), 0), 0xFFFFFFFFu);
    CHECK(19, (uint32_t)rv32_rem((int32_t)opaque(42), 0), 42);
    CHECK(20, rv32_divu(opaque(42), 0), 0xFFFFFFFFu);
    CHECK(21, rv32_remu(opaque(42), 0), 42);
    CHECK(22, (uint32_t)rv32_rem(INT32_MIN, -1), 0);

    /* 6. Sub-word loads with and without sign extension, and byte order. */
    CHECK(23, (uint32_t)(int32_t)g_s8, 0xFFFFFFFBu);
    CHECK(24, g_u8, 0xFBu);
    CHECK(25, (uint32_t)(int32_t)g_s16, 0xFFFFFB2Eu);
    CHECK(26, g_u16, 0xBEEFu);
    uint32_t word = opaque(0x11223344u);
    CHECK(27, *(volatile uint8_t *)&word, 0x44u);
    volatile uint16_t half = 0x8001u;
    CHECK(28, (uint32_t)(int32_t)(int16_t)half, 0xFFFF8001u);

    if (checksum != SELFCHECK_EXPECTED) {
        return failed(99);
    }
    rv32_puts("PASS ");
    rv32_put_hex32(checksum);
    rv32_putc('\n');
    return 0;
}
