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
/* Submit fails without mutation while busy. Wait resets on timeout (accepted
 * pixel writes survive); zero budget aborts immediately. */
int gpu_submit(const struct gpu_command *c);
int gpu_wait(uint32_t budget);
void gpu_reset(void);
#endif
