#include "gpu.h"
#include "mmio.h"
#include "console.h"
static struct gpu_result last_result;
static void capture(uint32_t status)
{
    last_result.status=status;
    last_result.reason=mmio_read32(GPU_BASE+GPU_ERROR);
    last_result.cycles=mmio_read32(GPU_BASE+GPU_CYCLES);
    last_result.stalls=mmio_read32(GPU_BASE+GPU_STALLS);
    last_result.reads=mmio_read32(GPU_BASE+GPU_READS);
    last_result.writes=mmio_read32(GPU_BASE+GPU_WRITES);
}
const struct gpu_result *gpu_last_result(void){return &last_result;}
void gpu_reset(void){mmio_write32(GPU_BASE+GPU_COMMAND,GPU_RESET);}
int gpu_submit(const struct gpu_command *c)
{
    if(mmio_read32(GPU_BASE+GPU_STATUS)==GPU_BUSY)return 0;
    for(uint32_t i=0;i<GP_COUNT;i++)mmio_write32(GPU_BASE+GPU_PARAMS+4*i,c->p[i]);
    mmio_write32(GPU_BASE+GPU_COMMAND,GPU_START);
    return 1;
}
uint32_t gpu_wait(uint32_t budget)
{
    while(budget--){
        uint32_t status=mmio_read32(GPU_BASE+GPU_STATUS);
        if(status!=GPU_BUSY){if(status!=GPU_DONE)capture(status);return status;}
    }
    capture(mmio_read32(GPU_BASE+GPU_STATUS));
    gpu_reset();return GPU_TIMEOUT;
}
static void field(const char *name,uint32_t value){rv32_puts(name);rv32_put_hex32(value);}
int gpu_run(const struct gpu_command *c,uint32_t budget)
{
    uint32_t outcome;
    if(!gpu_submit(c)){outcome=GPU_BUSY;capture(mmio_read32(GPU_BASE+GPU_STATUS));}
    else outcome=gpu_wait(budget);
    if(outcome==GPU_DONE)return 1;
    field("G1 failure op=",c->p[GP_OP]);field(" outcome=",outcome);
    field(" status=",last_result.status);field(" reason=",last_result.reason);
    field(" cycles=",last_result.cycles);field(" stalls=",last_result.stalls);
    field(" reads=",last_result.reads);field(" writes=",last_result.writes);rv32_putc('\n');
    return 0;
}
