/* CoreMark port for the RV32 machine; see core_portme.h. */
#include "coremark.h"
#include "bench.h"

#if VALIDATION_RUN
volatile ee_s32 seed1_volatile = 0x3415;
volatile ee_s32 seed2_volatile = 0x3415;
volatile ee_s32 seed3_volatile = 0x66;
#endif
#if PERFORMANCE_RUN
volatile ee_s32 seed1_volatile = 0x0;
volatile ee_s32 seed2_volatile = 0x0;
volatile ee_s32 seed3_volatile = 0x66;
#endif
#if PROFILE_RUN
volatile ee_s32 seed1_volatile = 0x8;
volatile ee_s32 seed2_volatile = 0x8;
volatile ee_s32 seed3_volatile = 0x8;
#endif
volatile ee_s32 seed4_volatile = ITERATIONS;
volatile ee_s32 seed5_volatile = 0;

#define EE_TICKS_PER_SEC 100000u /* a nominal 100 kHz clock; core_portme.h says why */

ee_u32 default_num_contexts = 1;
static bench_mark start_mark;
static uint64_t stop_cycles, stop_instret;

void start_time(void)
{
    start_mark = bench_start();
}

void stop_time(void)
{
    stop_cycles = bench_cycles();
    stop_instret = bench_instret();
}

CORE_TICKS get_time(void)
{
    return (CORE_TICKS)(stop_cycles - start_mark.cycles);
}

secs_ret time_in_secs(CORE_TICKS ticks)
{
    return (secs_ret)(ticks / EE_TICKS_PER_SEC);
}

int ee_printf(const char *fmt, ...)
{
    va_list args;
    va_start(args, fmt);
    int written = bench_vprintf(fmt, args);
    va_end(args);
    return written;
}

void portable_init(core_portable *p, int *argc, char *argv[])
{
    (void)argc;
    (void)argv;
    if (sizeof(ee_ptr_int) != sizeof(ee_u8 *)) {
        ee_printf("ERROR! Please define ee_ptr_int to a type that holds a pointer!\n");
    }
    if (sizeof(ee_u32) != 4) {
        ee_printf("ERROR! Please define ee_u32 to a 32b unsigned type!\n");
    }
    p->portable_id = 1;
}

/* The last measured region, the one CoreMark reports (the calibration runs are skipped:
 * ITERATIONS is fixed), in the runner's form. */
void portable_fini(core_portable *p)
{
    p->portable_id = 0;
    bench_printf("bench: coremark iterations=%u cycles=%llu instret=%llu\n", (unsigned)ITERATIONS,
                 (unsigned long long)(stop_cycles - start_mark.cycles),
                 (unsigned long long)(stop_instret - start_mark.instret));
}
