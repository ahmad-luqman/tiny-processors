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

#include "rv32_fp.h"

int main(void) {
    char line[256];
    unsigned op, rm;
    uint32_t a, b, c, out;
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
        unsigned error = op > OP_MAX || rm > softfloat_round_near_maxMag;
        uint8_t flags = 0;
        out = error ? 0 : rv32_fp(op, rm, a, b, c, &flags);
        printf("%08" PRIx32 " %02x %u\n", out, (unsigned)flags, error);
    }
    if (ferror(stdin) || fflush(stdout)) { fputs("oracle I/O failure\n", stderr); return 2; }
    return 0;
}
