/* fpmate: fpcheck's partner (issue #33). It runs beside fpcheck, preempted by the timer, rounding
 * to nearest where fpcheck rounds down and raising only the flags its own work raises: NX from a
 * division, NV from a comparison with a signaling NaN. Its comparisons write no f register, so on
 * QEMU, which leaves FS alone when an operation only accrues flags, they test that the kernel
 * saves fcsr even for a Clean owner. It prints nothing (fpcheck reports); its exit code says what
 * failed, one bit per check. */
#include "ufloat.h"
#include "ulib.h"

#define ROUNDS 6000u
#define COMPARES 8u

static volatile float one = 1.0f, three = 3.0f;
static volatile uint32_t signaling = 0x7f800001u;

int main(void)
{
    uint32_t failed = 0;
    if (f_csr_fcsr() != 0 || f_registers_or() != 0) {
        failed |= 1; /* a new process's floating state is zeros, not fpcheck's */
    }
    f_set_rm(FRM_RNE);
    for (uint32_t round = 0; round < ROUNDS; round++) {
        f_clear_flags();
        if (f_bits(f_done(one / three)) != 0x3eaaaaabu) { /* 1/3 to nearest rounds up */
            failed |= 2;
        }
        float nan = f_from_bits(signaling), x = three;
        for (uint32_t i = 0; i < COMPARES; i++) {
            uint32_t equal;
            /* feq writes x only, and signals NV on a signaling NaN; asm, so each one executes */
            __asm__ volatile("feq.s %0, %1, %2" : "=r"(equal) : "f"(nan), "f"(x));
            if (equal) {
                failed |= 2;
            }
            if (f_csr_flags() != (FFLAGS_NX | FFLAGS_NV)) {
                failed |= 4;
            }
        }
        if (f_csr_rm() != FRM_RNE) {
            failed |= 8;
        }
    }
    if (sys_switches() == 0) {
        failed |= 16; /* never preempted: the test proved nothing */
    }
    return (int)failed;
}
