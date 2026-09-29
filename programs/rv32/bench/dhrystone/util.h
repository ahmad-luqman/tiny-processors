/* Freestanding stand-in for riscv-tests' util.h, for third_party/dhrystone.
 *
 * Dhrystone times its loop with read_csr(mcycle), the machine-mode cycle counter.
 * Our machine has the unprivileged Zicntr `cycle` instead, which counts the same
 * clock, so read_csr reads it whatever register it names. setStats brackets the
 * same loop and prints the runner's `bench: dhrystone ...` line.
 */
#ifndef RV32_DHRYSTONE_UTIL_H
#define RV32_DHRYSTONE_UTIL_H

#include "../bench.h"

#define read_csr(reg) ((long)bench_cycles())
void setStats(int enable);

#endif
