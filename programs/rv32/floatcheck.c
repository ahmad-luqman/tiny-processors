#include <stdint.h>
#include "console.h"

float float_work(float a, float b, float c);
/* Volatile initialized memory is loaded at runtime on both backends. */
volatile float float_inputs[] = {1.5f, 2.0f, 4.0f, -3.25f, 0.5f, -1.0f, 8192.0f, 4.0f, 16.0f};
volatile uint32_t float_output[3];
static const uint32_t expected[] = {0x40000000u, 0xc0000000u, 0x40800000u};

int main(void)
{
    uint32_t checksum = 0;
    for (unsigned i = 0; i < 3; ++i) {
        union { float f; uint32_t u; } result;
        result.f = float_work(float_inputs[i*3], float_inputs[i*3+1], float_inputs[i*3+2]);
        float_output[i] = result.u;
        if (result.u != expected[i]) { rv32_puts("FAIL float\n"); return 1; }
        checksum ^= result.u;
    }
    rv32_puts("PASS "); rv32_put_hex32(checksum); rv32_putc('\n');
    return 0;
}
