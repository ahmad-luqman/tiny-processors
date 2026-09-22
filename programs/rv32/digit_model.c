#include "digit_model.h"

/* Centring runs on the canvas before pooling because a digit drawn with the
 * arrow keys sits wherever the cursor happened to be, while the training images
 * were centred. Both go through this function, so the model sees one
 * distribution. Row and column bases advance by addition: a multiply would be a
 * 32-step software loop here. */
void digit_prepare(const uint8_t canvas[DIGIT_PIXELS], uint8_t x[DIGIT_INPUTS])
{
    uint32_t top = DIGIT_SIDE, bottom = 0, left = DIGIT_SIDE, right = 0, row = 0;
    for (uint32_t r = 0; r < DIGIT_SIDE; r++, row += DIGIT_SIDE) {
        for (uint32_t c = 0; c < DIGIT_SIDE; c++) {
            if (!canvas[row + c]) continue;
            if (r < top) top = r;
            if (r > bottom) bottom = r;
            if (c < left) left = c;
            if (c > right) right = c;
        }
    }
    uint8_t centred[DIGIT_PIXELS];
    for (uint32_t i = 0; i < DIGIT_PIXELS; i++) centred[i] = 0;
    if (top < DIGIT_SIDE) {                       /* there is ink to centre */
        uint32_t down = ((DIGIT_SIDE - (bottom - top + 1)) >> 1) - top;
        uint32_t across = ((DIGIT_SIDE - (right - left + 1)) >> 1) - left;
        /* down and across are taken modulo 2^32 when the shift is negative; adding
         * them back to an in-range row or column lands in range again. */
        uint32_t source = 0, destination = 0;
        for (uint32_t r = 0; r < top; r++) source += DIGIT_SIDE;
        for (uint32_t r = top + down; r; r--) destination += DIGIT_SIDE;
        for (uint32_t r = top; r <= bottom; r++, source += DIGIT_SIDE, destination += DIGIT_SIDE)
            for (uint32_t c = left; c <= right; c++)
                centred[destination + c + across] = canvas[source + c];
    }
    uint32_t upper = 0, out = 0;
    for (uint32_t i = 0; i < DIGIT_POOLED; i++, upper += 2 * DIGIT_SIDE) {
        uint32_t lower = upper + DIGIT_SIDE;
        for (uint32_t j = 0, c = 0; j < DIGIT_POOLED; j++, c += 2)
            x[out++] = (uint8_t)((centred[upper + c] + centred[upper + c + 1] +
                                  centred[lower + c] + centred[lower + c + 1]) >> 2);
    }
}

void digit_requantize(const uint32_t accumulators[DIGIT_HIDDEN], uint8_t hidden[DIGIT_HIDDEN])
{
    for (uint32_t n = 0; n < DIGIT_HIDDEN; n++) {
        uint32_t total = accumulators[n] + (uint32_t)digit_b1[n] + DIGIT_ROUND;
        if (total & 0x80000000u) {                /* ReLU by sign test, never shift it */
            hidden[n] = 0;
            continue;
        }
        total >>= DIGIT_SHIFT;
        hidden[n] = (uint8_t)(total > DIGIT_HIDDEN_MAX ? DIGIT_HIDDEN_MAX : total);
    }
}

/* Layer 2 from the hidden vector, shared by both paths' CPU half. */
static void logits_from_hidden(const uint8_t hidden[DIGIT_HIDDEN], int32_t logits[DIGIT_CLASSES])
{
    for (uint32_t launch = 0, class_index = 0; launch < DIGIT_L2_LAUNCHES; launch++) {
        const int8_t *weights = digit_w2[launch];
        for (uint32_t lane = 0; lane < DIGIT_LANES; lane++, class_index++) {
            uint32_t accumulator = 0;
            for (uint32_t n = 0; n < DIGIT_HIDDEN; n++)
                accumulator = digit_mac(accumulator, *weights++, hidden[n]);
            if (class_index < DIGIT_CLASSES)
                logits[class_index] = digit_signed(accumulator + (uint32_t)digit_b2[class_index]);
        }
    }
}

/* The weights are stored in launch order, so this walks them with one moving
 * pointer in exactly the order the accelerator launches, which keeps the two
 * paths from drifting apart through a layout mistake. */
void digit_infer_cpu(const uint8_t x[DIGIT_INPUTS], int32_t logits[DIGIT_CLASSES])
{
    uint32_t accumulators[DIGIT_HIDDEN];
    for (uint32_t n = 0; n < DIGIT_HIDDEN; n++) accumulators[n] = 0;
    const uint8_t *chunk_inputs = x;
    for (uint32_t chunk = 0, launch = 0; chunk < DIGIT_L1_CHUNKS; chunk++) {
        for (uint32_t group = 0; group < DIGIT_L1_GROUPS; group++, launch++) {
            const int8_t *weights = digit_w1[launch];
            uint32_t neuron = group * DIGIT_LANES;
            for (uint32_t lane = 0; lane < DIGIT_LANES; lane++) {
                uint32_t accumulator = accumulators[neuron + lane];
                for (uint32_t j = 0; j < DIGIT_L1_DEPTH; j++)
                    accumulator = digit_mac(accumulator, *weights++, chunk_inputs[j]);
                accumulators[neuron + lane] = accumulator;
            }
        }
        chunk_inputs += DIGIT_L1_DEPTH;
    }
    uint8_t hidden[DIGIT_HIDDEN];
    digit_requantize(accumulators, hidden);
    logits_from_hidden(hidden, logits);
}

uint32_t digit_argmax(const int32_t logits[DIGIT_CLASSES], uint32_t *margin)
{
    /* Compare as unsigned with the sign bit flipped: the order is the signed
     * order, and a strict > keeps the lowest index on a tie. */
    uint32_t best = 0, best_key = (uint32_t)logits[0] ^ 0x80000000u, second_key = 0;
    for (uint32_t i = 1; i < DIGIT_CLASSES; i++) {
        uint32_t key = (uint32_t)logits[i] ^ 0x80000000u;
        if (key > best_key) { best_key = key; best = i; }
    }
    for (uint32_t i = 0, seen = 0; i < DIGIT_CLASSES; i++) {
        uint32_t key = (uint32_t)logits[i] ^ 0x80000000u;
        if (i == best) continue;
        if (!seen || key > second_key) { second_key = key; seen = 1; }
    }
    /* Both are valid int32 values and the first is the larger, but their signed
     * difference can still overflow, so subtract as unsigned. */
    *margin = (uint32_t)logits[best] - (uint32_t)digit_signed(second_key ^ 0x80000000u);
    return best;
}

/* Accessors so host tests never need this file's struct or array layout. */
#ifdef RV32_DIGIT_NATIVE
uint32_t digit_native_inputs(void) { return DIGIT_INPUTS; }
uint32_t digit_native_pixels(void) { return DIGIT_PIXELS; }
uint32_t digit_native_classes(void) { return DIGIT_CLASSES; }
uint32_t digit_native_hidden(void) { return DIGIT_HIDDEN; }
uint32_t digit_native_shift(void) { return DIGIT_SHIFT; }
uint32_t digit_native_hidden_max(void) { return DIGIT_HIDDEN_MAX; }
void digit_native_hidden_vector(const uint8_t *x, uint8_t *hidden)
{
    uint32_t accumulators[DIGIT_HIDDEN];
    for (uint32_t n = 0; n < DIGIT_HIDDEN; n++) accumulators[n] = 0;
    const uint8_t *chunk_inputs = x;
    for (uint32_t chunk = 0, launch = 0; chunk < DIGIT_L1_CHUNKS; chunk++) {
        for (uint32_t group = 0; group < DIGIT_L1_GROUPS; group++, launch++) {
            const int8_t *weights = digit_w1[launch];
            uint32_t neuron = group * DIGIT_LANES;
            for (uint32_t lane = 0; lane < DIGIT_LANES; lane++) {
                uint32_t accumulator = accumulators[neuron + lane];
                for (uint32_t j = 0; j < DIGIT_L1_DEPTH; j++)
                    accumulator = digit_mac(accumulator, *weights++, chunk_inputs[j]);
                accumulators[neuron + lane] = accumulator;
            }
        }
        chunk_inputs += DIGIT_L1_DEPTH;
    }
    digit_requantize(accumulators, hidden);
}
#endif
