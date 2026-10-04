/* riscv_test.h: our machine as the environment of riscv-tests' ISA tests (issue #34).
 *
 * riscv-tests (fetched by `make fetch-rv32-riscv-tests`, see tools/rv32_riscv_tests.py) brings its
 * own environments in a submodule; this header replaces them with a bare M-mode one for our
 * machine, the counterpart of tests/arch/model_test.h:
 *
 * - The test starts at the reset PC (rvtest_entry_point, first in .text.init), installs a trap
 *   handler, and runs in machine mode with TESTNUM (gp) zero.
 * - Pass prints "PASS" on the console and writes the pass word to the done register.
 * - Fail prints "FAIL" and writes a fail word whose number is the failing TESTNUM.
 * - No selected test traps, so any trap prints "TRAP" and fails with number 1, which no test case
 *   uses (riscv-tests numbers them from 2).
 *
 * The same bytes come out of the emulator, both RTL simulators and QEMU's virt board, which has the
 * same console and done register (docs/rv32.md).
 */
#ifndef RV32_RISCV_TEST_H
#define RV32_RISCV_TEST_H

#define RVTEST_RV64U .macro init; .endm
#define RVTEST_RV32U .macro init; .endm

#define TESTNUM gp

#define RV32_PUTS4(a, b, c, d) \
    li t0, 0x10000000; \
    li t1, a; sb t1, 0(t0); \
    li t1, b; sb t1, 0(t0); \
    li t1, c; sb t1, 0(t0); \
    li t1, d; sb t1, 0(t0); \
    li t1, '\n'; sb t1, 0(t0);

#define RV32_DONE(word_reg) \
    li t0, 0x00100000; \
    sw word_reg, 0(t0); \
7781: j 7781b;

#define RVTEST_CODE_BEGIN \
    .section .text.init; \
    .align 2; \
    .globl rvtest_entry_point; \
rvtest_entry_point: \
    la t0, rv32_trap; \
    csrw mtvec, t0; \
    li TESTNUM, 0; \
    init; \
    j rv32_test_start; \
    .align 2; \
rv32_trap: \
    RV32_PUTS4('T', 'R', 'A', 'P') \
    li t2, 0x00013333; \
    RV32_DONE(t2) \
rv32_test_start:

#define RVTEST_CODE_END unimp

#define RVTEST_PASS \
    fence; \
    RV32_PUTS4('P', 'A', 'S', 'S') \
    li t2, 0x5555; \
    RV32_DONE(t2)

#define RVTEST_FAIL \
    fence; \
    RV32_PUTS4('F', 'A', 'I', 'L') \
    slli t2, TESTNUM, 16; \
    li t1, 0x3333; \
    or t2, t2, t1; \
    RV32_DONE(t2)

#define EXTRA_DATA
#define RVTEST_DATA_BEGIN EXTRA_DATA .align 4; .global begin_signature; begin_signature:
#define RVTEST_DATA_END .align 4; .global end_signature; end_signature:

#endif
