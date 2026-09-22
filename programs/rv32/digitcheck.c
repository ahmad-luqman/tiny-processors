/* N1: the guest classifies MNIST canvases on the accelerator and proves its
 * results are the ones the independent oracle computed, then proves the
 * accelerator and the CPU agree bit for bit, then recovers from a fault and a
 * poll-budget timeout. Exit codes start at 20; 1-13 belong to earlier
 * diagnostics.
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
/* The data image loaded before the deliberately broken launches. Slot 0 carries a
 * nonzero poison value, so a later inference that forgot to rewrite its operands
 * would produce visibly wrong logits instead of quietly reading a zero. The
 * initializer also gives the image a non-empty .data section, which the image
 * checker requires of every firmware. */
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
        digit_hw_counters(&counters);
        if (!counters_agree(&counters)) return fail(24);
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

    /* A launch that never finishes: the bounded poll budget must abort it, the
     * reset must clear status, and the next inference must still be exact. */
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
    rv32_puts("recovery OK\nPASS N1\n");
    return 0;
}
