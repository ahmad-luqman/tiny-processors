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

#define RV32_GPU_BASE 0x20007000
#define RV32_G3D_BASE 0x20008000
#define RV32_SIMD4_BASE        0x20004000
#define RV32_SIMD4_PROGRAM     0x20005000
#define RV32_SIMD4_DATA        0x20006000
#define RV32_SIMD4_COMMAND           0x00
#define RV32_SIMD4_STATUS            0x04
#define RV32_SIMD4_ENTRY             0x08
#define RV32_SIMD4_CYCLES            0x0c
#define RV32_SIMD4_STALLS            0x10
#define RV32_SIMD4_TRANSFERS         0x14
#define RV32_SIMD4_INSTRUCTIONS      0x18
#define RV32_SIMD4_BUSY              0x01
#define RV32_SIMD4_DONE              0x02
#define RV32_SIMD4_FAULT             0x04
#define RV32_SIMD4_START             0x01
#define RV32_SIMD4_RESET             0x02

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

/* Timer (M5): TICKS is a free-running 32-bit counter. One tick is one clock
 * cycle on the RTL and one executed instruction on the emulator, so only
 * monotonicity modulo 2^32 may be relied on. A word write loads it. */
#define RV32_TIMER_BASE        0x20000000
#define RV32_TIMER_TICKS       0x0

/* Input (M5): EVENT pops the oldest event (0 when empty), COUNT is the
 * queue length (0..16), KEYS has bit k set while key code k is held. An
 * event is RV32_EVENT_VALID | (press << RV32_EVENT_PRESS_SHIFT) | code. */
#define RV32_INPUT_BASE        0x20001000
#define RV32_INPUT_EVENT       0x0
#define RV32_INPUT_COUNT       0x4
#define RV32_INPUT_KEYS        0x8
#define RV32_INPUT_QUEUE       16
#define RV32_EVENT_VALID       0x80000000
#define RV32_EVENT_PRESS       0x00000100
#define RV32_EVENT_PRESS_SHIFT 8
#define RV32_EVENT_CODE_MASK   0x1F

/* Key codes are 0..31 so KEYS holds one bit per key. */
#define RV32_KEY_LEFT          1
#define RV32_KEY_RIGHT         2
#define RV32_KEY_UP            3
#define RV32_KEY_DOWN          4
#define RV32_KEY_SPACE         5
#define RV32_KEY_ENTER         6
#define RV32_KEY_ESCAPE        7
#define RV32_KEY_A             8
#define RV32_KEY_D             9
#define RV32_KEY_W             10
#define RV32_KEY_S             11
#define RV32_KEY_P             12
#define RV32_KEY_Q             13
#define RV32_KEY_R             14

/* Display (M5): a word write to PRESENT snapshots the framebuffer (a
 * checkpoint hash in M5, the native window in M6); FRAMES counts presents
 * since reset; WIDTH and HEIGHT read the resolution. */
#define RV32_DISPLAY_BASE      0x20002000
#define RV32_DISPLAY_PRESENT   0x0
#define RV32_DISPLAY_FRAMES    0x4
#define RV32_DISPLAY_WIDTH     0x8
#define RV32_DISPLAY_HEIGHT    0xC
#define RV32_DISPLAY_COLUMNS   320
#define RV32_DISPLAY_ROWS      240

/* Framebuffer (M5): one byte per pixel, row-major from the top-left, RGB332
 * until a palette exists; readable and writable at every width. */
#define RV32_FB_BASE           0x30000000
#define RV32_FB_SIZE           (RV32_DISPLAY_COLUMNS * RV32_DISPLAY_ROWS)

#ifndef __ASSEMBLER__
#include <stdint.h>

/* Defined in start.S. Zero means pass; 1..255 is a failure code. */
_Noreturn void rv32_exit(uint32_t code);
#endif

#endif
