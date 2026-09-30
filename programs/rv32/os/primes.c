/* primes: a sieve on memory from sbrk (Track 2, O2). "primes N" counts the
 * primes below N (default 20000, at most 100000) and prints the largest. */
#include "ulib.h"

int main(const char *args)
{
    uint32_t n = *args ? u_parse(args, 0) : 20000u;
    if (n < 3 || n > 100000u) {
        u_puts("primes: N must be 3..100000\n");
        return 1;
    }
    uint8_t *composite = sys_sbrk(n);
    if (composite == (uint8_t *)(uintptr_t)SYS_ERROR) {
        u_puts("primes: out of memory\n");
        return 2;
    }
    memset(composite, 0, n);
    uint32_t count = 0, largest = 0;
    for (uint32_t i = 2; i < n; i++) {
        if (composite[i]) {
            continue;
        }
        count++;
        largest = i;
        for (uint32_t j = i + i; j < n; j += i) {
            composite[j] = 1;
        }
    }
    u_puts("primes: ");
    u_putdec(count);
    u_puts(" below ");
    u_putdec(n);
    u_puts(", largest ");
    u_putdec(largest);
    u_puts("\n");
    return 0;
}
