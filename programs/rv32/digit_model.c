#include "digit_model.h"
#include "digit_weights.h"

#define DIGIT_TARGET 20u                  /* MNIST fits every digit to a 20x20 box */

/* The ink's bounding box. Returns 0 for a blank canvas. */
static uint32_t ink_box(const uint8_t canvas[DIGIT_PIXELS], uint32_t *top, uint32_t *bottom,
                        uint32_t *left, uint32_t *right)
{
    *top = DIGIT_SIDE; *bottom = 0; *left = DIGIT_SIDE; *right = 0;
    for (uint32_t r = 0, row = 0; r < DIGIT_SIDE; r++, row += DIGIT_SIDE) {
        for (uint32_t c = 0; c < DIGIT_SIDE; c++) {
            if (!canvas[row + c]) continue;
            if (r < *top) *top = r;
            if (r > *bottom) *bottom = r;
            if (c < *left) *left = c;
            if (c > *right) *right = c;
        }
    }
    return *top < DIGIT_SIDE;
}

/* offset[d] = floor((2d + 1) * extent / (2 * size)) for d < size: the source pixel
 * under the centre of destination pixel d. RV32I has no divide and both operands
 * are runtime values, so the quotient is carried from one d to the next: the
 * numerator grows by 2 * extent per step, and the quotient catches up by
 * subtraction, at most `extent` times in all. */
static void axis_offsets(uint8_t offset[DIGIT_TARGET], uint32_t extent, uint32_t size)
{
    uint32_t numerator = extent, quotient = 0, divisor = size << 1;
    for (uint32_t d = 0; d < size; d++, numerator += extent << 1) {
        while (numerator >= divisor) { numerator -= divisor; quotient++; }
        offset[d] = (uint8_t)quotient;
    }
}

/* Resize the ink box so its longer side is 20 pixels, keeping the aspect ratio
 * to within rounding, into the top left of `out`. Every MNIST digit was fit to a
 * 20x20 box, so this is what lets a drawing of any size look like the training
 * data. Nearest neighbour copies values, so a binary drawing stays binary, and
 * sampling pixel centres (not left edges) keeps a stroke on the last row or
 * column, where the brush clips to one cell, from vanishing in a shrink. */
static void resize(const uint8_t canvas[DIGIT_PIXELS], uint8_t out[DIGIT_PIXELS])
{
    for (uint32_t i = 0; i < DIGIT_PIXELS; i++) out[i] = 0;
    uint32_t top, bottom, left, right;
    if (!ink_box(canvas, &top, &bottom, &left, &right)) return;
    uint32_t height = bottom - top + 1, width = right - left + 1;
    uint32_t tall = height >= width;
    uint32_t longer = tall ? height : width, shorter = tall ? width : height;
    /* n = (shorter * 20 + longer / 2) / longer, by subtraction: the quotient is
     * at most 20, so the loop is short and needs no divide. */
    uint32_t remainder = shorter * DIGIT_TARGET + (longer >> 1), n = 0;
    while (remainder >= longer) { remainder -= longer; n++; }
    if (n == 0) n = 1;
    uint32_t rows = tall ? DIGIT_TARGET : n, columns = tall ? n : DIGIT_TARGET;
    uint8_t row_offset[DIGIT_TARGET], column_offset[DIGIT_TARGET];
    axis_offsets(row_offset, height, rows);
    axis_offsets(column_offset, width, columns);
    for (uint32_t r = 0, destination = 0; r < rows; r++, destination += DIGIT_SIDE) {
        const uint8_t *source = canvas + (top + row_offset[r]) * DIGIT_SIDE + left;
        for (uint32_t c = 0; c < columns; c++)
            out[destination + c] = source[column_offset[c]];
    }
}

/* Resize, then centre, then pool. Centring runs on the resized canvas rather than
 * being folded into the resize because shrinking can skip the last source row or
 * column, leaving the resized ink box smaller than the box the resize wrote.
 * Centring matters because a digit drawn with the arrow keys sits wherever the
 * cursor happened to be, while the training images were centred. Training and
 * both runtime paths go through this contract, so the model sees one distribution.
 *
 * Multiplying by DIGIT_SIDE is fine here: it is a compile-time constant, so the
 * compiler turns it into a pair of shifts rather than calling the runtime's
 * 32-step helper. Only a runtime operand would need avoiding, which is what
 * digit_mac does. */
void digit_prepare(const uint8_t canvas[DIGIT_PIXELS], uint8_t x[DIGIT_INPUTS])
{
    uint8_t resized[DIGIT_PIXELS];
    resize(canvas, resized);
    uint32_t top, bottom, left, right;
    uint8_t centred[DIGIT_PIXELS];
    for (uint32_t i = 0; i < DIGIT_PIXELS; i++) centred[i] = 0;
    if (ink_box(resized, &top, &bottom, &left, &right)) {
        uint32_t down = ((DIGIT_SIDE - (bottom - top + 1)) >> 1) - top;
        uint32_t across = ((DIGIT_SIDE - (right - left + 1)) >> 1) - left;
        /* The halved leftovers are never negative, but subtracting `top` and `left`
         * can be, so `down` and `across` wrap modulo 2^32. Adding a wrapped value
         * back to an in-range row or column lands in range again, which is why
         * these stay unsigned rather than becoming signed offsets. */
        uint32_t source = top * DIGIT_SIDE, destination = (top + down) * DIGIT_SIDE;
        for (uint32_t r = top; r <= bottom; r++, source += DIGIT_SIDE, destination += DIGIT_SIDE)
            for (uint32_t c = left; c <= right; c++)
                centred[destination + c + across] = resized[source + c];
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
static void hidden_from_inputs(const uint8_t x[DIGIT_INPUTS], uint8_t hidden[DIGIT_HIDDEN])
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

void digit_infer_cpu(const uint8_t x[DIGIT_INPUTS], int32_t logits[DIGIT_CLASSES])
{
    uint8_t hidden[DIGIT_HIDDEN];
    hidden_from_inputs(x, hidden);
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

/* Accessors so host tests read the generated sizes and shift constants from the
 * same header the firmware compiles against, instead of restating them. */
#ifdef RV32_DIGIT_NATIVE
uint32_t digit_native_inputs(void) { return DIGIT_INPUTS; }
uint32_t digit_native_pixels(void) { return DIGIT_PIXELS; }
uint32_t digit_native_classes(void) { return DIGIT_CLASSES; }
uint32_t digit_native_hidden(void) { return DIGIT_HIDDEN; }
uint32_t digit_native_shift(void) { return DIGIT_SHIFT; }
uint32_t digit_native_hidden_max(void) { return DIGIT_HIDDEN_MAX; }
void digit_native_hidden_vector(const uint8_t *x, uint8_t *hidden) { hidden_from_inputs(x, hidden); }
#endif
