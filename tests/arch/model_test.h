/* model_test.h: our machine as a target of the RISC-V architectural tests
 * (riscv-arch-test, "the model" in its terms; docs/rv32-groundwork.md).
 *
 * The suite's test files include this header and then arch_test.h, which
 * expands the RVMODEL_* macros below around each test. What they do here:
 *
 * - Boot installs a trap handler, and any trap fails the test (done code 2):
 *   no selected test is meant to trap. The one place the I, M and F suites
 *   touch a privileged CSR is RVTEST_FP_ENABLE's `csrs mstatus, a0`, which sets
 *   FS so F instructions work on a machine that can turn them off. Until
 *   Track 2 the machine had no mstatus and the handler skipped exactly that
 *   word; since O1 mstatus exists and the write simply retires, as on QEMU.
 *   FS read 3 whatever was written until issue #33 made it writable; it is
 *   Dirty from reset and the write keeps it on.
 * - Halt prints the signature (begin_signature to end_signature) on the
 *   console, one line per 32-bit word: the word's value as eight lowercase hex
 *   digits, most significant first, the format the suite's reference
 *   signatures use; then it writes the pass word.
 *   The same bytes come out of the emulator, both RTL simulators and QEMU, whose
 *   virt board has the same console and done register (docs/rv32.md).
 * - With RVMODEL_ASSERT defined (the I and M suites), every integer result is
 *   also compared with the value the test generator computed and written into
 *   the test source, and a mismatch fails the test (done code 3). The F suite's
 *   sources carry no expected values, so only the signature comparison applies.
 */
#ifndef RV32_MODEL_TEST_H
#define RV32_MODEL_TEST_H

#define RVMODEL_DATA_SECTION

#define RVMODEL_BOOT \
    la t0, rvmodel_trap; \
    csrw mtvec, t0;

#define RVMODEL_HALT \
    la t0, begin_signature; \
    la t1, end_signature; \
    li t2, 0x10000000; \
7771: bgeu t0, t1, 7774f; \
    lw t3, 0(t0); \
    li t4, 28; \
7772: srl t5, t3, t4; \
    andi t5, t5, 15; \
    addi t6, t5, '0'; \
    li a0, 10; \
    blt t5, a0, 7773f; \
    addi t6, t5, 'a' - 10; \
7773: sb t6, 0(t2); \
    addi t4, t4, -4; \
    bgez t4, 7772b; \
    li t6, '\n'; \
    sb t6, 0(t2); \
    addi t0, t0, 4; \
    j 7771b; \
7774: li t0, 0x00100000; \
    li t1, 0x5555; \
    sw t1, 0(t0); \
7775: j 7775b; \
    .align 2; \
rvmodel_trap: \
    li t0, 0x00100000; \
    li t1, 0x00023333; \
    sw t1, 0(t0); \
7776: j 7776b; \
rvmodel_assert_fail: \
    li t0, 0x00100000; \
    li t1, 0x00033333; \
    sw t1, 0(t0); \
7777: j 7777b;

#define RVMODEL_DATA_BEGIN RVMODEL_DATA_SECTION .align 4; .global begin_signature; begin_signature:
#define RVMODEL_DATA_END .align 4; .global end_signature; end_signature:

#define RVMODEL_IO_INIT
#define RVMODEL_IO_WRITE_STR(_R, _STR)
#define RVMODEL_IO_CHECK()
#ifdef RVMODEL_ASSERT
#define RVMODEL_IO_ASSERT_GPR_EQ(_S, _R, _I) \
    li _S, _I; \
    beq _S, _R, 7778f; \
    j rvmodel_assert_fail; \
7778:
#else
#define RVMODEL_IO_ASSERT_GPR_EQ(_S, _R, _I)
#endif
#define RVMODEL_IO_ASSERT_SFPR_EQ(_F, _R, _I)
#define RVMODEL_IO_ASSERT_DFPR_EQ(_D, _R, _I)

#define RVMODEL_SET_MSW_INT
#define RVMODEL_CLR_MSW_INT
#define RVMODEL_CLR_MTIMER_INT
#define RVMODEL_CLR_MEXT_INT

#endif
