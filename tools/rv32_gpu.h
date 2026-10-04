#ifndef RV32_GPU_DEVICE_H
#define RV32_GPU_DEVICE_H
#include <stdint.h>
#include <stdbool.h>
#include "../programs/rv32/gpu.h"
typedef enum { IDLE=0, SETUP, SCAN, READ, WRITE, ADVANCE } gpu_phase;
/* One incremental device: latched parameters, coverage cursor and accepted-transfer
 * counters. BUSY is equivalent to a non-IDLE phase. */
typedef struct {
    uint32_t p[GP_COUNT], status,error,cycles,stalls,reads,writes;
    int32_t x,y,lo,top,hi,bottom,step,dx,dy,sy,err;
    int32_t ax,ay,bx,by,cx,cy;
    gpu_phase phase; uint8_t pixel;
    /* Issue #34: the framebuffer offset this tick wrote, for the CPU's LR.W reservation; the
     * machine reads and resets it after each tick. */
    bool fb_written; uint32_t fb_written_at;
    /* START/RESET consumes its store tick; SETUP begins on the next tick. */
    bool command_tick;
} gpu_device;
static inline bool gpu_busy(const gpu_device *g){return g->status==GPU_BUSY;}
void gpu_device_reset(gpu_device *g);
bool gpu_access(gpu_device *g,uint32_t off,int width,bool write,uint32_t *value);
/* window_start/window_end: the DMA window (docs/rv32.md), the RAM a blit may read from; SETUP
 * refuses a RAM source outside it. At reset it is all of RAM. */
void gpu_tick(gpu_device *g,uint8_t *ram,uint32_t ram_size,uint8_t *fb,bool hold,uint32_t window_start,uint32_t window_end);
bool gpu_source_locked(const gpu_device *g,uint32_t addr,int width);
#endif
