#include "console.h"

#include "board.h"
#include "mmio.h"

void rv32_putc(char c)
{
    while ((mmio_read8(RV32_CONSOLE_BASE + RV32_CONSOLE_STATUS) & RV32_CONSOLE_TX_READY) == 0) {
        /* Our machine is always ready; a real UART may not be. */
    }
    mmio_write8(RV32_CONSOLE_BASE + RV32_CONSOLE_TX, (uint8_t)c);
}

void rv32_puts(const char *s)
{
    while (*s != '\0') {
        rv32_putc(*s++);
    }
}

void rv32_put_hex32(uint32_t value)
{
    static const char digits[] = "0123456789abcdef";
    for (int shift = 28; shift >= 0; shift -= 4) {
        rv32_putc(digits[(value >> shift) & 0xFu]);
    }
}

void rv32_put_udec(uint32_t value)
{
    char buffer[10];
    uint32_t count = 0;
    do {
        buffer[count++] = (char)('0' + value % 10u);  /* calls __umodsi3 */
        value /= 10u;                                 /* calls __udivsi3 */
    } while (value != 0);
    while (count > 0) {
        rv32_putc(buffer[--count]);
    }
}
