/* fpmate: fpcheck's partner (issue #33). It runs beside fpcheck, preempted by the timer, rounding
 * to nearest where fpcheck rounds down and raising only the flags its own work raises: NX from a
 * division, NV from a comparison with a signaling NaN, which writes no f register: the flag alone
 * must make FS Dirty, as it does on our hart and on QEMU, or the kernel would not save it. Then it holds
 * its own values in f8 and f9 through many ticks without writing an f register or fcsr (f_hold),
 * so the kernel's claims find it Clean on every backend and skip the save. It prints nothing
 * (fpcheck reports); its exit code says what failed, one bit per check.
 *
 * `fpmate fresh` only checks that a new process's floating state is zeros: fpcheck runs it after
 * fpmate has finished, in the process table entry fpmate left. `fpmate bad` takes the FPU and then
 * runs an F instruction whose rounding mode (frm 5) is reserved, which the kernel must kill, not
 * claim the FPU for again. */
#include "ufloat.h"
#include "ulib.h"

#define COMPARES 8u

static volatile float one = 1.0f, three = 3.0f;
static volatile uint32_t signaling = 0x7f800001u;

int main(const char *args)
{
    uint32_t failed = 0;
    if (f_csr_fcsr() != 0 || f_registers_or() != 0) {
        failed |= 1; /* a new process's floating state is zeros, not fpcheck's or an earlier fpmate's */
    }
    if (u_strcmp(args, "fresh") == 0) {
        return (int)failed;
    }
    if (u_strcmp(args, "bad") == 0) {
        /* fsrmi claims the FPU and makes frm 5, reserved; an add rounding as frm says is then
         * illegal, with the FPU already mine */
        __asm__ volatile("fsrmi 5\n\t"
                         "fadd.s f1, f1, f1, dyn");
        return 3;                                        /* not reached: the kernel kills it */
    }
    f_set_rm(FRM_RNE);
    for (uint32_t round = 0; round < FLOAT_ROUNDS; round++) {
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
    /* pi and -e in f8 and f9, and fcsr, as I left them */
    if (f_hold(0x40490fdbu, 0xc02df854u, HOLD_ROUNDS) != 0 || f_csr_fcsr() != (FRM_RNE << 5 | FFLAGS_NX | FFLAGS_NV)) {
        failed |= 16;
    }
    if (sys_switches() < 2) {
        failed |= 32; /* hardly preempted: the test proved little */
    }
    return (int)failed;
}
