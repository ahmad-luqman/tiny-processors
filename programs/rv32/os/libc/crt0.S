/* Entry of a program built with the C library (Track 3, L1). The kernel starts
 * a program as ustart.S describes: .bss zeroed, sp below the argument string,
 * a0 pointing at that string. __libc_start (crt.c) turns it into argc and
 * argv, runs the constructors and calls exit(main(argc, argv)). */
    .section .text.init, "ax", @progbits
    .globl _start
    .type _start, @function
_start:
    call __libc_start
1:  j 1b
    .size _start, . - _start
