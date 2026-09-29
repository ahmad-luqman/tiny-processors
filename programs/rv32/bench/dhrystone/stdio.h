/* Freestanding stand-in for <stdio.h>, for third_party/dhrystone: printf only. */
#ifndef RV32_DHRYSTONE_STDIO_H
#define RV32_DHRYSTONE_STDIO_H
int printf(const char *format, ...);
#endif
