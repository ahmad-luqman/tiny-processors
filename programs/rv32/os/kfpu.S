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

    /* fpu_save(struct fstate *s, uint32_t registers): fcsr always; f0..f31 when
     * `registers` is nonzero (the owner's FS was Dirty: Clean means memory has them). */
    .balign 4
    .globl fpu_save
    .type fpu_save, @function
fpu_save:
    li t0, 0x6000
    csrs mstatus, t0
    frcsr t1
    sw t1, 128(a0)
    beqz a1, 1f
    fsw f0, 0(a0)
    fsw f1, 4(a0)
    fsw f2, 8(a0)
    fsw f3, 12(a0)
    fsw f4, 16(a0)
    fsw f5, 20(a0)
    fsw f6, 24(a0)
    fsw f7, 28(a0)
    fsw f8, 32(a0)
    fsw f9, 36(a0)
    fsw f10, 40(a0)
    fsw f11, 44(a0)
    fsw f12, 48(a0)
    fsw f13, 52(a0)
    fsw f14, 56(a0)
    fsw f15, 60(a0)
    fsw f16, 64(a0)
    fsw f17, 68(a0)
    fsw f18, 72(a0)
    fsw f19, 76(a0)
    fsw f20, 80(a0)
    fsw f21, 84(a0)
    fsw f22, 88(a0)
    fsw f23, 92(a0)
    fsw f24, 96(a0)
    fsw f25, 100(a0)
    fsw f26, 104(a0)
    fsw f27, 108(a0)
    fsw f28, 112(a0)
    fsw f29, 116(a0)
    fsw f30, 120(a0)
    fsw f31, 124(a0)
1:  ret
    .size fpu_save, . - fpu_save

    /* fpu_load(const struct fstate *s): f0..f31 and fcsr. */
    .balign 4
    .globl fpu_load
    .type fpu_load, @function
fpu_load:
    li t0, 0x6000
    csrs mstatus, t0
    flw f0, 0(a0)
    flw f1, 4(a0)
    flw f2, 8(a0)
    flw f3, 12(a0)
    flw f4, 16(a0)
    flw f5, 20(a0)
    flw f6, 24(a0)
    flw f7, 28(a0)
    flw f8, 32(a0)
    flw f9, 36(a0)
    flw f10, 40(a0)
    flw f11, 44(a0)
    flw f12, 48(a0)
    flw f13, 52(a0)
    flw f14, 56(a0)
    flw f15, 60(a0)
    flw f16, 64(a0)
    flw f17, 68(a0)
    flw f18, 72(a0)
    flw f19, 76(a0)
    flw f20, 80(a0)
    flw f21, 84(a0)
    flw f22, 88(a0)
    flw f23, 92(a0)
    flw f24, 96(a0)
    flw f25, 100(a0)
    flw f26, 104(a0)
    flw f27, 108(a0)
    flw f28, 112(a0)
    flw f29, 116(a0)
    flw f30, 120(a0)
    flw f31, 124(a0)
    lw t1, 128(a0)
    fscsr t1
    ret
    .size fpu_load, . - fpu_load
