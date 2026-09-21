/* Noinline conversion calls keep the emitted integer/float transfers inspectable. */
#include <stdint.h>
#include "console.h"
volatile int32_t signed_input = -16777217;
volatile uint32_t unsigned_input = 0xffffffffu;
volatile float fractional_input = -3.75f;
volatile float outputs[2];
static const char pass[] = "PASS 4f800003\n";

__attribute__((noinline)) static float signed_float(int32_t x) { return (float)x; }
__attribute__((noinline)) static float unsigned_float(uint32_t x) { return (float)x; }

int main(void)
{
    union { float f; uint32_t u; } a, b;
    a.f = signed_float(signed_input); b.f = unsigned_float(unsigned_input);
    outputs[0] = a.f; outputs[1] = b.f;
    int32_t truncated = (int32_t)fractional_input;
    uint32_t positive = (uint32_t)(-fractional_input);
    uint32_t flags;
    __asm__ volatile ("csrr %0, fflags" : "=r"(flags) : "r"(truncated), "r"(positive) : "memory");
    if (a.u != 0xcb800000u || b.u != 0x4f800000u || truncated != -3 || positive != 3 || flags != 1) {
        rv32_puts("FAIL convert\n"); return 1;
    }
    rv32_puts(pass);
    return 0;
}
