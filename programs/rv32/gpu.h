/* G1 public contract; docs/rv32-gfx.md fixes coverage and ownership. */
#ifndef RV32_GPU_H
#define RV32_GPU_H
#include <stdint.h>
#define GPU_BASE 0x20007000u
#define GPU_COMMAND 0x00u
#define GPU_STATUS 0x04u
#define GPU_ERROR 0x08u
#define GPU_CYCLES 0x0cu
#define GPU_STALLS 0x10u
#define GPU_READS 0x14u
#define GPU_WRITES 0x18u
#define GPU_PARAMS 0x40u
#define GPU_START 1u
#define GPU_RESET 2u
/* Status is a whole-value enumeration, not combinable flags. */
#define GPU_IDLE 0u
#define GPU_INVALID 1u
#define GPU_INTERNAL 2u
#define GPU_BUSY 1u
#define GPU_DONE 2u
#define GPU_FAULT 4u
#define GPU_FILL 1u
#define GPU_BLIT 2u
#define GPU_LINE 3u
#define GPU_TRIANGLE 4u
/* Parameter words, all 32 bits; coordinates are signed two's complement. */
enum { GP_OP, GP_COLOR, GP_X0, GP_Y0, GP_X1, GP_Y1, GP_X2, GP_Y2,
       GP_W, GP_H, GP_SRC, GP_STRIDE, GP_SW, GP_SH, GP_SX, GP_SY, GP_COUNT };
struct gpu_command { uint32_t p[GP_COUNT]; };
void gpu_command_init(struct gpu_command *c, uint32_t op, uint32_t color);
/* Software reference: RAM sources are supplied as a host/guest pointer separately.
 * Caller supplies valid bounded parameters; destination is exactly 320x240. */
void gpu_reference(uint8_t *fb, const uint8_t *source, const struct gpu_command *c);
/* Submit fails without mutation while BUSY. Status values are returned by wait;
 * GPU_TIMEOUT is driver-only. The budget counts MMIO polls, not device ticks.
 * FAULT/IDLE return without reset; a subsequent submit is legal. Timeout saves
 * diagnostics then resets; accepted pixel writes survive, even at budget zero. */
#define GPU_TIMEOUT 8u
struct gpu_result { uint32_t status, reason, cycles, stalls, reads, writes; };
int gpu_submit(const struct gpu_command *c);
uint32_t gpu_wait(uint32_t budget);
/* Last failed wait's snapshot remains available after timeout reset. */
const struct gpu_result *gpu_last_result(void);
/* Submit and wait; print op/status/reason/counters on any failure. */
int gpu_run(const struct gpu_command *c, uint32_t budget);
/* Legal while BUSY; clears all parameters/status/counters and cancels unaccepted
 * transfers. A raw START must rewrite parameters after RESET. */
void gpu_reset(void);
#endif
