/* N1 digit classifier: preprocessing and the integer model, free of any device.
 *
 * Both inference paths share everything here. The accelerator computes the two
 * dot products and nothing else; the bias, rounding, ReLU, saturation and output
 * selection below are the CPU's job on either path, so a hardware result and a
 * software result can only differ in the dot products themselves.
 *
 * The arithmetic is deliberately written in unsigned types. Accumulators wrap
 * modulo 2^32 exactly as the engine's do, shifts are never applied to a negative
 * value, and the one place a signed result is needed converts explicitly. The
 * exporter proves the true values fit in int32, so no wrap ever loses data.
 */
#ifndef RV32_DIGIT_MODEL_H
#define RV32_DIGIT_MODEL_H
#include <stdint.h>
#include "digit_weights.h"

/* Centre the ink by its bounding box, then average each 2x2 block: 784 -> 196.
 * A blank canvas produces 196 zeros. This is the only preprocessing there is;
 * tools/digit_data.py defines the same function for the oracle. */
void digit_prepare(const uint8_t canvas[DIGIT_PIXELS], uint8_t x[DIGIT_INPUTS]);

/* Bias, round half up, ReLU and saturate the four-lane accumulators of layer 1. */
void digit_requantize(const uint32_t accumulators[DIGIT_HIDDEN], uint8_t hidden[DIGIT_HIDDEN]);

/* The whole model on the CPU: the reference the accelerator must reproduce. */
void digit_infer_cpu(const uint8_t x[DIGIT_INPUTS], int32_t logits[DIGIT_CLASSES]);

/* The predicted class, lowest index on a tie, and the non-negative margin over
 * the runner-up. The margin is unsigned because the difference of two valid
 * int32 logits can overflow a signed subtraction. */
uint32_t digit_argmax(const int32_t logits[DIGIT_CLASSES], uint32_t *margin);

/* Reinterpret a 32-bit pattern as two's complement without relying on the
 * implementation-defined conversion. */
static inline int32_t digit_signed(uint32_t value)
{
    return (value & 0x80000000u) ? (int32_t)(value - 0x80000000u) - 2147483647 - 1 : (int32_t)value;
}

/* acc += weight * value for an int8 weight and a 0..255 input, in eight shift-add
 * steps. The runtime's `*` helper iterates over the bit length of its second
 * operand, which is 32 for a negative weight; this loop is bounded by the input. */
static inline uint32_t digit_mac(uint32_t accumulator, int32_t weight, uint32_t value)
{
    uint32_t shifted = (uint32_t)weight;
    while (value) {
        if (value & 1u) accumulator += shifted;
        shifted += shifted;
        value >>= 1;
    }
    return accumulator;
}
#endif
