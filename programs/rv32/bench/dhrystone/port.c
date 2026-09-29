/* Dhrystone's output and statistics hooks for the RV32 machine (third_party/dhrystone,
 * docs/rv32-groundwork.md). printf goes to the console, and so does debug_printf:
 * riscv-tests' dhrystone.c defines it empty, which hides the final values the benchmark
 * prints to show it computed correctly. The link passes --wrap=debug_printf, so
 * dhrystone_main.c's calls reach __wrap_debug_printf here instead, and the runner
 * checks every value against its "should be" line. */
#include <stdarg.h>
#include "util.h"

static bench_mark mark;
/* The name on the runner's line. A writable global rather than a literal: the image checker
 * requires initialized data, and Dhrystone itself has none (its globals start at zero). */
const char *dhrystone_bench_name = "dhrystone";

void setStats(int enable)
{
    if (enable) {
        mark = bench_start();
    } else {
        bench_stop(dhrystone_bench_name, mark);
    }
}

int printf(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    int written = bench_vprintf(format, args);
    va_end(args);
    return written;
}

void __wrap_debug_printf(const char *format, ...)
{
    va_list args;
    va_start(args, format);
    bench_vprintf(format, args);
    va_end(args);
}
