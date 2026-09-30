/* u_decimal, apart from the rest of the user library so that the host can build and test it
 * (tests/test_rv32_os.py): it makes no system calls. */
#include "udecimal.h"

uint32_t u_decimal(uint32_t value, char digits[11])
{
    uint32_t i = 10;
    digits[i] = 0;
    do {
        /* No divide instruction in RV32I: long division by 10 in binary, one quotient bit per
           dividend bit from the highest set one down, so a digit costs at most 32 steps. */
        uint32_t q = 0, r = 0, bit = 0x80000000u;
        while (bit > value)
            bit >>= 1;
        for (; bit; bit >>= 1) {
            r = r << 1 | ((value & bit) != 0);
            q <<= 1;
            if (r >= 10) {
                r -= 10;
                q |= 1;
            }
        }
        digits[--i] = (char)('0' + r);
        value = q;
    } while (value);
    return i;
}
