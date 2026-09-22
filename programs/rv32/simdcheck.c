/* A2: CPU loads and checks every result, including overflowing MAC/MACU. */
#include "simd4.h"
#include "console.h"
#include "mmio.h"
#include "board.h"
#include "simd4_kernels.h"

static uint16_t data[256] = {0xa2};
static uint32_t bad_program[256];
static const uint16_t corners[] = {0x7fff, 0x8000, 0xffff, 1};

static int fail(unsigned code) { rv32_puts("FAIL "); rv32_put_udec(code); rv32_putc('\n'); return (int)code; }
static int32_t signed16(uint16_t x) { return (int32_t)x - ((x & 0x8000u) ? 65536 : 0); }

static bool run(const uint32_t *program)
{
    return simd4_load(program, data) && simd4_start(0) && simd4_wait(10000) == SIMD4_OK;
}

static bool counters(uint32_t instructions, uint32_t transfers)
{
    uint32_t cycles, stalls, observed_transfers, observed_instructions;
    return simd4_counter(0, &cycles) && simd4_counter(1, &stalls) &&
           simd4_counter(2, &observed_transfers) && simd4_counter(3, &observed_instructions) &&
           observed_transfers == transfers && observed_instructions == instructions &&
           cycles == 2 * instructions + transfers + stalls;
}

int main(void)
{
    simd4_reset();
    if (simd4_start(256)) return fail(1);
    for (unsigned i = 0; i < 32; i++) {
        data[i] = corners[i & 3];
        data[64+i] = corners[(i+1) & 3];
    }
    if (!run(vector_kernel) || !counters(51, 96)) return fail(2);
    for (unsigned i = 0; i < 32; i++) {
        uint16_t value;
        if (!simd4_read(128+i, &value) || value != (uint16_t)(data[i] + data[64+i])) return fail(3);
    }
    rv32_puts("vector OK\n");
    for (unsigned i = 0; i < 16; i++) {
        data[i] = i < 4 ? 0x7fff : corners[i & 3];
        data[64+i] = (i & 3) == 0 ? 0x7fff : corners[((i >> 2)+(i & 3)) & 3];
    }
    for (unsigned unsign = 0; unsign < 2; unsign++) {
        if (!run(unsign ? unsigned_matrix_kernel : matrix_kernel) || !counters(77, 144)) return fail(4);
        for (unsigned i = 0; i < 4; i++) for (unsigned j = 0; j < 4; j++) {
            uint32_t sum = 0;
            for (unsigned k = 0; k < 4; k++) {
                uint16_t a = data[i*4+k], b = data[64+k*4+j];
                sum += unsign ? (uint32_t)a * b : (uint32_t)(signed16(a) * signed16(b));
            }
            uint16_t value;
            if (!simd4_read(128+i*4+j, &value) || value != (uint16_t)(sum >> 16)) return fail(5);
            /* A literal anchor independent of the guest's multiply loop. */
            if (i == 0 && j == 0 && value != 0xfffc) return fail(6);
        }
    }
    rv32_puts("matrix signed/unsigned OK\n");
    bad_program[0] = 0xff000000u;
    if (!simd4_load(bad_program, data) || !simd4_start(0) || simd4_wait(100) != SIMD4_FAULT) return fail(7);
    if (mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS) != (RV32_SIMD4_DONE | RV32_SIMD4_FAULT)) return fail(8);
    if (!run(vector_kernel)) return fail(9); /* relaunch clears sticky fault */
    /* A nonzero bounded poll loop must abort work that is still busy. */
    bad_program[0] = 0x07ffffffu; /* SETLOOP 65535 */
    bad_program[1] = 0x08000001u; /* LOOP itself */
    if (!simd4_load(bad_program, data) || !simd4_start(0)) return fail(10);
    uint16_t busy_value;
    if (simd4_load(vector_kernel, data) || simd4_start(0) || simd4_read(0, &busy_value)) return fail(11);
    if (simd4_wait(8) != SIMD4_TIMEOUT) return fail(12);
    if (mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS) != 0 || !run(vector_kernel)) return fail(13);
    uint32_t invalid_counter = 0xa2;
    if (simd4_counter(4, &invalid_counter) || invalid_counter != 0xa2) return fail(14);
    rv32_puts("recovery OK\nPASS A2\n");
    return 0;
}
