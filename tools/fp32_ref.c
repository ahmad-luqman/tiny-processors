/* Test-only oracle. Arithmetic is Berkeley SoftFloat, never host float. */
#include <ctype.h>
#include <errno.h>
#include <stdlib.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include "softfloat.h"

enum { OP_ADD=0, OP_SUB=1, OP_MUL=2, OP_FMADD=3, OP_FMSUB=4, OP_FNMSUB=5, OP_FNMADD=6, OP_DIV=7, OP_SQRT=8, OP_I32_TO_F32=9, OP_U32_TO_F32=10, OP_F32_TO_I32=11, OP_F32_TO_U32=12, OP_EQ=13, OP_LT=14, OP_LE=15, OP_MIN=16, OP_MAX=17 };
_Static_assert(softfloat_round_near_even == 0 && softfloat_round_minMag == 1 &&
               softfloat_round_min == 2 && softfloat_round_max == 3 &&
               softfloat_round_near_maxMag == 4, "SoftFloat rounding encoding changed");
_Static_assert(softfloat_flag_inexact == 1 && softfloat_flag_underflow == 2 &&
               softfloat_flag_overflow == 4 && softfloat_flag_infinite == 8 &&
               softfloat_flag_invalid == 16, "SoftFloat exception encoding changed");

static bool nan32(uint32_t x) { return (x & 0x7fffffff) > 0x7f800000; }
static bool snan32(uint32_t x) { return nan32(x) && !(x & 0x00400000); }
static uint32_t minmax(uint32_t a, uint32_t b, bool maximum) {
    if (snan32(a) || snan32(b)) softfloat_exceptionFlags |= softfloat_flag_invalid;
    if (nan32(a)) return nan32(b) ? 0x7fc00000 : b;
    if (nan32(b)) return a;
    if (!((a | b) & 0x7fffffff)) return maximum ? a & b : a | b;
    bool less = f32_lt_quiet((float32_t){a}, (float32_t){b});
    return (less != maximum) ? a : b;
}
int main(void) {
    char line[256];
    unsigned op, rm;
    uint32_t a, b, c, out;
    softfloat_detectTininess = softfloat_tininess_afterRounding;
    while (fgets(line, sizeof line, stdin)) {
        uint32_t fields[5];
        char *cursor = line, *end;
        bool malformed = !strchr(line, '\n');
        for (unsigned i = 0; i < 5 && !malformed; ++i) {
            while (isspace((unsigned char)*cursor)) ++cursor;
            if (!isalnum((unsigned char)*cursor)) { malformed = true; break; }
            errno = 0;
            unsigned long long value = strtoull(cursor, &end, i < 2 ? 10 : 16);
            if (end == cursor || errno || value > (i == 0 ? 31u : i == 1 ? 7u : UINT32_MAX)
                || (*end && !isspace((unsigned char)*end))) malformed = true;
            fields[i] = (uint32_t)value;
            cursor = end;
        }
        while (isspace((unsigned char)*cursor)) ++cursor;
        if (malformed || *cursor) {
            fputs("malformed oracle request\n", stderr); return 2;
        }
        op = fields[0]; rm = fields[1]; a = fields[2]; b = fields[3]; c = fields[4];
        softfloat_exceptionFlags = 0;
        softfloat_roundingMode = (uint_fast8_t)rm;
        unsigned error = op > OP_MAX || rm > softfloat_round_near_maxMag;
        float32_t x = {a}, y = {b}, z = {c};
        out = 0;
        if (!error) switch (op) {
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
        default: fputs("unimplemented oracle operation\n", stderr); return 2;
        }
        printf("%08" PRIx32 " %02x %u\n", out, (unsigned)softfloat_exceptionFlags, error);
    }
    if (ferror(stdin) || fflush(stdout)) { fputs("oracle I/O failure\n", stderr); return 2; }
    return 0;
}
