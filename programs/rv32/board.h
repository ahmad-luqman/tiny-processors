/* RV32 machine contract constants.
 *
 * docs/rv32.md is the source of truth. The same numbers appear in link.ld
 * (RAM origin and length) and as defaults in tools/rv32_image.py and
 * tools/rv32_run_qemu.py; change all of them together.
 *
 * This header is included from C and from start.S, so it contains only
 * preprocessor definitions outside the __ASSEMBLER__ guard.
 */
#ifndef RV32_BOARD_H
#define RV32_BOARD_H

/* RAM: 4 MiB planned; the M1 firmware image, .bss, and stack fit in the
 * first 256 KiB so a small emulator or RTL memory can run the same ELF. */
#define RV32_RAM_BASE          0x80000000
#define RV32_RAM_SLICE_SIZE    0x00040000
#define RV32_RAM_PLANNED_SIZE  0x00400000

/* Debug console: write one byte to TX. STATUS bit 5 reads 1 when TX can
 * accept a byte; in our machine it is always 1. Compatible with the 16550
 * UART on QEMU's virt board. */
#define RV32_CONSOLE_BASE      0x10000000
#define RV32_CONSOLE_TX        0x0
#define RV32_CONSOLE_STATUS    0x5
#define RV32_CONSOLE_TX_READY  0x20

/* Done register: a 32-bit store ends the run. PASS exits with status 0;
 * (code << 16) | FAIL exits with status code, code in 1..255. Compatible
 * with the sifive_test device on QEMU's virt board. */
#define RV32_DONE              0x00100000
#define RV32_DONE_PASS         0x5555
#define RV32_DONE_FAIL         0x3333

#ifndef __ASSEMBLER__
#include <stdint.h>

/* Defined in start.S. Zero means pass; 1..255 is a failure code. */
_Noreturn void rv32_exit(uint32_t code);
#endif

#endif
