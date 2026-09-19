/* Byte output on the machine's debug console. */
#ifndef RV32_CONSOLE_H
#define RV32_CONSOLE_H

#include <stdint.h>

void rv32_putc(char c);
void rv32_puts(const char *s);
void rv32_put_hex32(uint32_t value);  /* exactly eight lowercase digits */
void rv32_put_udec(uint32_t value);   /* decimal, no leading zeros */

#endif
