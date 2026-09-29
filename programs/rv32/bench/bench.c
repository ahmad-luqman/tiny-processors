/* bench: counters, printf and string routines for the benchmark ports; see bench.h. */
#include "bench.h"
#include "../console.h"

/* A 64-bit counter read as two halves: read high, low, high again, and retry if the high
 * half moved, so a carry between the two reads cannot tear the value. */
#define READ_COUNTER64(low, high)                                             \
    do {                                                                      \
        uint32_t hi, lo, again;                                               \
        do {                                                                  \
            __asm__ volatile("csrr %0, " #high : "=r"(hi));                   \
            __asm__ volatile("csrr %0, " #low : "=r"(lo));                    \
            __asm__ volatile("csrr %0, " #high : "=r"(again));                \
        } while (hi != again);                                                \
        return ((uint64_t)hi << 32) | lo;                                     \
    } while (0)

uint64_t bench_cycles(void) { READ_COUNTER64(cycle, cycleh); }
uint64_t bench_instret(void) { READ_COUNTER64(instret, instreth); }

bench_mark bench_start(void)
{
    bench_mark mark;
    mark.instret = bench_instret();
    mark.cycles = bench_cycles();
    return mark;
}

void bench_stop(const char *name, bench_mark start)
{
    uint64_t cycles = bench_cycles() - start.cycles;
    uint64_t instret = bench_instret() - start.instret;
    bench_printf("bench: %s cycles=%llu instret=%llu\n", name, (unsigned long long)cycles,
                 (unsigned long long)instret);
}

/* Unsigned decimal or hex digits of a 64-bit value, least significant last. Decimal digits
 * come from subtracting powers of ten, so no 64-bit division (a libcall we do not have)
 * appears. Returns the digit count. */
static int digits(uint64_t value, int hex, int upper, char *out)
{
    static const uint64_t powers[] = {
        10000000000000000000ull, 1000000000000000000ull, 100000000000000000ull, 10000000000000000ull,
        1000000000000000ull, 100000000000000ull, 10000000000000ull, 1000000000000ull, 100000000000ull,
        10000000000ull, 1000000000ull, 100000000ull, 10000000ull, 1000000ull, 100000ull, 10000ull,
        1000ull, 100ull, 10ull, 1ull};
    int n = 0;
    if (hex) {
        for (int shift = 60; shift >= 0; shift -= 4) {
            unsigned nibble = (unsigned)(value >> shift) & 15u;
            if (nibble || n || shift == 0) {
                out[n++] = (char)(nibble < 10 ? '0' + nibble : (upper ? 'A' : 'a') + nibble - 10);
            }
        }
        return n;
    }
    for (unsigned i = 0; i < sizeof powers / sizeof powers[0]; i++) {
        char digit = '0';
        while (value >= powers[i]) {
            value -= powers[i];
            digit++;
        }
        if (digit != '0' || n || powers[i] == 1) {
            out[n++] = digit;
        }
    }
    return n;
}

int bench_vprintf(const char *format, va_list args)
{
    int written = 0;
    for (const char *p = format; *p; p++) {
        if (*p != '%') {
            rv32_putc(*p);
            written++;
            continue;
        }
        p++;
        char pad = ' ';
        int width = 0, longs = 0;
        if (*p == '0') {
            pad = '0';
            p++;
        }
        while (*p >= '0' && *p <= '9') {
            width = width * 10 + (*p++ - '0');
        }
        while (*p == 'l') {
            longs++;
            p++;
        }
        char buffer[24];
        const char *text = buffer;
        int length = 0, negative = 0;
        switch (*p) {
        case 'd': case 'i': {
            int64_t value = longs >= 2 ? va_arg(args, long long) : longs ? va_arg(args, long) : va_arg(args, int);
            negative = value < 0;
            length = digits(negative ? (uint64_t)0 - (uint64_t)value : (uint64_t)value, 0, 0, buffer);
            break;
        }
        case 'u': case 'x': case 'X': {
            uint64_t value = longs >= 2 ? va_arg(args, unsigned long long)
                           : longs ? va_arg(args, unsigned long) : va_arg(args, unsigned);
            length = digits(value, *p != 'u', *p == 'X', buffer);
            break;
        }
        case 'c':
            buffer[0] = (char)va_arg(args, int);
            length = 1;
            break;
        case 's':
            text = va_arg(args, const char *);
            length = (int)strlen(text);
            break;
        case '%':
            buffer[0] = '%';
            length = 1;
            break;
        default: /* an unsupported conversion prints as itself, visibly */
            rv32_putc('%');
            rv32_putc(*p);
            written += 2;
            if (!*p) {
                return written;
            }
            continue;
        }
        if (negative && pad == '0') {
            rv32_putc('-');
            written++;
        }
        for (int i = length + negative; i < width; i++) {
            rv32_putc(pad);
            written++;
        }
        if (negative && pad == ' ') {
            rv32_putc('-');
            written++;
        }
        for (int i = 0; i < length; i++) {
            rv32_putc(text[i]);
        }
        written += length;
    }
    return written;
}

int bench_printf(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    int written = bench_vprintf(format, args);
    va_end(args);
    return written;
}

/* Byte loops: simple and obviously correct. They are part of what the benchmarks measure
 * (Dhrystone copies strings every run), so their cost is in the recorded figures. */
void *memcpy(void *destination, const void *source, size_t n)
{
    unsigned char *d = destination;
    const unsigned char *s = source;
    while (n--) {
        *d++ = *s++;
    }
    return destination;
}

void *memset(void *destination, int value, size_t n)
{
    unsigned char *d = destination;
    while (n--) {
        *d++ = (unsigned char)value;
    }
    return destination;
}

char *strcpy(char *destination, const char *source)
{
    char *d = destination;
    while ((*d++ = *source++)) {
    }
    return destination;
}

int strcmp(const char *a, const char *b)
{
    while (*a && *a == *b) {
        a++;
        b++;
    }
    return (unsigned char)*a - (unsigned char)*b;
}

size_t strlen(const char *s)
{
    size_t n = 0;
    while (s[n]) {
        n++;
    }
    return n;
}
