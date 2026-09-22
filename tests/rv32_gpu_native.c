#include "../tools/rv32_gpu.h"
static gpu_device g;
static uint8_t ram[262144],fb[76800];
void native_gpu_init(void){gpu_device_reset(&g);for(unsigned i=0;i<sizeof ram;i++)ram[i]=(uint8_t)(i*17+3);for(unsigned i=0;i<sizeof fb;i++)fb[i]=(uint8_t)(i*13+7);}
int native_gpu_access(uint32_t off,int width,int write,uint32_t *v){return gpu_access(&g,off,width,write!=0,v);}
void native_gpu_tick(int hold){gpu_tick(&g,ram,sizeof ram,fb,hold!=0);}
uint8_t *native_gpu_fb(void){return fb;}
uint8_t *native_gpu_ram(void){return ram;}
uint32_t native_gpu_phase(void){return g.phase;}
int native_gpu_lock(uint32_t addr,int width){return gpu_source_locked(&g,addr,width);}
