#include "digit_hw.h"
#include "simd4.h"
#include "digit_kernels.h"

/* Poll budget. One poll is five CPU instructions, and the emulator advances the
 * device one tick per instruction, so a K=49 launch (1,008 device ticks with no
 * waits) needs at least 202 polls there; on RTL a poll is about 21 clocks and even
 * three wait cycles per transfer leaves the requirement near 105. 2048 is roughly
 * ten times the binding case. A timeout resets the device, so it is reported, never
 * retried silently. */
#define DIGIT_POLLS 2048u

/* Layer 2 reuses the slots layer 1 wrote, which is safe only because the layers
 * run in order: every layer-1 launch has completed and been read back before the
 * first layer-2 launch overwrites slot 0. */
#define L1_X 0u
#define L1_W DIGIT_LAYER1_DEPTH
#define L1_LO (5u * DIGIT_LAYER1_DEPTH)
#define L2_X 0u
#define L2_W DIGIT_LAYER2_DEPTH
#define L2_LO (5u * DIGIT_LAYER2_DEPTH)

static uint8_t ready;
static struct digit_counters counters;

void digit_hw_forget(void) { ready = 0; }

void digit_hw_counters(struct digit_counters *out)
{
    out->launches = counters.launches;
    out->cycles = counters.cycles;
    out->stalls = counters.stalls;
    out->transfers = counters.transfers;
    out->instructions = counters.instructions;
}

enum digit_status digit_hw_init(void)
{
    if (ready) return DIGIT_OK;
    /* Power-up contents are unspecified, so start from a known data image rather
     * than trusting the harness to zero the memories. */
    static const uint16_t blank[256];
    simd4_reset();
    if (!simd4_load(digit_kernel_bank, blank)) return DIGIT_REFUSED;
    ready = 1;
    return DIGIT_OK;
}

/* One launch: the operands are already in place, so start, poll, and collect the
 * four accumulators from their low and high halves. */
static enum digit_status run(unsigned entry, unsigned low_slot, uint32_t out[DIGIT_LANES])
{
    if (!simd4_start(entry)) return DIGIT_REFUSED;
    enum simd4_result result = simd4_wait(DIGIT_POLLS);
    if (result == SIMD4_FAULT) return DIGIT_FAULT;
    if (result == SIMD4_TIMEOUT) { ready = 0; return DIGIT_TIMEOUT; }
    for (unsigned lane = 0; lane < DIGIT_LANES; lane++) {
        uint16_t low, high;
        if (!simd4_read(low_slot + lane, &low)) return DIGIT_REFUSED;
        if (!simd4_read(low_slot + DIGIT_LANES + lane, &high)) return DIGIT_REFUSED;
        out[lane] = (uint32_t)low | ((uint32_t)high << 16);
    }
    uint32_t value;
    counters.launches++;
    if (simd4_counter(0, &value)) counters.cycles += value;
    if (simd4_counter(1, &value)) counters.stalls += value;
    if (simd4_counter(2, &value)) counters.transfers += value;
    if (simd4_counter(3, &value)) counters.instructions += value;
    return DIGIT_OK;
}

enum digit_status digit_hw_infer(const uint8_t x[DIGIT_INPUTS], int32_t logits[DIGIT_CLASSES])
{
    enum digit_status status = digit_hw_init();
    if (status != DIGIT_OK) return status;
    counters.launches = counters.cycles = counters.stalls = 0;
    counters.transfers = counters.instructions = 0;

    uint32_t accumulators[DIGIT_HIDDEN];
    for (unsigned n = 0; n < DIGIT_HIDDEN; n++) accumulators[n] = 0;

    /* Chunk-major: each 49-value input chunk is written once and then reused by
     * the eight launches that need it, instead of once per launch. */
    const uint8_t *chunk_inputs = x;
    for (unsigned chunk = 0, launch = 0; chunk < DIGIT_L1_CHUNKS; chunk++) {
        if (!simd4_write_bytes(L1_X, chunk_inputs, DIGIT_LAYER1_DEPTH)) return DIGIT_REFUSED;
        for (unsigned group = 0; group < DIGIT_L1_GROUPS; group++, launch++) {
            if (!simd4_write_signed(L1_W, digit_w1[launch], DIGIT_L1_BLOCK)) return DIGIT_REFUSED;
            uint32_t lanes[DIGIT_LANES];
            status = run(DIGIT_LAYER1_ENTRY, L1_LO, lanes);
            if (status != DIGIT_OK) return status;
            unsigned neuron = group * DIGIT_LANES;
            for (unsigned lane = 0; lane < DIGIT_LANES; lane++)
                accumulators[neuron + lane] += lanes[lane];
        }
        chunk_inputs += DIGIT_LAYER1_DEPTH;
    }

    uint8_t hidden[DIGIT_HIDDEN];
    digit_requantize(accumulators, hidden);

    if (!simd4_write_bytes(L2_X, hidden, DIGIT_HIDDEN)) return DIGIT_REFUSED;
    for (unsigned launch = 0, class_index = 0; launch < DIGIT_L2_LAUNCHES; launch++) {
        if (!simd4_write_signed(L2_W, digit_w2[launch], DIGIT_L2_BLOCK)) return DIGIT_REFUSED;
        uint32_t lanes[DIGIT_LANES];
        status = run(DIGIT_LAYER2_ENTRY, L2_LO, lanes);
        if (status != DIGIT_OK) return status;
        for (unsigned lane = 0; lane < DIGIT_LANES; lane++, class_index++)
            if (class_index < DIGIT_CLASSES)
                logits[class_index] = digit_signed(lanes[lane] + (uint32_t)digit_b2[class_index]);
    }
    return DIGIT_OK;
}
