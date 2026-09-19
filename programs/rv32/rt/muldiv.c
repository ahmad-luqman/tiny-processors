/* Multiply and divide in plain C for a CPU without MUL/DIV/REM.
 *
 * Written for this project; no external code. Only shifts, adds, subtracts,
 * compares, and 32-bit unsigned arithmetic are used, so the compiler cannot
 * turn any of this back into a libcall to itself (see check-rv32-image).
 *
 * The firmware build (any __riscv target) also exports these under the names
 * clang emits for `*`, `/`, and `%` on 32-bit integers. The host build used by
 * tests/test_rv32_rt.py omits those aliases: the host already has compiler-rt
 * symbols with the same names, and Mach-O does not support alias attributes.
 */
#include "muldiv.h"

uint32_t rv32_mul(uint32_t a, uint32_t b)
{
    /* Shift-and-add: the low 32 bits of a product do not depend on whether
     * the operands are read as signed or unsigned, so one routine serves both. */
    uint32_t product = 0;
    while (b != 0) {
        if (b & 1u) {
            product += a;
        }
        a <<= 1;
        b >>= 1;
    }
    return product;
}

/* Restoring division, one quotient bit per step, most significant first.
 * `high` is the bit shifted out of the 32-bit partial remainder; when it is
 * set the remainder is already at least 2^32 > d, so the subtraction must
 * happen even though the truncated compare would say otherwise (d >= 2^31). */
static uint32_t udivmod(uint32_t n, uint32_t d, uint32_t *remainder)
{
    uint32_t quotient = 0;
    uint32_t rem = 0;
    for (int bit = 31; bit >= 0; bit--) {
        uint32_t high = rem >> 31;
        rem = (rem << 1) | ((n >> bit) & 1u);
        if (high || rem >= d) {
            rem -= d;
            quotient |= 1u << bit;
        }
    }
    *remainder = rem;
    return quotient;
}

uint32_t rv32_divu(uint32_t n, uint32_t d)
{
    uint32_t rem;
    if (d == 0) {
        return 0xFFFFFFFFu; /* M extension: DIVU by zero yields all ones. */
    }
    return udivmod(n, d, &rem);
}

uint32_t rv32_remu(uint32_t n, uint32_t d)
{
    uint32_t rem;
    if (d == 0) {
        return n; /* M extension: REMU by zero yields the dividend. */
    }
    udivmod(n, d, &rem);
    return rem;
}

/* Signed division and remainder.
 *
 * TODO(you): implement these two functions. This is the one place in M1 where
 * a numeric contract has to be chosen and it has to match hardware we will
 * build later, so the choice is yours. The tests in tests/test_rv32_rt.py and
 * the firmware self-check pin the RISC-V M-extension answers:
 *
 *   rv32_div(n, 0)             == -1          rv32_rem(n, 0)             == n
 *   rv32_div(INT32_MIN, -1)    == INT32_MIN   rv32_rem(INT32_MIN, -1)    == 0
 *   otherwise: quotient truncates toward zero and n == q * d + r, so the
 *   remainder takes the sign of the dividend:  -7 / 2 == -3,  -7 % 2 == -1.
 *
 * Constraints worth respecting:
 *   - C signed overflow is undefined behavior, so do not negate INT32_MIN as an
 *     int32_t, and do not write `n / d` (that would call this function again).
 *   - Work on unsigned magnitudes with udivmod(), then fix up the signs.
 *     Negating an unsigned value (0u - x) is well defined and wraps.
 *   - The host tests build this file at -O0 and -O2; a UB-dependent answer
 *     will show up as a difference between the two.
 */
int32_t rv32_div(int32_t n, int32_t d)
{
    (void)n;
    (void)d;
    return 0; /* placeholder: replace */
}

int32_t rv32_rem(int32_t n, int32_t d)
{
    (void)n;
    (void)d;
    return 0; /* placeholder: replace */
}

#if defined(__riscv)
/* Names clang uses for 32-bit `*`, `/`, `%` when the target lacks M. */
uint32_t __mulsi3(uint32_t a, uint32_t b) __attribute__((alias("rv32_mul")));
uint32_t __udivsi3(uint32_t n, uint32_t d) __attribute__((alias("rv32_divu")));
uint32_t __umodsi3(uint32_t n, uint32_t d) __attribute__((alias("rv32_remu")));
int32_t __divsi3(int32_t n, int32_t d) __attribute__((alias("rv32_div")));
int32_t __modsi3(int32_t n, int32_t d) __attribute__((alias("rv32_rem")));
#endif
