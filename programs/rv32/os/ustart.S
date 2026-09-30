/* Program entry (Track 2, O2). The kernel loads the program into its slot,
 * zeroes .bss, points sp below the slot's top and a0 at the argument string,
 * and starts here; main's return value is the exit code. */
#include "sys.h"

    .section .text.init, "ax", @progbits
    .globl _start
    .type _start, @function
_start:
    call main
    li a7, SYS_EXIT
    ecall
1:  j 1b
    .size _start, . - _start
