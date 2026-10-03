/* Issue #33: the floating CSRs and registers from C, for fpcheck and fpmate, which are built
 * rv32if with the soft-float ABI (ILP32) on the bare user library. */
#ifndef RV32_OS_UFLOAT_H
#define RV32_OS_UFLOAT_H

#include <stdint.h>

#define FFLAGS_NX 0x01u /* inexact */
#define FFLAGS_DZ 0x08u /* divide by zero */
#define FFLAGS_NV 0x10u /* invalid */
#define FRM_RNE 0u
#define FRM_RDN 2u

static inline uint32_t f_csr_fcsr(void) { uint32_t v; __asm__ volatile("frcsr %0" : "=r"(v)); return v; }
static inline uint32_t f_csr_flags(void) { uint32_t v; __asm__ volatile("frflags %0" : "=r"(v)); return v; }
static inline uint32_t f_csr_rm(void) { uint32_t v; __asm__ volatile("frrm %0" : "=r"(v)); return v; }
static inline void f_clear_flags(void) { __asm__ volatile("fsflags zero"); }
static inline void f_set_rm(uint32_t rm) { __asm__ volatile("fsrm %0" : : "r"(rm)); }

/* `x`, computed by now: C does not order arithmetic against the CSR accesses above, so a result
 * whose flags are read next goes through here, after operands read from volatiles. */
static inline float f_done(float x)
{
    __asm__ volatile("" : "+f"(x));
    return x;
}

static inline uint32_t f_bits(float x)
{
    union { float f; uint32_t u; } v = { .f = x };
    return v.u;
}

static inline float f_from_bits(uint32_t u)
{
    union { uint32_t u; float f; } v = { .u = u };
    return v.f;
}

/* Every f register's bits OR-ed together: 0 only when all 32 are zero, as a new process's are. */
static inline uint32_t f_registers_or(void)
{
    uint32_t all = 0, v;
#define F_OR(n) __asm__ volatile("fmv.x.w %0, f" #n : "=r"(v)); all |= v;
    F_OR(0) F_OR(1) F_OR(2) F_OR(3) F_OR(4) F_OR(5) F_OR(6) F_OR(7)
    F_OR(8) F_OR(9) F_OR(10) F_OR(11) F_OR(12) F_OR(13) F_OR(14) F_OR(15)
    F_OR(16) F_OR(17) F_OR(18) F_OR(19) F_OR(20) F_OR(21) F_OR(22) F_OR(23)
    F_OR(24) F_OR(25) F_OR(26) F_OR(27) F_OR(28) F_OR(29) F_OR(30) F_OR(31)
#undef F_OR
    return all;
}

/* Every round of the hold phase: f8 and f9 hold `a` and `b`, written once, then read and compared
 * `rounds` times, with a quiet comparison (no flag) between; nothing writes an f register or fcsr,
 * so after the FPU comes back FS stays Clean and the kernel's next claim skips the save. Returns
 * the rounds that found either register changed. One asm statement, so the compiler cannot touch
 * f8 or f9 in between. */
static inline uint32_t f_hold(uint32_t a, uint32_t b, uint32_t rounds)
{
    uint32_t wrong = 0, t;
    __asm__ volatile(
        "fmv.w.x f8, %[a]\n\t"
        "fmv.w.x f9, %[b]\n\t"
        "1: fmv.x.w %[t], f8\n\t"
        "bne %[t], %[a], 2f\n\t"
        "fmv.x.w %[t], f9\n\t"
        "bne %[t], %[b], 2f\n\t"
        "feq.s %[t], f8, f9\n\t"
        "j 3f\n\t"
        "2: addi %[w], %[w], 1\n\t"
        "3: addi %[n], %[n], -1\n\t"
        "bnez %[n], 1b"
        : [w] "+r"(wrong), [n] "+r"(rounds), [t] "=&r"(t)
        : [a] "r"(a), [b] "r"(b)
        : "f8", "f9", "memory");
    return wrong;
}

#define FLOAT_ROUNDS 6000u /* each of fpcheck's and fpmate's arithmetic rounds */
#define HOLD_ROUNDS 60000u /* and of their hold rounds: several timer ticks on every backend */

#endif
