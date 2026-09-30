/* Machine-mode trap entry for programs that take interrupts (O1).
 *
 * mtvec points here (direct mode). Save the caller-saved registers, hand
 * mcause, mtval and mepc to trap_handler in C, take the PC it returns as the
 * resume address, restore, and mret. The same shape as trap.S, the M5
 * diagnostic's entry, under a name any program can link; an interrupt is
 * resumed at mepc itself, an exception usually at mepc + 4.
 */
#include "board.h"

    .section .text, "ax", @progbits
    .balign 4
    .globl trap_entry
    .type trap_entry, @function
trap_entry:
    addi sp, sp, -64
    sw ra, 0(sp)
    sw t0, 4(sp)
    sw t1, 8(sp)
    sw t2, 12(sp)
    sw t3, 16(sp)
    sw t4, 20(sp)
    sw t5, 24(sp)
    sw t6, 28(sp)
    sw a0, 32(sp)
    sw a1, 36(sp)
    sw a2, 40(sp)
    sw a3, 44(sp)
    sw a4, 48(sp)
    sw a5, 52(sp)
    sw a6, 56(sp)
    sw a7, 60(sp)
    csrr a0, mcause
    csrr a1, mtval
    csrr a2, mepc
    call trap_handler             /* returns the PC to resume at */
    csrw mepc, a0
    lw ra, 0(sp)
    lw t0, 4(sp)
    lw t1, 8(sp)
    lw t2, 12(sp)
    lw t3, 16(sp)
    lw t4, 20(sp)
    lw t5, 24(sp)
    lw t6, 28(sp)
    lw a0, 32(sp)
    lw a1, 36(sp)
    lw a2, 40(sp)
    lw a3, 44(sp)
    lw a4, 48(sp)
    lw a5, 52(sp)
    lw a6, 56(sp)
    lw a7, 60(sp)
    addi sp, sp, 64
    mret
    .size trap_entry, . - trap_entry
