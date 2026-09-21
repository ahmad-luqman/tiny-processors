/* Exact host arithmetic for the synchronous emulator. SoftFloat state is temporary,
 * not guest state: set it on every call, including calls from another machine. */
#include "rv32_fp.h"
#include "softfloat.h"
#include <stdbool.h>
#include <stdlib.h>
#include <stdio.h>
#include <string.h>
_Static_assert(softfloat_round_near_even == 0 && softfloat_round_minMag == 1 &&
               softfloat_round_min == 2 && softfloat_round_max == 3 &&
               softfloat_round_near_maxMag == 4, "SoftFloat rounding encoding changed");
_Static_assert(softfloat_flag_inexact == 1 && softfloat_flag_underflow == 2 &&
               softfloat_flag_overflow == 4 && softfloat_flag_infinite == 8 &&
               softfloat_flag_invalid == 16, "SoftFloat exception encoding changed");

static bool nan32(uint32_t x) { return (x & 0x7fffffff) > 0x7f800000; }
static bool snan32(uint32_t x) { return nan32(x) && !(x & 0x00400000); }
// RISC-V: signaling NaNs raise NV; one NaN yields the number; opposite
// zeros choose -0 for minimum and +0 for maximum. Two NaNs canonicalize.
static uint32_t minmax(uint32_t a, uint32_t b, bool maximum) {
    if (snan32(a) || snan32(b)) softfloat_exceptionFlags |= softfloat_flag_invalid;
    if (nan32(a)) return nan32(b) ? 0x7fc00000 : b;
    if (nan32(b)) return a;
    if (!((a | b) & 0x7fffffff)) return maximum ? a & b : a | b;
    bool less = f32_lt_quiet((float32_t){a}, (float32_t){b});
    return (less != maximum) ? a : b;
}
uint32_t rv32_fp(unsigned op, unsigned rm, uint32_t a, uint32_t b, uint32_t c, uint8_t *flags)
{
    if (rm > 4 || op > OP_MAX) {
        fprintf(stderr, "rv32_fp: invalid internal operation %u or rounding mode %u\n", op, rm);
        abort(); /* callers validate before invoking arithmetic */
    }
    softfloat_detectTininess = softfloat_tininess_afterRounding;
    softfloat_roundingMode = (uint_fast8_t)rm;
    softfloat_exceptionFlags = 0;
    float32_t x = {a}, y = {b}, z = {c};
    uint32_t out = 0;
    switch (op) {
        case OP_ADD: out = f32_add(x,y).v; break;
        case OP_SUB: out = f32_sub(x,y).v; break;
        case OP_MUL: out = f32_mul(x,y).v; break;
        case OP_FMADD: out = f32_mulAdd(x,y,z).v; break;
        case OP_FMSUB: z.v ^= 0x80000000; out = f32_mulAdd(x,y,z).v; break;
        case OP_FNMSUB: x.v ^= 0x80000000; out = f32_mulAdd(x,y,z).v; break;
        case OP_FNMADD: x.v ^= 0x80000000; z.v ^= 0x80000000; out = f32_mulAdd(x,y,z).v; break;
        case OP_DIV: out = f32_div(x,y).v; break;
        case OP_SQRT: out = f32_sqrt(x).v; break;
        case OP_I32_TO_F32: { int32_t signed_a; memcpy(&signed_a, &a, sizeof a); out = i32_to_f32(signed_a).v; break; }
        case OP_U32_TO_F32: out = ui32_to_f32(a).v; break;
        case OP_F32_TO_I32: out = (uint32_t)f32_to_i32(x,rm,true); break;
        case OP_F32_TO_U32: out = (uint32_t)f32_to_ui32(x,rm,true); break;
        case OP_EQ: out = f32_eq(x,y); break;
        case OP_LT: out = f32_lt(x,y); break;
        case OP_LE: out = f32_le(x,y); break;
        case OP_MIN: case OP_MAX: out = minmax(a,b,op == OP_MAX); break;
        default: fputs("rv32_fp: missing arithmetic dispatch\n", stderr); abort();
    }
    *flags = (uint8_t)softfloat_exceptionFlags;
    return out;
}
