#include "gpu.h"
#include "mmio.h"
void gpu_reset(void) { mmio_write32(GPU_BASE+GPU_COMMAND,GPU_RESET); }
int gpu_submit(const struct gpu_command *c)
{
    if(mmio_read32(GPU_BASE+GPU_STATUS)&GPU_BUSY) return 0;
    for(uint32_t i=0;i<GP_COUNT;i++) mmio_write32(GPU_BASE+GPU_PARAMS+4*i,c->p[i]);
    mmio_write32(GPU_BASE+GPU_COMMAND,GPU_START);
    return 1;
}
int gpu_wait(uint32_t budget)
{
    while(budget--) {
        uint32_t s=mmio_read32(GPU_BASE+GPU_STATUS);
        if(!(s&GPU_BUSY)) return s==GPU_DONE;
    }
    gpu_reset(); return 0;
}
