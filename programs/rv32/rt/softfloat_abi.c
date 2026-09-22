/* Benchmark-only RV32I compiler libcalls, implemented by pinned SoftFloat.
 * ILP32 passes float bit patterns in integer registers on both build variants.
 * This is software execution in the guest, not the emulator's host adapter. */
#include <stdint.h>
#include "softfloat.h"
float __addsf3(float a, float b)
{
    union { float f; uint32_t u; } x = {.f=a}, y = {.f=b}, z;
    z.u = f32_add((float32_t){x.u}, (float32_t){y.u}).v; return z.f;
}
float __subsf3(float a, float b)
{
    union { float f; uint32_t u; } x = {.f=a}, y = {.f=b}, z;
    z.u = f32_sub((float32_t){x.u}, (float32_t){y.u}).v; return z.f;
}
float __mulsf3(float a, float b)
{
    union { float f; uint32_t u; } x = {.f=a}, y = {.f=b}, z;
    z.u = f32_mul((float32_t){x.u}, (float32_t){y.u}).v; return z.f;
}
float __divsf3(float a, float b)
{
    union { float f; uint32_t u; } x = {.f=a}, y = {.f=b}, z;
    z.u = f32_div((float32_t){x.u}, (float32_t){y.u}).v; return z.f;
}
/* Low 64 bits of an unsigned product using 32-bit limbs, without recursive
 * compiler multiplication libcalls. Needed by SoftFloat's significand multiply. */
uint64_t __muldi3(uint64_t a, uint64_t b)
{
    union { uint64_t u; struct { uint32_t lo, hi; } w; } x = {.u=a}, y = {.u=b}, z = {.u=0};
    for (unsigned i = 0; i < 64; ++i) {
        if (y.w.lo & 1u) {
            uint32_t old = z.w.lo;
            z.w.lo += x.w.lo;
            z.w.hi += x.w.hi + (z.w.lo < old);
        }
        x.w.hi = (x.w.hi << 1) | (x.w.lo >> 31); x.w.lo <<= 1;
        y.w.lo = (y.w.lo >> 1) | (y.w.hi << 31); y.w.hi >>= 1;
    }
    return z.u;
}
