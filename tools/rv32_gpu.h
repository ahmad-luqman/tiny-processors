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
    /* START/RESET consumes its store tick; SETUP begins on the next tick. */
    bool command_tick;
} gpu_device;
static inline bool gpu_busy(const gpu_device *g){return g->status==GPU_BUSY;}
void gpu_device_reset(gpu_device *g);
bool gpu_access(gpu_device *g,uint32_t off,int width,bool write,uint32_t *value);
void gpu_tick(gpu_device *g,uint8_t *ram,uint32_t ram_size,uint8_t *fb,bool hold);
bool gpu_source_locked(const gpu_device *g,uint32_t addr,int width);
#endif
