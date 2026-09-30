/* memcpy and memset for the kernel and the programs (Track 2): the compiler may call them for
 * structure copies even with -fno-builtin, and there is no C library. */
#include <stddef.h>
#include <stdint.h>

void *memcpy(void *dst, const void *src, size_t n);
void *memset(void *dst, int value, size_t n);

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
