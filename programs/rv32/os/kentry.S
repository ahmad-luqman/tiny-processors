/* Kernel entry, trap entry and resume (Track 2, O2; docs/rv32-os.md).
 *
 * Every context the machine runs, each process and the kernel's idle loop,
 * has a struct frame (kernel.c): x0..x31 at 4*n (x0's word unused), the pc
 * at 128 and mstatus at 132. While a context runs, mscratch holds the
 * address of its frame. A trap swaps sp with mscratch, saves every register
 * and the trap CSRs into that frame, switches to the kernel stack and calls
 * kernel_trap(frame), which returns the frame to run next; kernel_resume
 * restores it and returns with mret. The kernel itself always runs with
 * interrupts off (trap entry clears MIE), on one stack, and must never trap:
 * mscratch is 0 while it runs, so a trap that swaps a 0 into sp is a fault in
 * the kernel. That path takes the kernel's own stack back and calls
 * kernel_fault(), which reports it and stops the machine, instead of saving
 * registers through a user stack pointer into a frame that does not exist.
 */
#include "board.h"

    .section .text.init, "ax", @progbits
    .globl _start
    .type _start, @function
_start:
    la sp, _stack_top            /* a0 (hart) and a1 (tree) pass through to kernel_main */
    la t0, __bss_start
    la t1, __bss_end
1:  bgeu t0, t1, 2f
    sw zero, 0(t0)
    addi t0, t0, 4
    j 1b
2:  call kernel_main             /* never returns: it resumes the first process */
3:  j 3b
    .size _start, . - _start

    .section .text, "ax", @progbits
    .balign 4
    .globl trap_vector
    .type trap_vector, @function
trap_vector:
    csrrw sp, mscratch, sp       /* sp = the frame, mscratch = the context's sp */
    beqz sp, kernel_trapped      /* the kernel was running: mscratch was 0 */
    sw x1, 4(sp)
    sw x3, 12(sp)
    sw x4, 16(sp)
    sw x5, 20(sp)
    sw x6, 24(sp)
    sw x7, 28(sp)
    sw x8, 32(sp)
    sw x9, 36(sp)
    sw x10, 40(sp)
    sw x11, 44(sp)
    sw x12, 48(sp)
    sw x13, 52(sp)
    sw x14, 56(sp)
    sw x15, 60(sp)
    sw x16, 64(sp)
    sw x17, 68(sp)
    sw x18, 72(sp)
    sw x19, 76(sp)
    sw x20, 80(sp)
    sw x21, 84(sp)
    sw x22, 88(sp)
    sw x23, 92(sp)
    sw x24, 96(sp)
    sw x25, 100(sp)
    sw x26, 104(sp)
    sw x27, 108(sp)
    sw x28, 112(sp)
    sw x29, 116(sp)
    sw x30, 120(sp)
    sw x31, 124(sp)
    csrr t0, mscratch
    sw t0, 8(sp)                 /* the context's own sp */
    csrw mscratch, zero          /* the kernel runs: a trap now is a fault in it */
    csrr t0, mepc
    sw t0, 128(sp)
    csrr t0, mstatus
    sw t0, 132(sp)
    mv a0, sp
    la sp, _stack_top
    call kernel_trap             /* returns the frame to resume */
    /* fall through */
    .globl kernel_resume
    .type kernel_resume, @function
kernel_resume:                   /* a0 = the frame */
    csrw mscratch, a0
    lw t0, 128(a0)
    csrw mepc, t0
    lw t0, 132(a0)
    csrw mstatus, t0             /* MIE stays clear; mret takes MIE from MPIE and the mode from MPP */
    lw x1, 4(a0)
    lw x2, 8(a0)
    lw x3, 12(a0)
    lw x4, 16(a0)
    lw x5, 20(a0)
    lw x6, 24(a0)
    lw x7, 28(a0)
    lw x8, 32(a0)
    lw x9, 36(a0)
    lw x11, 44(a0)
    lw x12, 48(a0)
    lw x13, 52(a0)
    lw x14, 56(a0)
    lw x15, 60(a0)
    lw x16, 64(a0)
    lw x17, 68(a0)
    lw x18, 72(a0)
    lw x19, 76(a0)
    lw x20, 80(a0)
    lw x21, 84(a0)
    lw x22, 88(a0)
    lw x23, 92(a0)
    lw x24, 96(a0)
    lw x25, 100(a0)
    lw x26, 104(a0)
    lw x27, 108(a0)
    lw x28, 112(a0)
    lw x29, 116(a0)
    lw x30, 120(a0)
    lw x31, 124(a0)
    lw x10, 40(a0)
    mret
    .size trap_vector, . - trap_vector

/* A trap while the kernel ran: sp is 0 and mscratch holds the kernel's sp. */
kernel_trapped:
    la sp, _stack_top
    call kernel_fault            /* reports the trap and halts; never returns */
1:  j 1b

/* write_scounteren(a0): scounteren = a0 where the hart has S-mode (QEMU's does, and there a
 * user-mode counter read needs its bit in scounteren as well as mcounteren); on ours the CSR does
 * not exist and the write is an illegal instruction, so mtvec points at the next instruction
 * meanwhile and the trap just skips it. Boot time only: interrupts are off. */
    .balign 4
    .globl write_scounteren
    .type write_scounteren, @function
write_scounteren:
    la t0, 1f
    csrrw t0, mtvec, t0
    csrw scounteren, a0
    .balign 4
1:  csrw mtvec, t0
    ret
    .size write_scounteren, . - write_scounteren

/* The idle context: waits for an interrupt with interrupts on. */
    .balign 4
    .globl kernel_idle
    .type kernel_idle, @function
kernel_idle:
    wfi
    j kernel_idle
    .size kernel_idle, . - kernel_idle

/* The RAM disk: the program images tools/rv32_ramdisk.py packed. */
    .section .rodata.ramdisk, "a", @progbits
    .balign 4
    .globl ramdisk
ramdisk:
    .incbin RAMDISK_IMAGE
    .globl ramdisk_end
ramdisk_end:
