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

#define RV32_GPU_BASE 0x11007000
#define RV32_G3D_BASE 0x11008000
/* The DMA window (issue #20): the RAM G1 and G2 may reach, [START, END); all of RAM at reset. */
#define RV32_DMA_WINDOW_BASE 0x1100a000
#define RV32_DMA_WINDOW_START 0x0
#define RV32_DMA_WINDOW_END 0x4
#define RV32_SIMD4_BASE        0x11004000
#define RV32_SIMD4_PROGRAM     0x11005000
#define RV32_SIMD4_DATA        0x11006000
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

/* RAM: 16 MiB (4 MiB before issue #33, 8 MiB before issue #35); the M1 firmware image, .bss, and stack fit in the
 * first 256 KiB so a small emulator or RTL memory can run the same ELF. */
#define RV32_RAM_BASE          0x80000000
#define RV32_RAM_SLICE_SIZE    0x00040000
#define RV32_RAM_PLANNED_SIZE  0x01000000

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

/* CLINT (Track 1, at QEMU virt's address): mtime is a free-running 64-bit
 * counter, read and written a word at a time. One tick is one clock cycle on
 * the RTL, one executed instruction on the emulator and 100 ns on QEMU, so
 * only monotonicity may be relied on. The `time` CSR reads the same count.
 * Since O1 mip.MTIP is mtime >= mtimecmp and mip.MSIP is msip. */
#define RV32_CLINT_BASE        0x02000000
#define RV32_CLINT_MSIP        0x0
#define RV32_CLINT_MTIMECMP    0x4000
#define RV32_CLINT_MTIME       0xbff8

/* PLIC (O1, at QEMU virt's address): one context, hart 0 in machine mode.
 * Word access only. PRIORITY(n) is source n's priority (3 bits), PENDING the
 * pending word, ENABLE context 0's enable word, THRESHOLD its threshold, and
 * CLAIM returns the best pending source (0 for none) and takes its number
 * back to complete it. The input queue is source RV32_PLIC_SOURCE_INPUT;
 * find it through the device tree's `interrupts`, never by this number. */
#define RV32_PLIC_BASE         0x0c000000
#define RV32_PLIC_PRIORITY(n)  (4 * (n))
#define RV32_PLIC_PENDING      0x1000
#define RV32_PLIC_ENABLE       0x2000
#define RV32_PLIC_THRESHOLD    0x200000
#define RV32_PLIC_CLAIM        0x200004
#define RV32_PLIC_SOURCE_INPUT 12
#define RV32_PLIC_SOURCE_VIRTIO 1

/* virtio-blk (O3): a virtio-mmio version 2 block device in virt's first slot,
 * 512 bytes of registers, one queue of up to 8 entries, a 128 KiB disk. Find it
 * through the device tree ("virtio,mmio" whose DeviceID reads 2). */
#define RV32_VIRTIO_BASE       0x10001000

/* Boot convention (Track 1): at reset a0 holds the hart id and a1 the address
 * of a flattened device tree. On our backends the tree is in this boot ROM;
 * on QEMU it is in RAM. Use a1, never this address. */
#define RV32_BOOTROM_BASE      0x00001000
#define RV32_BOOTROM_SIZE      0x1000

/* Input (M5): EVENT pops the oldest event (0 when empty), COUNT is the
 * queue length (0..16), KEYS has bit k set while key code k is held. An
 * event is RV32_EVENT_VALID | (press << RV32_EVENT_PRESS_SHIFT) | code. */
#define RV32_INPUT_BASE        0x11001000
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
/* Issue #35, for Doom: fire, run, the map, yes and no, and the weapons. */
#define RV32_KEY_CTRL            15
#define RV32_KEY_SHIFT           16
#define RV32_KEY_TAB             17
#define RV32_KEY_Y               18
#define RV32_KEY_N               19
#define RV32_KEY_DIGIT1          20
#define RV32_KEY_DIGIT2          21
#define RV32_KEY_DIGIT3          22
#define RV32_KEY_DIGIT4          23
#define RV32_KEY_DIGIT5          24
#define RV32_KEY_DIGIT6          25
#define RV32_KEY_DIGIT7          26

/* Display (M5): a word write to PRESENT snapshots the framebuffer (a
 * checkpoint hash in M5, the native window in M6); FRAMES counts presents
 * since reset; WIDTH and HEIGHT read the resolution. */
#define RV32_DISPLAY_BASE      0x11002000
#define RV32_DISPLAY_PRESENT   0x0
#define RV32_DISPLAY_FRAMES    0x4
#define RV32_DISPLAY_WIDTH     0x8
#define RV32_DISPLAY_HEIGHT    0xC
#define RV32_DISPLAY_COLUMNS   320
#define RV32_DISPLAY_ROWS      240

/* Palette (issue #35): 256 words, word N the colour of pixel value N as
 * 0x00RRGGBB; word access only, the top byte reads as zero. Its power-on
 * contents are the RGB332 mapping. */
#define RV32_PALETTE_BASE      0x11003000
#define RV32_PALETTE_SIZE      0x400

/* Framebuffer (M5): one byte per pixel, row-major from the top-left, coloured
 * through the palette; readable and writable at every width. */
#define RV32_FB_BASE           0x12000000
#define RV32_FB_SIZE           (RV32_DISPLAY_COLUMNS * RV32_DISPLAY_ROWS)

#ifndef __ASSEMBLER__
#include <stdint.h>

/* Defined in start.S. Zero means pass; 1..255 is a failure code. */
_Noreturn void rv32_exit(uint32_t code);
#endif

#endif
