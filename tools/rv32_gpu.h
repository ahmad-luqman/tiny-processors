#ifndef RV32_GPU_DEVICE_H
#define RV32_GPU_DEVICE_H
#include <stdint.h>
#include <stdbool.h>
#include "../programs/rv32/gpu.h"
typedef struct {
    uint32_t p[16], status,error,cycles,stalls,reads,writes;
    int32_t x,y,lo,top,hi,bottom,step,dx,dy,sy,err;
    int32_t ax,ay,bx,by,cx,cy;
    uint32_t phase; uint8_t pixel;
    bool command_tick;
} gpu_device;
void gpu_device_reset(gpu_device *g);
bool gpu_access(gpu_device *g,uint32_t off,int width,bool write,uint32_t *value);
void gpu_tick(gpu_device *g,uint8_t *ram,uint32_t ram_size,uint8_t *fb,bool hold);
bool gpu_source_locked(const gpu_device *g,uint32_t addr,int width);
#endif
