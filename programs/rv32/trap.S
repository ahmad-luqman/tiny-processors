/* Machine-mode trap entry for the M5 diagnostic.
 *
 * mtvec points here (direct mode, so the address must be word aligned).
 * Save the caller-saved registers, hand mcause, mtval, and mepc to
 * diag_trap in C, take the PC it returns as the resume address, restore,
 * and mret. Sixty-four bytes keep the stack 16-byte aligned (ILP32 ABI).
 * Only the diagnostic links this file: csr* and mret are outside the M1
 * image contract, and the image checker admits them with --allow-privileged.
 */
#include "board.h"

    .section .text, "ax", @progbits
    .balign 4
    .globl diag_trap_entry
    .type diag_trap_entry, @function
diag_trap_entry:
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
    call diag_trap             /* returns the PC to resume at */
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
    .size diag_trap_entry, . - diag_trap_entry
