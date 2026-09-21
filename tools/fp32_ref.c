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
        unsigned error = op > 17 || rm > 4;
        float32_t x = {a}, y = {b}, z = {c};
        out = 0;
        if (!error) switch (op) {
        case 0: out = f32_add(x,y).v; break;
        case 1: out = f32_sub(x,y).v; break;
        case 2: out = f32_mul(x,y).v; break;
        case 3: out = f32_mulAdd(x,y,z).v; break;
        case 4: z.v ^= 0x80000000; out = f32_mulAdd(x,y,z).v; break;
        case 5: x.v ^= 0x80000000; out = f32_mulAdd(x,y,z).v; break;
        case 6: x.v ^= 0x80000000; z.v ^= 0x80000000; out = f32_mulAdd(x,y,z).v; break;
        case 7: out = f32_div(x,y).v; break;
        case 8: out = f32_sqrt(x).v; break;
        case 9: { int32_t signed_a; memcpy(&signed_a, &a, sizeof a); out = i32_to_f32(signed_a).v; break; }
        case 10: out = ui32_to_f32(a).v; break;
        case 11: out = (uint32_t)f32_to_i32(x,rm,true); break;
        case 12: out = (uint32_t)f32_to_ui32(x,rm,true); break;
        case 13: out = f32_eq(x,y); break;
        case 14: out = f32_lt(x,y); break;
        case 15: out = f32_le(x,y); break;
        case 16: case 17: out = minmax(a,b,op == 17); break;
        }
        printf("%08" PRIx32 " %02x %u\n", out, (unsigned)softfloat_exceptionFlags, error);
    }
    if (ferror(stdin) || fflush(stdout)) { fputs("oracle I/O failure\n", stderr); return 2; }
    return 0;
}
