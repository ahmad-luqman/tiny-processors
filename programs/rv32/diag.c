/* M5 device diagnostic.
 *
 * Exercises every device of docs/rv32.md on whichever backend runs it: the
 * timer (monotonic, writable, wrapping), faults through a trap handler
 * (trap.S), the display and framebuffer (writes at every width, a readback
 * hashed the way the host hashes a checkpoint, two presents), and the input
 * queue (scripted events delivered at those presents). Every value that is
 * the same on both backends is folded into a checksum; timer readings are
 * not, because a tick is a cycle on the RTL and an instruction on the
 * emulator (device time). The console shows one line per section and ends
 * with `PASS <8 hex digits>` or `FAIL <n>`; the runner compares the whole
 * transcript and the checkpoints between backends.
 *
 * DIAG_EXPECTED and the frame-1 hash are recomputed independently by
 * tests/test_rv32_tools.py from the pattern drawn below.
 */
#include <stdint.h>

#include "board.h"
#include "console.h"
#include "mmio.h"

#define DIAG_EXPECTED 0x8bd87e9au /* FNV-1a fold of the 27 checked values; also the Makefile's RV32_DIAG_HEX */
#define DIAG_UNMAPPED 0x50000000u /* no window there (tools/rv32_asm.py UNMAPPED) */
#define FB_WORDS (RV32_FB_SIZE / 4u)

static uint32_t checksum = 2166136261u;

static uint32_t opaque(uint32_t x)
{
    __asm__ volatile("" : "+r"(x));
    return x;
}

static void fold(uint32_t value)
{
    checksum = (checksum ^ value) * 16777619u; /* FNV-1a; the `*` is __mulsi3 */
}

static int failed(uint32_t number)
{
    if (number == 0 || number > 255) {
        number = 255; /* 0 would become the pass word in rv32_exit */
    }
    rv32_puts("FAIL ");
    rv32_put_udec(number);
    rv32_putc('\n');
    return (int)number;
}

#define CHECK(number, observed, expected)                         \
    do {                                                          \
        uint32_t observed_ = (uint32_t)(observed);                \
        if (observed_ != (uint32_t)(expected)) {                  \
            return failed(number);                                \
        }                                                         \
        fold(observed_);                                          \
    } while (0)

/* Trap bookkeeping, filled by diag_trap, which trap.S calls with mcause,
 * mtval, and mepc and which returns the PC to resume at. Every RV32I
 * instruction is four bytes, so the faulting one is skipped. */
#define TRAPS 8
static volatile uint32_t trap_count;
static volatile uint32_t trap_cause[TRAPS], trap_tval[TRAPS];

uint32_t diag_trap(uint32_t cause, uint32_t tval, uint32_t epc)
{
    if (trap_count < TRAPS) {
        trap_cause[trap_count] = cause;
        trap_tval[trap_count] = tval;
    }
    trap_count++;
    return epc + 4;
}

extern void diag_trap_entry(void);

static void set_mtvec(void (*entry)(void))
{
    __asm__ volatile("csrw mtvec, %0" : : "r"(entry));
}

/* The checkpoint hash of docs/rv32.md over the framebuffer as read back
 * through the bus: h = ((h << 5) + h) ^ word from 5381; shift, add, xor, no
 * multiply. */
static uint32_t hash_frame(void)
{
    uint32_t h = 5381u;
    for (uint32_t i = 0; i < FB_WORDS; i++) {
        h = ((h << 5) + h) ^ mmio_read32(RV32_FB_BASE + 4u * i);
    }
    return h;
}

/* The pattern, also drawn by tools/rv32_devices.py: pixel (x, y) is
 * (x ^ y) & 0xFF, written four pixels per word store, then a red box by
 * byte stores, then one row by halfword stores. */
static void draw_frame_one(void)
{
    uint32_t row = RV32_FB_BASE;
    for (uint32_t y = 0; y < RV32_DISPLAY_ROWS; y++) {
        for (uint32_t x = 0; x < RV32_DISPLAY_COLUMNS; x += 4) {
            uint32_t v = (x ^ y) & 0xFFu;
            uint32_t word = v | (v << 8) | (v << 16) | (v << 24);
            mmio_write32(row + x, word ^ 0x03020100u); /* lanes 1..3 are x+1..x+3 */
        }
        row += RV32_DISPLAY_COLUMNS;
    }
    row = RV32_FB_BASE + 40u * RV32_DISPLAY_COLUMNS;
    for (uint32_t y = 40; y < 80; y++) {
        for (uint32_t x = 100; x < 200; x++) {
            mmio_write8(row + x, 0xE0); /* red */
        }
        row += RV32_DISPLAY_COLUMNS;
    }
    row = RV32_FB_BASE + 120u * RV32_DISPLAY_COLUMNS;
    for (uint32_t x = 0; x < RV32_DISPLAY_COLUMNS; x += 2) {
        mmio_write16(row + x, 0x1C1C); /* green */
    }
}

/* Frame two adds a blue box. */
static void draw_frame_two(void)
{
    uint32_t row = RV32_FB_BASE + 200u * RV32_DISPLAY_COLUMNS;
    for (uint32_t y = 200; y < 220; y++) {
        for (uint32_t x = 10; x < 30; x++) {
            mmio_write8(row + x, 0x03);
        }
        row += RV32_DISPLAY_COLUMNS;
    }
}

static uint32_t input_word(uint32_t offset)
{
    return mmio_read32(RV32_INPUT_BASE + offset);
}

int main(void)
{
    /* 1. Timer: it advances, a write loads it, and it wraps. */
    uint32_t t0 = mmio_read32(RV32_TIMER_BASE + RV32_TIMER_TICKS);
    for (uint32_t i = 0; i < 100; i++) {
        opaque(i);
    }
    uint32_t t1 = mmio_read32(RV32_TIMER_BASE + RV32_TIMER_TICKS);
    CHECK(1, (t1 - t0) != 0 && (t1 - t0) < 0x80000000u, 1);
    mmio_write32(RV32_TIMER_BASE + RV32_TIMER_TICKS, 0xFFFFFF00u);
    uint32_t ticks, guard = 0;
    do {
        ticks = mmio_read32(RV32_TIMER_BASE + RV32_TIMER_TICKS);
    } while (ticks >= 0xFFFFFF00u && ++guard < 100000u);
    CHECK(2, ticks < 0x10000u, 1); /* wrapped through zero, not lost */
    rv32_puts("diag: timer ok\n");

    /* 2. Faults: one per device rule, each resumed after the faulting instruction. */
    set_mtvec(diag_trap_entry);
    (void)mmio_read32(DIAG_UNMAPPED);
    (void)mmio_read8(RV32_TIMER_BASE + RV32_TIMER_TICKS);
    mmio_write32(RV32_INPUT_BASE + RV32_INPUT_KEYS, 1);
    mmio_write8(RV32_FB_BASE + RV32_FB_SIZE, 1);
    CHECK(3, trap_count, 4);
    CHECK(4, trap_cause[0], 5);
    CHECK(5, trap_tval[0], DIAG_UNMAPPED);
    CHECK(6, trap_cause[1], 5);
    CHECK(7, trap_tval[1], RV32_TIMER_BASE);
    CHECK(8, trap_cause[2], 7);
    CHECK(9, trap_tval[2], RV32_INPUT_BASE + RV32_INPUT_KEYS);
    CHECK(10, trap_cause[3], 7);
    CHECK(11, trap_tval[3], RV32_FB_BASE + RV32_FB_SIZE);
    rv32_puts("diag: faults ");
    rv32_put_udec(trap_count);
    rv32_putc('\n');

    /* 3. Display: the resolution, a frame drawn at every width, read back and hashed. */
    CHECK(12, mmio_read32(RV32_DISPLAY_BASE + RV32_DISPLAY_WIDTH), RV32_DISPLAY_COLUMNS);
    CHECK(13, mmio_read32(RV32_DISPLAY_BASE + RV32_DISPLAY_HEIGHT), RV32_DISPLAY_ROWS);
    CHECK(14, mmio_read32(RV32_DISPLAY_BASE + RV32_DISPLAY_FRAMES), 0);
    draw_frame_one();
    uint32_t hash = hash_frame();
    fold(hash);
    rv32_puts("diag: display ");
    rv32_put_hex32(hash);
    rv32_putc('\n');
    mmio_write32(RV32_DISPLAY_BASE + RV32_DISPLAY_PRESENT, 1); /* checkpoint: frame 1 */
    CHECK(15, mmio_read32(RV32_DISPLAY_BASE + RV32_DISPLAY_FRAMES), 1);

    /* 4. Input: frame 1's scripted events are now queued. */
    uint32_t events = 0;
    CHECK(16, input_word(RV32_INPUT_COUNT), 3);
    CHECK(17, input_word(RV32_INPUT_KEYS), 1u << RV32_KEY_SPACE); /* LEFT came and went; SPACE is held */
    for (uint32_t event = input_word(RV32_INPUT_EVENT); event != 0; event = input_word(RV32_INPUT_EVENT)) {
        fold(event);
        events++;
    }
    CHECK(18, events, 3);
    draw_frame_two();
    mmio_write32(RV32_DISPLAY_BASE + RV32_DISPLAY_PRESENT, 0); /* checkpoint: frame 2 */
    CHECK(19, mmio_read32(RV32_DISPLAY_BASE + RV32_DISPLAY_FRAMES), 2);
    CHECK(20, input_word(RV32_INPUT_COUNT), 1);
    CHECK(21, input_word(RV32_INPUT_KEYS), 0);
    for (uint32_t event = input_word(RV32_INPUT_EVENT); event != 0; event = input_word(RV32_INPUT_EVENT)) {
        fold(event);
        events++;
    }
    CHECK(22, events, 4);
    rv32_puts("diag: input ");
    rv32_put_udec(events);
    rv32_putc('\n');

    if (trap_count != 4) {
        return failed(98); /* a later, unexpected trap was resumed past silently */
    }
    if (checksum != DIAG_EXPECTED) {
        return failed(99);
    }
    rv32_puts("PASS ");
    rv32_put_hex32(checksum);
    rv32_putc('\n');
    return 0;
}
