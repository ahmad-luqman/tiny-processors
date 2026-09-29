/* bench: what the benchmark ports need from a machine without a C library
 * (docs/rv32-groundwork.md, "CoreMark and Dhrystone").
 *
 * The Zicntr counters as 64-bit values, a small printf onto the console, and the
 * string and memory routines the benchmarks (and the compiler, for struct copies)
 * call. Timing lines are printed in one fixed form, `bench: <name> cycles=C
 * instret=I`, so a runner can compare everything else across backends: on the
 * RTL a cycle is a clock cycle, on the emulator an instruction (device time).
 */
#ifndef RV32_BENCH_H
#define RV32_BENCH_H

#include <stdarg.h>
#include <stddef.h>
#include <stdint.h>

uint64_t bench_cycles(void);
uint64_t bench_instret(void);

/* Start and stop one measured region, then print its line. */
typedef struct {
    uint64_t cycles, instret;
} bench_mark;
bench_mark bench_start(void);
void bench_stop(const char *name, bench_mark start);

/* printf onto the console: %d %i %u %x %X %c %s %% with an optional 0 flag, a width and
 * l/ll length modifiers (64-bit values print without 64-bit division). */
int bench_vprintf(const char *format, va_list args);
int bench_printf(const char *format, ...);

void *memcpy(void *destination, const void *source, size_t n);
void *memset(void *destination, int value, size_t n);
char *strcpy(char *destination, const char *source);
int strcmp(const char *a, const char *b);
size_t strlen(const char *s);

#endif
