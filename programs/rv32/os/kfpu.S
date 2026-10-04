/* The floating registers' save and load for the kernel's lazy switch (issue #33;
 * docs/rv32-os.md, "Floating state").
 *
 * The kernel is RV32I and never uses the FPU itself; these two routines are
 * its only F instructions. A struct fstate (kernel.c) holds f0..f31 at 4*n and
 * fcsr at 128. Each first sets mstatus.FS: the kernel runs with the trapped
 * context's mstatus, whose FS is Off for the process claiming the FPU (that is
 * why it trapped), and an F instruction with FS Off would be a fault in the
 * kernel. kernel_resume writes the frame's mstatus back before mret.
 */
    .section .text, "ax", @progbits

    /* mstatus.FS on (Dirty), for the routines' own F instructions. */
    .macro fs_on
    li t0, 0x6000
    csrs mstatus, t0
    .endm

    /* fpu_save(struct fstate *s): f0..f31 and fcsr. */
    .balign 4
    .globl fpu_save
    .type fpu_save, @function
fpu_save:
    fs_on
    frcsr t1
    sw t1, 128(a0)
    .irp n, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31
    fsw f\n, 4*\n(a0)
    .endr
    ret
    .size fpu_save, . - fpu_save

    /* fpu_load(const struct fstate *s): f0..f31 and fcsr, every one of them, so that nothing of
     * an owner that exited unsaved is left for the next. */
    .balign 4
    .globl fpu_load
    .type fpu_load, @function
fpu_load:
    fs_on
    .irp n, 0, 1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17, 18, 19, 20, 21, 22, 23, 24, 25, 26, 27, 28, 29, 30, 31
    flw f\n, 4*\n(a0)
    .endr
    lw t1, 128(a0)
    fscsr t1
    ret
    .size fpu_load, . - fpu_load
