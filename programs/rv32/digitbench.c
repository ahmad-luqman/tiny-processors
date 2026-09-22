/* N1 cost measurement: the same classifications on the CPU and on the
 * accelerator. Built twice with different DIGIT_BENCH_COUNT values so the
 * difference cancels startup; it covers the extra inferences between the two
 * counts, and tools/rv32_digit_bench.py divides it down to one.
 *
 * The sink is printed as fixed-width hex rather than decimal on purpose. The two
 * builds reach different sink values, and rv32_put_udec costs a software divide
 * and modulo per decimal digit, so a five-digit total did measurably more work
 * than a four-digit one and that difference landed inside the subtraction.
 * rv32_put_hex32 always writes eight digits using shifts alone, so the two runs
 * now do identical console work and it cancels as the method assumes.
 * Nothing here is compared across backends; it is a measurement, not a test.
 */
#include "digit_hw.h"
#include "console.h"
#include "digit_check.h"

#ifndef DIGIT_BENCH_COUNT
#define DIGIT_BENCH_COUNT 1u
#endif

/* Keeps .data non-empty, which the image checker requires, and keeps the
 * accumulated result somewhere the optimizer cannot discard. */
static uint32_t sink = 1;

int main(void)
{
    for (uint32_t i = 0; i < DIGIT_BENCH_COUNT; i++) {
        uint8_t x[DIGIT_INPUTS];
        int32_t logits[DIGIT_CLASSES];
        digit_prepare(digit_check_canvas[i % DIGIT_CHECK_IMAGES], x);
#ifdef DIGIT_BENCH_CPU
        digit_infer_cpu(x, logits);
#else
        if (digit_hw_infer(x, logits) != DIGIT_OK) return 1;
#endif
        uint32_t margin;
        sink += digit_argmax(logits, &margin) + margin;
    }
    rv32_puts("bench ");
    rv32_put_hex32(sink);
    rv32_putc('\n');
    return 0;
}
