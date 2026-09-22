/* N1: the guest classifies MNIST canvases on the accelerator and proves its
 * results are the ones the independent oracle computed, then proves the
 * accelerator and the CPU agree bit for bit, then recovers from a fault and a
 * poll-budget timeout. Exit codes start at 20; 1-14 belong to earlier
 * diagnostics and 80-83 to the capstone.
 */
#include "digit_hw.h"
#include "simd4.h"
#include "console.h"
#include "mmio.h"
#include "board.h"
#include "digit_weights.h"   /* the launch counts and the layer-2 block this checks */
#include "digit_check.h"
#include "digit_kernels.h"

static uint32_t bad_program[256];
/* The data image loaded before the deliberately broken launches, so those launches
 * start from a known state rather than from whatever the previous inference left.
 * Note this does not survive a later digit_hw_init: that deliberately loads a blank
 * data image, because power-up contents are unspecified. The initializer also gives
 * the firmware a non-empty .data section, which the image checker requires. */
static uint16_t poison[256] = {0xdead};

static int fail(unsigned code) { rv32_puts("FAIL "); rv32_put_udec(code); rv32_putc('\n'); return (int)code; }

/* Transfers and instructions are fixed by the kernel shape and must match the
 * builder's prediction on every backend. Cycles and stalls are device time and
 * differ between the emulator and the RTL, so they are only checked for internal
 * consistency, never printed or compared across backends. */
static bool counters_agree(const struct digit_counters *c)
{
    uint32_t transfers = DIGIT_L1_LAUNCHES * DIGIT_LAYER1_TRANSFERS +
                         DIGIT_L2_LAUNCHES * DIGIT_LAYER2_TRANSFERS;
    uint32_t instructions = DIGIT_L1_LAUNCHES * DIGIT_LAYER1_INSTRUCTIONS +
                            DIGIT_L2_LAUNCHES * DIGIT_LAYER2_INSTRUCTIONS;
    return c->launches == DIGIT_L1_LAUNCHES + DIGIT_L2_LAUNCHES &&
           c->transfers == transfers && c->instructions == instructions &&
           c->cycles == 2 * instructions + transfers + c->stalls;
}

int main(void)
{
    if (digit_hw_init() != DIGIT_OK) return fail(20);

    for (unsigned image = 0; image < DIGIT_CHECK_IMAGES; image++) {
        uint8_t x[DIGIT_INPUTS];
        int32_t logits[DIGIT_CLASSES];
        digit_prepare(digit_check_canvas[image], x);
        if (digit_hw_infer(x, logits) != DIGIT_OK) return fail(21);
        for (unsigned c = 0; c < DIGIT_CLASSES; c++)
            if (logits[c] != digit_check_logits[image][c]) return fail(22);
        uint32_t margin;
        uint32_t predicted = digit_argmax(logits, &margin);
        if (predicted != digit_check_expected[image]) return fail(23);
        struct digit_counters counters;
        if (!digit_hw_counters(&counters) || !counters_agree(&counters)) return fail(24);
        /* The first two images also run the software model, so the comparison
         * covers the arithmetic rather than two readings of one computation. */
        if (image < 2) {
            int32_t software[DIGIT_CLASSES];
            digit_infer_cpu(x, software);
            for (unsigned c = 0; c < DIGIT_CLASSES; c++)
                if (software[c] != logits[c]) return fail(25);
        }
        rv32_puts("digit ");
        rv32_put_udec(image);
        rv32_puts(" pred ");
        rv32_put_udec(predicted);
        rv32_puts(" margin ");
        rv32_put_udec(margin);
        rv32_puts(" label ");
        rv32_put_udec(digit_check_label[image]);
        rv32_putc('\n');
    }
    rv32_puts("inference OK\n");

    /* A launch whose first instruction is an illegal opcode must finish with
     * FAULT, leave the sticky bits set, and not prevent the next inference. */
    bad_program[0] = 0xff000000u;
    if (!simd4_load(bad_program, poison) || !simd4_start(0)) return fail(26);
    if (simd4_wait(100) != SIMD4_FAULT) return fail(27);
    if (mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS) != (RV32_SIMD4_DONE | RV32_SIMD4_FAULT))
        return fail(28);

    /* The bank was overwritten, so the driver has to be told to reload it. */
    digit_hw_forget();
    uint8_t x[DIGIT_INPUTS];
    int32_t logits[DIGIT_CLASSES];
    digit_prepare(digit_check_canvas[0], x);
    if (digit_hw_infer(x, logits) != DIGIT_OK) return fail(29);
    for (unsigned c = 0; c < DIGIT_CLASSES; c++)
        if (logits[c] != digit_check_logits[0][c]) return fail(30);

    /* A launch that outlives the poll budget. It is not infinite: SETLOOP 65535 then
     * LOOP retires about 65,535 times and falls through to the zero word, which is
     * HLT. What matters is that it is still running when the budget expires, so the
     * bounded wait must abort it, the reset must clear status, and the next
     * inference must still be exact. */
    bad_program[0] = 0x07ffffffu;    /* SETLOOP 65535 */
    bad_program[1] = 0x08000001u;    /* LOOP to itself */
    if (!simd4_load(bad_program, poison) || !simd4_start(0)) return fail(31);
    uint16_t refused;
    if (simd4_read(0, &refused) || simd4_write(0, 1) ||
        simd4_write_bytes(0, x, 4) || simd4_write_signed(0, digit_w2[0], 4))
        return fail(32);             /* every buffer access is refused while busy */
    if (simd4_wait(8) != SIMD4_TIMEOUT) return fail(33);
    if (mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS) != 0) return fail(34);

    digit_hw_forget();
    if (digit_hw_infer(x, logits) != DIGIT_OK) return fail(35);
    for (unsigned c = 0; c < DIGIT_CLASSES; c++)
        if (logits[c] != digit_check_logits[0][c]) return fail(36);

    /* Out-of-range driver calls are refused without touching the device. */
    if (simd4_write(256, 0) || simd4_write_bytes(250, x, 8) || simd4_write_signed(255, digit_w2[0], 2))
        return fail(37);
    /* And the accept side of the same bound: a block that ends exactly at the last
     * slot must go through, or a future kernel that fills the window is refused. */
    if (!simd4_write(255, 0x1234) || !simd4_write_bytes(248, x, 8) ||
        !simd4_write_signed(254, digit_w2[0], 2)) return fail(38);
    uint16_t edge;
    if (!simd4_read(255, &edge) || edge != (uint16_t)(int16_t)digit_w2[0][1]) return fail(39);

    /* A fault raised inside digit_hw_infer, rather than through the raw driver:
     * the entry word is replaced so the first launch faults. The driver must report
     * it, refuse to hand out counters as a finished inference, and reload the bank
     * by itself on the next call. */
    digit_hw_forget();
    if (digit_hw_infer(x, logits) != DIGIT_OK) return fail(40);
    mmio_write32(RV32_SIMD4_PROGRAM + 4 * DIGIT_LAYER1_ENTRY, 0xff000000u);
    if (digit_hw_infer(x, logits) != DIGIT_FAULT) return fail(41);
    struct digit_counters partial;
    if (digit_hw_counters(&partial) || partial.launches != 0) return fail(42);
    if (digit_hw_infer(x, logits) != DIGIT_OK) return fail(43);   /* reloaded without being told */
    for (uint32_t c = 0; c < DIGIT_CLASSES; c++)
        if (logits[c] != digit_check_logits[0][c]) return fail(44);
    rv32_puts("recovery OK\nPASS N1\n");
    return 0;
}
