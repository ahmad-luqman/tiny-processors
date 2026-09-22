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
int main(int argc,char **argv)
{
    int result=native_gpu_anchor();
    if(result){fprintf(stderr,"G1 native anchor failed %d\n",result);return result;}
    if(argc==2){
        FILE *input=fopen(argv[1],"r");if(!input)return 10;
        static uint8_t before[76800];
        int mode,abort_tick,rc;unsigned jobs=0;
        native_gpu_init();
        while((rc=fscanf(input,"%d %d",&mode,&abort_tick))==2){
            struct gpu_command c;
            for(unsigned i=0;i<16;i++){
                if(fscanf(input,"%x",&c.p[i])!=1)return 11;
                if(!gpu_access(&g,64+4*i,4,true,&c.p[i]))return 12;
            }
            for(unsigned i=0;i<76800;i++)before[i]=fb[i];
            uint32_t start=1;if(!gpu_access(&g,0,4,true,&start))return 13;
            gpu_tick(&g,ram,sizeof ram,fb,false);
            unsigned ticks=0;
            while(g.status==GPU_BUSY){
                if((int)ticks==abort_tick){
                    if(mode&2)gpu_device_reset(&g);
                    else{uint32_t reset=2;if(!gpu_access(&g,0,4,true,&reset))return 14;}
                }
                gpu_tick(&g,ram,sizeof ram,fb,(mode&1) && ticks%7<2);
                if(++ticks>4000000)return 15;
            }
            if(g.status==GPU_DONE){
                const uint8_t *source=c.p[GP_SRC]==0x30000000u?before:
                    c.p[GP_OP]==GPU_BLIT?ram+(c.p[GP_SRC]-0x80000000u):0;
                gpu_reference(before,source,&c);
                for(unsigned i=0;i<76800;i++)if(before[i]!=fb[i])return 16;
            }
            jobs++;
        }
        if(rc!=EOF || !jobs || fclose(input))return 17;
        printf("G1 native anchors and %u corpus jobs pass under sanitizers\n",jobs);
    }else puts("G1 native anchors pass under sanitizers");
    return 0;
}
#endif
