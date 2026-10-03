/* fpcheck: each process's floating state is its own (issue #33). It starts fpmate, then both do
 * float work for many timer ticks, preempting each other: fpcheck rounds down and raises NX and
 * DZ, fpmate rounds to nearest and raises NX and NV. Each checks, every round, its results, its
 * rounding mode and that its flags are exactly what its own work raised; a kernel that let one
 * process see the other's registers or fcsr fails the checks. fpcheck reports after fpmate has
 * finished, so the transcript does not depend on the interleaving. */
#include "ufloat.h"
#include "ulib.h"

#define ROUNDS 6000u

static volatile float one = 1.0f, three = 3.0f, zero = 0.0f;

static uint32_t failures;

static void check(const char *what, uint32_t got, uint32_t want)
{
    if (got != want) {
        u_puts("fpcheck: FAILED ");
        u_puts(what);
        u_puts(" got ");
        u_puthex(got);
        u_puts(" want ");
        u_puthex(want);
        u_puts("\n");
        failures++;
    }
}

int main(void)
{
    check("fresh fcsr", f_csr_fcsr(), 0);
    check("fresh registers", f_registers_or(), 0);
    f_set_rm(FRM_RDN);
    uint32_t mate = sys_spawn("fpmate", "");
    uint32_t wrong_quotient = 0, wrong_infinity = 0, wrong_flags = 0, wrong_rm = 0;
    for (uint32_t round = 0; round < ROUNDS; round++) {
        f_clear_flags();
        if (f_bits(f_done(one / three)) != 0x3eaaaaaau) { /* 1/3 rounded down */
            wrong_quotient++;
        }
        if (f_bits(f_done(one / zero)) != 0x7f800000u) {
            wrong_infinity++;
        }
        if (f_csr_flags() != (FFLAGS_NX | FFLAGS_DZ)) {
            wrong_flags++;
        }
        if (f_csr_rm() != FRM_RDN) {
            wrong_rm++;
        }
    }
    uint32_t preempted = sys_switches();
    uint32_t mate_failed = sys_wait(mate);
    u_puts("fpcheck: fresh state\n");
    check("rounding down", wrong_quotient, 0);
    check("rounding mode", wrong_rm, 0);
    u_puts("fpcheck: rounding\n");
    check("division by zero", wrong_infinity, 0);
    check("flags", wrong_flags, 0);
    u_puts("fpcheck: flags\n");
    check("fpmate's checks", mate_failed, 0);
    u_puts("fpcheck: fpmate\n");
    check("preempted", preempted != 0, 1);
    u_puts("fpcheck: preempted\n");
    u_puts(failures ? "fpcheck: FAILED\n" : "fpcheck: ok\n");
    return failures ? 1 : 0;
}
