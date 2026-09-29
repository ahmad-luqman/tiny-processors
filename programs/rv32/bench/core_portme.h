/* CoreMark port for the RV32 machine (docs/rv32-groundwork.md, "CoreMark and Dhrystone").
 *
 * Written for this project after EEMBC's barebones template (third_party/coremark,
 * Apache License 2.0); the benchmark files themselves are used unmodified.
 *
 * Timing: a tick is one `cycle` count (Zicntr). EE_TICKS_PER_SEC fixes a nominal
 * 1 MHz clock, which makes CoreMark's "Iterations/Sec" read directly as
 * CoreMark/MHz and gives its 10-second rule a meaning: at least ten million
 * cycles, which the iteration count is chosen to exceed (on the emulator a tick
 * is an instruction, so the count is chosen to exceed it there too). The
 * measured region is also printed as `bench: coremark cycles=C instret=I`.
 * No floating point (HAS_FLOAT 0): nothing here needs it, and double-precision
 * arithmetic would need a software library the firmware does not have.
 */
#ifndef CORE_PORTME_H
#define CORE_PORTME_H

#include <stddef.h>

#define HAS_FLOAT 0
#define HAS_TIME_H 0
#define USE_CLOCK 0
#define HAS_STDIO 0
#define HAS_PRINTF 0
#ifndef COMPILER_VERSION
#define COMPILER_VERSION "clang " __clang_version__
#endif
#ifndef COMPILER_FLAGS
#define COMPILER_FLAGS "(unknown)"
#endif
#define MEM_LOCATION "STATIC"

typedef signed short ee_s16;
typedef unsigned short ee_u16;
typedef signed int ee_s32;
typedef double ee_f32;
typedef unsigned char ee_u8;
typedef unsigned int ee_u32;
typedef ee_u32 ee_ptr_int;
typedef size_t ee_size_t;
#ifndef NULL
#define NULL ((void *)0)
#endif

#define align_mem(x) (void *)(4 + (((ee_ptr_int)(x)-1) & ~3))

#define CORETIMETYPE ee_u32
typedef ee_u32 CORE_TICKS;

#define SEED_METHOD SEED_VOLATILE
#define MEM_METHOD MEM_STATIC   /* no malloc; the data block is a static array */
#define MULTITHREAD 1
#define USE_PTHREAD 0
#define USE_FORK 0
#define USE_SOCKET 0
#define MAIN_HAS_NOARGC 1
#define MAIN_HAS_NORETURN 0

extern ee_u32 default_num_contexts;

typedef struct CORE_PORTABLE_S {
    ee_u8 portable_id;
} core_portable;

void portable_init(core_portable *p, int *argc, char *argv[]);
void portable_fini(core_portable *p);

#if !defined(PROFILE_RUN) && !defined(PERFORMANCE_RUN) && !defined(VALIDATION_RUN)
#if (TOTAL_DATA_SIZE == 1200)
#define PROFILE_RUN 1
#elif (TOTAL_DATA_SIZE == 2000)
#define PERFORMANCE_RUN 1
#else
#define VALIDATION_RUN 1
#endif
#endif

int ee_printf(const char *fmt, ...);

#endif
