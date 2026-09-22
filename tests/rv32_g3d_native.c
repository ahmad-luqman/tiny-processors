/* Native bridge for the G2 emulator device: the tests drive it exactly as the
 * CPU does, through word accesses and ticks, and the holds model a slow RAM.
 * With G3D_NATIVE_MAIN it is also the sanitizer harness: every corpus job
 * (tools/rv32_g3d_corpus.py) runs on the C reference and on the device, and
 * both must match the oracle's counters and hashes. */
#include "../tools/rv32_g3d.h"
#include <stddef.h>
size_t native_g3d_size(void) { return sizeof(g3d_device); }

#ifdef G3D_NATIVE_MAIN
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#define RAM_SIZE 0x400000u
#define ZBASE 0x80040000u

static uint32_t hash(const uint8_t *bytes, uint32_t n)
{
    uint32_t h=5381;
    for (uint32_t i=0;i<n;i+=4) h=((h<<5)+h)^(bytes[i]|(uint32_t)bytes[i+1]<<8|(uint32_t)bytes[i+2]<<16|(uint32_t)bytes[i+3]<<24);
    return h;
}

static int check(int job, const char *who, const char *name, uint32_t got, uint32_t want)
{
    if (got==want) return 1;
    fprintf(stderr,"job %d %s %s got %08x want %08x\n",job,who,name,got,want);
    return 0;
}

int main(int argc, char **argv)
{
    FILE *f=fopen(argc>1?argv[1]:"build/g3d/corpus.txt","r");
    if (!f) { perror("corpus"); return 2; }
    static uint8_t ram[RAM_SIZE], fb[76800], ref_fb[76800];
    static uint16_t ref_z[76800];
    static g3d_device dev;
    static uint32_t w[4096];
    char line[65536];
    int job=0, failed=0;
    while (fgets(line,sizeof line,f)) {
        uint32_t n=0;
        for (char *p=strtok(line," \n");p && n<4096;p=strtok(NULL," \n")) w[n++]=(uint32_t)strtoul(p,NULL,16);
        uint32_t vcount=w[0], tcount=w[1], limit=w[2], zbase=w[3], seed=w[4];
        const uint32_t *program=w+5, *consts=program+128, *inputs=consts+32, *tris=inputs+vcount*8, *want=tris+tcount;
        const char *names[12]={"status","error","fault_pc","instructions","transfers","divides","pixels","zfail","culled","cycles","fb","z"};
        /* Reference. */
        memset(ref_fb,0,sizeof ref_fb);
        for (uint32_t i=0;i<76800;i++) ref_z[i]=0xffff;
        struct g3d_job j={program,consts,(const uint32_t (*)[G3D_SLOTS])inputs,tris,vcount,tcount,limit,G3D_PROGRAM_WORDS};
        struct g3d_counts c;
        uint32_t e=zbase==ZBASE?g3d_reference(ref_fb,ref_z,&j,&c):(c.error=G3D_E_PARAM,c.fault_pc=c.instructions=c.transfers=c.divides=c.pixels=c.zfail=c.culled=0,c.cycles=1,G3D_E_PARAM);
        uint32_t ref[12]={e?G3D_FAULT:G3D_DONE,c.error,c.fault_pc,c.instructions,c.transfers,c.divides,c.pixels,c.zfail,c.culled,c.cycles,
                          hash(ref_fb,sizeof ref_fb),hash((const uint8_t *)ref_z,sizeof ref_z)};
        /* Device, through the access path, with seeded holds. */
        memset(&dev,0,sizeof dev); g3d_device_reset(&dev);
        memset(ram,0,sizeof ram); memset(fb,0,sizeof fb);
        memset(ram+(ZBASE-0x80000000u),0xff,320u*240u*2u);
        uint32_t v, ok=1;
        for (uint32_t i=0;i<128;i++) { v=program[i]; ok&=g3d_access(&dev,G3D_PROGRAM+4*i,4,true,&v,false); }
        for (uint32_t i=0;i<32;i++) { v=consts[i]; ok&=g3d_access(&dev,G3D_CONST+4*i,4,true,&v,false); }
        for (uint32_t i=0;i<vcount*8;i++) { v=inputs[i]; ok&=g3d_access(&dev,G3D_VERTEX+4*i,4,true,&v,false); }
        for (uint32_t i=0;i<tcount;i++) { v=tris[i]; ok&=g3d_access(&dev,G3D_TRIANGLE+4*i,4,true,&v,false); }
        uint32_t params[4][2]={{G3D_VCOUNT,vcount},{G3D_TCOUNT,tcount},{G3D_ZBASE,zbase},{G3D_LIMIT,limit}};
        for (int i=0;i<4;i++) ok&=g3d_access(&dev,params[i][0],4,true,&params[i][1],false);
        v=G3D_START; ok&=g3d_access(&dev,G3D_COMMAND,4,true,&v,false);
        if (!ok) { fprintf(stderr,"job %d: access refused\n",job); return 1; }
        g3d_tick(&dev,ram,RAM_SIZE,fb,false);
        uint32_t rng=seed*2654435761u+1u, ticks=0;
        while (g3d_busy(&dev) && ticks++<50000000u) { rng=rng*1103515245u+12345u; g3d_tick(&dev,ram,RAM_SIZE,fb,seed && (rng>>16)%4==0); }
        uint32_t got[12]={dev.status,dev.error,dev.fault_pc,dev.instructions,dev.transfers,dev.divides,dev.pixels,dev.zfail,dev.culled,
                          dev.cycles-dev.stalls,hash(fb,sizeof fb),hash(ram+(ZBASE-0x80000000u),320u*240u*2u)};
        for (int k=0;k<12;k++) {
            failed|=!check(job,"reference",names[k],ref[k],want[k]);
            failed|=!check(job,"device",names[k],got[k],want[k]);
        }
        if (seed && dev.transfers && !dev.stalls) { fprintf(stderr,"job %d: holds never stalled\n",job); failed=1; }
        job++;
    }
    fclose(f);
    if (failed) return 1;
    printf("%d G2 corpus jobs agree under ASan/UBSan\n",job);
    return 0;
}
#endif
