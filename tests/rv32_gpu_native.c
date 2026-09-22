#include "../tools/rv32_gpu.h"
static gpu_device g;
static uint8_t ram[262144],fb[76800];
void native_gpu_reset(void){gpu_device_reset(&g);}
void native_gpu_init(void){gpu_device_reset(&g);for(unsigned i=0;i<sizeof ram;i++)ram[i]=(uint8_t)(i*17+3);for(unsigned i=0;i<sizeof fb;i++)fb[i]=(uint8_t)(i*13+7);}
int native_gpu_access(uint32_t off,int width,int write,uint32_t *v){return gpu_access(&g,off,width,write!=0,v);}
void native_gpu_tick(int hold){gpu_tick(&g,ram,sizeof ram,fb,hold!=0);}
uint8_t *native_gpu_fb(void){return fb;}
uint8_t *native_gpu_ram(void){return ram;}
uint32_t native_gpu_phase(void){return g.phase;}
int native_gpu_lock(uint32_t addr,int width){return gpu_source_locked(&g,addr,width);}
/* Two triangles exactly partition a 4x4 square; literal independent coverage. */
int native_gpu_anchor(void)
{
    static uint8_t a[76800],b[76800];
    struct gpu_command c;
    for(unsigned i=0;i<76800;i++)a[i]=b[i]=0;
    gpu_command_init(&c,GPU_TRIANGLE,1);
    c.p[GP_X0]=1;c.p[GP_Y0]=1;c.p[GP_X1]=5;c.p[GP_Y1]=1;c.p[GP_X2]=1;c.p[GP_Y2]=5;
    gpu_reference(a,0,&c);
    c.p[GP_X0]=5;c.p[GP_Y0]=5;c.p[GP_X1]=1;c.p[GP_Y1]=5;c.p[GP_X2]=5;c.p[GP_Y2]=1;
    gpu_reference(b,0,&c);
    for(unsigned y=0;y<240;y++)for(unsigned x=0;x<320;x++)
        if(a[y*320+x]+b[y*320+x]!=(unsigned)(x>=1&&x<5&&y>=1&&y<5))return 1;
    native_gpu_init();
    uint32_t v=1;
    if(!gpu_access(&g,64,4,1,&v))return 2;
    v=320;if(!gpu_access(&g,96,4,1,&v))return 3;
    v=240;if(!gpu_access(&g,100,4,1,&v))return 4;
    v=1;if(!gpu_access(&g,0,4,1,&v))return 5;
    for(unsigned i=0;i<300000;i++)gpu_tick(&g,ram,sizeof ram,fb,i%7==0);
    if(g.status!=GPU_DONE || g.writes!=76800 || g.stalls==0)return 6;
    for(unsigned i=0;i<76800;i++)if(fb[i]!=0)return 7;
    return 0;
}
#ifdef G1_NATIVE_MAIN
#include <stdio.h>
int main(void){int result=native_gpu_anchor();if(result)fprintf(stderr,"G1 native anchor failed %d\n",result);else puts("G1 native anchors pass under sanitizers");return result;}
#endif
