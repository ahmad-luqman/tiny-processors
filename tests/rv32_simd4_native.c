/* Test-only inspection bridge. The production access/tick functions do all work. */
#include "rv32_simd4.h"
#include <string.h>
static simd_device device;
void native_init(void) { memset(&device, 0, sizeof device); }
void native_reset(void) { simd_reset(&device); }
int native_access(uint32_t address, int width, int write, uint32_t *value)
{ return simd_access(&device, address, width, write != 0, value); }
void native_tick(int hold) { simd_tick(&device, hold != 0); }
uint32_t native_get(unsigned index)
{
    if (index < 16) return device.r[index / 4][index % 4];
    if (index < 20) return device.acc[index - 16];
    switch (index) {
    case 20: return device.pc;
    case 21: return device.loop;
    case 22: return device.state;
    case 23: return device.instruction;
    case 24: return device.lane;
    case 25: return device.command_tick;
    default: return 0;
    }
}
uint32_t native_data(unsigned index) { return device.data[index & 255]; }
