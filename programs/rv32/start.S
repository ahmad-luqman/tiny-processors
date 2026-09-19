/* Reset entry for the RV32 machine.
 *
 * The linker script places .text.init first, so _start sits at the reset
 * address 0x80000000. On arrival only the PC is meaningful: no stack, no
 * zeroed .bss, unspecified registers. Initialized data needs no copying
 * because the whole image is loaded at its link address (LMA == VMA).
 *
 * No CSR instructions, no WFI: the M1 slice stays inside base RV32I.
 */
#include "board.h"

    .section .text.init, "ax", @progbits
    .globl _start
    .type _start, @function
_start:
    la sp, _stack_top          /* 16-byte aligned by link.ld */

    la t0, __bss_start         /* zero .bss one word at a time */
    la t1, __bss_end
1:  bgeu t0, t1, 2f
    sw zero, 0(t0)
    addi t0, t0, 4
    j 1b

2:  call main                  /* a0 = return value: 0 pass, 1..255 fail */
    /* fall through into rv32_exit with a0 still holding the code */

    .globl rv32_exit
    .type rv32_exit, @function
rv32_exit:
    li t0, RV32_DONE
    beqz a0, 1f
    slli a0, a0, 16            /* (code << 16) | 0x3333; 0x3333 does not fit ori */
    li t1, RV32_DONE_FAIL
    or a0, a0, t1
    j 2f
1:  li a0, RV32_DONE_PASS
2:  sw a0, 0(t0)               /* the machine stops here */
3:  j 3b                       /* if it does not, spin forever */
    .size rv32_exit, . - rv32_exit
    .size _start, rv32_exit - _start
