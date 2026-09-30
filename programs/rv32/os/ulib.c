/* The user library; see ulib.h. */
#include "ulib.h"

#include "board.h"
#include "console.h"

_Noreturn void sys_exit(uint32_t code)
{
    (void)syscall3(SYS_EXIT, code, 0, 0);
    for (;;) {
    }
}

_Noreturn void rv32_exit(uint32_t code)
{
    sys_exit(code);
}

uint32_t u_strlen(const char *s)
{
    uint32_t n = 0;
    while (s[n]) {
        n++;
    }
    return n;
}

int u_strcmp(const char *a, const char *b)
{
    while (*a && *a == *b) {
        a++;
        b++;
    }
    return (unsigned char)*a - (unsigned char)*b;
}

int u_starts(const char *s, const char *prefix)
{
    while (*prefix) {
        if (*s++ != *prefix++) {
            return 0;
        }
    }
    return 1;
}

void *memcpy(void *dst, const void *src, size_t n)
{
    uint8_t *d = dst;
    const uint8_t *s = src;
    while (n--) {
        *d++ = *s++;
    }
    return dst;
}

void *memset(void *dst, int value, size_t n)
{
    uint8_t *d = dst;
    while (n--) {
        *d++ = (uint8_t)value;
    }
    return dst;
}

void u_puts(const char *s)
{
    (void)sys_write(1, s, u_strlen(s));
}

void u_putdec(uint32_t value)
{
    char text[11];
    int i = 10;
    text[i] = 0;
    do {
        uint32_t q = 0, r = value;
        while (r >= 10) { /* no divide: RV32I programs would call the software routine for every digit */
            r -= 10;
            q++;
        }
        text[--i] = (char)('0' + r);
        value = q;
    } while (value);
    u_puts(text + i);
}

void u_puthex(uint32_t value)
{
    char text[9];
    for (int i = 7; i >= 0; i--) {
        uint32_t digit = value & 15u;
        text[i] = (char)(digit < 10 ? '0' + digit : 'a' + digit - 10);
        value >>= 4;
    }
    text[8] = 0;
    u_puts(text);
}

uint32_t u_parse(const char *s, const char **end)
{
    uint32_t value = 0;
    while (*s >= '0' && *s <= '9') {
        value = value * 10u + (uint32_t)(*s++ - '0');
    }
    if (end) {
        *end = s;
    }
    return value;
}

/* console.h on top of the write call. */
void rv32_putc(char c)
{
    (void)sys_write(1, &c, 1);
}

void rv32_puts(const char *s)
{
    u_puts(s);
}

void rv32_put_hex32(uint32_t value)
{
    u_puthex(value);
}

void rv32_put_udec(uint32_t value)
{
    u_putdec(value);
}
