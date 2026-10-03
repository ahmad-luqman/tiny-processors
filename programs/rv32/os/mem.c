/* memcpy and memset for the kernel and the user library's programs (Track 2): the compiler may
 * call them for structure copies even with -fno-builtin, and neither links a C library (Track 3's
 * programs get picolibc's). A word at a time where the addresses allow, since Track 3: spawn
 * copies Lua's 370 KB image with memcpy. */
#include <stddef.h>
#include <stdint.h>

void *memcpy(void *dst, const void *src, size_t n);
void *memset(void *dst, int value, size_t n);

void *memcpy(void *dst, const void *src, size_t n)
{
    uint8_t *d = dst;
    const uint8_t *s = src;
    if ((((uintptr_t)d | (uintptr_t)s) & 3u) == 0) {
        for (; n >= 4; n -= 4, d += 4, s += 4) {
            *(uint32_t *)(void *)d = *(const uint32_t *)(const void *)s;
        }
    }
    while (n--) {
        *d++ = *s++;
    }
    return dst;
}

void *memset(void *dst, int value, size_t n)
{
    uint8_t *d = dst;
    if (((uintptr_t)d & 3u) == 0) {
        uint32_t word = (uint8_t)value;
        word |= word << 8;
        word |= word << 16;
        for (; n >= 4; n -= 4, d += 4) {
            *(uint32_t *)(void *)d = word;
        }
    }
    while (n--) {
        *d++ = (uint8_t)value;
    }
    return dst;
}
