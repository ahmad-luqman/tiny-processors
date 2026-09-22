#include "rv32_g3d.h"
#include <string.h>

#define RAM_ORIGIN 0x80000000u
#define Z_BYTES (320u*240u*2u)

void g3d_device_reset(g3d_device *d)
{
    /* The windows are CPU-owned memory: RESET leaves them. */
    uint32_t consts[G3D_CONSTS], program[G3D_PROGRAM_WORDS], inputs[G3D_VMAX][G3D_SLOTS], tris[G3D_TMAX];
    memcpy(consts,d->consts,sizeof consts); memcpy(program,d->program,sizeof program);
    memcpy(inputs,d->inputs,sizeof inputs); memcpy(tris,d->tris,sizeof tris);
    memset(d,0,sizeof *d);
    memcpy(d->consts,consts,sizeof consts); memcpy(d->program,program,sizeof program);
    memcpy(d->inputs,inputs,sizeof inputs); memcpy(d->tris,tris,sizeof tris);
}

static uint32_t *window(g3d_device *d, uint32_t off)
{
    if (off>=G3D_CONST && off<G3D_CONST+4*G3D_CONSTS) return &d->consts[(off-G3D_CONST)/4];
    if (off>=G3D_PROGRAM && off<G3D_PROGRAM+4*G3D_PROGRAM_WORDS) return &d->program[(off-G3D_PROGRAM)/4];
    if (off>=G3D_VERTEX && off<G3D_VERTEX+4*G3D_VMAX*G3D_SLOTS) return &d->inputs[0][0]+(off-G3D_VERTEX)/4;
    if (off>=G3D_TRIANGLE && off<G3D_TRIANGLE+4*G3D_TMAX) return &d->tris[(off-G3D_TRIANGLE)/4];
    return NULL;
}

static uint32_t *param(g3d_device *d, uint32_t off)
{
    switch (off) {
    case G3D_VCOUNT: return &d->vcount;
    case G3D_TCOUNT: return &d->tcount;
    case G3D_ZBASE: return &d->zbase;
    case G3D_LIMIT: return &d->limit;
    default: return NULL;
    }
}

bool g3d_access(g3d_device *d, uint32_t off, int width, bool write, uint32_t *v, bool other_busy)
{
    if (width!=4 || (off&3)) return false;
    uint32_t *w=window(d,off), *p=param(d,off);
    if (w) {
        if (g3d_busy(d)) return false;
        if (write) *w=*v; else *v=*w;
        return true;
    }
    if (p) {
        if (write) { if (g3d_busy(d)) return false; *p=*v; }
        else *v=*p;
        return true;
    }
    if (write) {
        if (off!=G3D_COMMAND) return false;
        if (*v==G3D_RESET) { g3d_device_reset(d); d->command_tick=true; return true; }
        if ((*v!=G3D_START && *v!=G3D_CLEAR_Z) || g3d_busy(d) || other_busy) return false;
        d->status=G3D_BUSY; d->phase=G3D_PH_VALIDATE; d->clearing=*v==G3D_CLEAR_Z;
        d->error=d->fault_pc=d->cycles=d->stalls=d->instructions=d->transfers=0;
        d->divides=d->pixels=d->zfail=d->culled=0;
        d->command_tick=true;
        return true;
    }
    switch (off) {
    case G3D_STATUS: *v=d->status; break;
    case G3D_ERROR: *v=d->error; break;
    case G3D_FAULT_PC: *v=d->fault_pc; break;
    case G3D_CYCLES: *v=d->cycles; break;
    case G3D_STALLS: *v=d->stalls; break;
    case G3D_INSTRUCTIONS: *v=d->instructions; break;
    case G3D_TRANSFERS: *v=d->transfers; break;
    case G3D_DIVIDES: *v=d->divides; break;
    case G3D_PIXELS: *v=d->pixels; break;
    case G3D_ZFAIL: *v=d->zfail; break;
    case G3D_CULLED: *v=d->culled; break;
    default: return false;
    }
    return true;
}

bool g3d_z_locked(const g3d_device *d, uint32_t addr, int width)
{
    return g3d_busy(d) && (uint64_t)addr+(uint32_t)width>d->zbase && (uint64_t)addr<(uint64_t)d->zbase+Z_BYTES;
}

static void fault(g3d_device *d, uint32_t error)
{
    d->status=G3D_FAULT; d->error=error; d->phase=G3D_PH_IDLE;
}

static int32_t sat(int64_t n, int32_t den)
{
    int64_t q=n/den;
    return q>INT32_MAX?INT32_MAX:q<INT32_MIN?INT32_MIN:(int32_t)q;
}

static int32_t qmul(uint32_t a, uint32_t b) { return (int32_t)(uint32_t)(((int64_t)(int32_t)a*(int32_t)b)>>16); }

static void next_triangle(g3d_device *d)
{
    d->index++;
    d->phase=d->index<d->tcount?G3D_PH_FETCH:G3D_PH_FINISH;
}

static void next_vertex(g3d_device *d)
{
    d->lane++;
    if (d->lane<G3D_LANES && d->batch*G3D_LANES+d->lane<d->vcount) { d->phase=G3D_PH_VCHECK; return; }
    d->batch++;
    if (d->batch*G3D_LANES<d->vcount) { d->phase=G3D_PH_BATCH; return; }
    d->index=0;
    d->phase=d->tcount?G3D_PH_FETCH:G3D_PH_FINISH;
}

static void next_pixel(g3d_device *d)
{
    if (d->x<d->right) { d->x++; d->phase=G3D_PH_TEST; return; }
    if (d->y<d->bottom) { d->x=d->left; d->y++; d->phase=G3D_PH_TEST; return; }
    next_triangle(d);
}

static void start_divides(g3d_device *d, bool setup, int32_t den)
{
    d->div_setup=setup; d->div_den=den; d->div_k=0; d->div_left=G3D_DIVIDE_TICKS; d->phase=G3D_PH_DIVIDE;
}

/* A shader fault records where it happened; FAULT_PC stays 0 otherwise. */
static bool shader_fault(g3d_device *d, uint32_t error)
{
    d->fault_pc=d->pc|d->batch<<8;
    fault(d,error);
    return false;
}

/* One shader instruction for the batch; returns false after a fault. */
static bool execute(g3d_device *d)
{
    if (d->count==d->limit) return shader_fault(d,G3D_E_LIMIT);
    if (d->pc>=G3D_PROGRAM_WORDS) return shader_fault(d,G3D_E_PC);
    uint32_t w=d->program[d->pc], op=w>>26, rd=w>>22&15u, ra=w>>18&15u, rb=w>>14&15u, rc=w>>10&15u;
    uint32_t imm=w&0x3fffu, target=w&0x7fu, next=d->pc+1;
    if (op>=G3D_OP_COUNT) return shader_fault(d,G3D_E_ILLEGAL);
    d->count++; d->instructions++;
    uint32_t top=d->depth-1;
    switch (op) {
    case G3D_OP_END:
        if (d->depth) return shader_fault(d,G3D_E_MISMATCH);
        d->lane=0; d->phase=G3D_PH_VCHECK;
        return true;
    case G3D_OP_IF: case G3D_OP_LOOP:
        if (d->depth==G3D_DEPTH) return shader_fault(d,G3D_E_OVERFLOW);
        d->stack[d->depth].loop=op==G3D_OP_LOOP;
        d->stack[d->depth].parent=(uint8_t)d->mask;
        d->stack[d->depth].taken=(uint8_t)(op==G3D_OP_IF?d->mask&d->pred:0);
        d->depth++;
        if (op==G3D_OP_IF) { d->mask&=d->pred; if (!d->mask) next=target; }
        break;
    case G3D_OP_ELSE: case G3D_OP_ENDIF:
        if (!d->depth || d->stack[top].loop) return shader_fault(d,G3D_E_MISMATCH);
        if (op==G3D_OP_ELSE) {
            d->mask=d->stack[top].parent&~(uint32_t)d->stack[top].taken&15u;
            if (!d->mask) next=target;
        } else { d->mask=d->stack[top].parent; d->depth--; }
        break;
    case G3D_OP_ENDLOOP:
        if (!d->depth || !d->stack[top].loop) return shader_fault(d,G3D_E_MISMATCH);
        if (d->mask) next=target; else { d->mask=d->stack[top].parent; d->depth--; }
        break;
    case G3D_OP_BREAK: {
        uint32_t i=d->depth;
        while (i && !d->stack[i-1].loop) i--;
        if (!i) return shader_fault(d,G3D_E_MISMATCH);
        for (;i<d->depth;i++) d->stack[i].parent&=(uint8_t)~d->mask;
        d->mask=0;
        break;
    }
    default:
        for (uint32_t l=0;l<G3D_LANES;l++) {
            if (!(d->mask>>l&1u)) continue;
            uint32_t *r=d->regs[l], a=r[ra], b=r[rb], vid=d->batch*G3D_LANES+l;
            int32_t sa=(int32_t)a, sb=(int32_t)b;
            switch (op) {
            case G3D_OP_SLT: case G3D_OP_SEQ:
                d->pred=(d->pred&~(1u<<l))|(uint32_t)(op==G3D_OP_SLT?sa<sb:a==b)<<l; break;
            case G3D_OP_OUT: d->out[l][imm&7u]=a; break;
            case G3D_OP_LDI: r[rd]=(uint32_t)((int32_t)(w<<10)>>10); break;
            case G3D_OP_LDC: r[rd]=d->consts[imm&31u]; break;
            case G3D_OP_IN: r[rd]=d->inputs[vid][imm&7u]; break;
            case G3D_OP_SPC: { uint32_t s[4]={l,vid,d->vcount,0}; r[rd]=s[imm&3u]; break; }
            case G3D_OP_MOV: r[rd]=a; break;
            case G3D_OP_ADD: r[rd]=a+b; break;
            case G3D_OP_SUB: r[rd]=a-b; break;
            case G3D_OP_MUL: r[rd]=(uint32_t)qmul(a,b); break;
            case G3D_OP_MAD: r[rd]=r[rc]+(uint32_t)qmul(a,b); break;
            case G3D_OP_MIN: r[rd]=sa<sb?a:b; break;
            case G3D_OP_MAX: r[rd]=sa>sb?a:b; break;
            case G3D_OP_ABS: r[rd]=sa<0?0u-a:a; break;
            case G3D_OP_AND: r[rd]=a&b; break;
            case G3D_OP_OR: r[rd]=a|b; break;
            case G3D_OP_XOR: r[rd]=a^b; break;
            case G3D_OP_SHL: r[rd]=a<<(imm&31u); break;
            case G3D_OP_SRA: r[rd]=(uint32_t)(sa>>(imm&31u)); break;
            case G3D_OP_ADDI: r[rd]=a+(uint32_t)((int32_t)(imm<<18)>>18); break;
            default: return shader_fault(d,G3D_E_INTERNAL);
            }
        }
    }
    d->pc=next;
    return true;
}

static void vertex_check(g3d_device *d)
{
    const uint32_t *o=d->out[d->lane];
    g3d_vertex *v=&d->verts[d->batch*G3D_LANES+d->lane];
    int64_t x=(int32_t)o[G3D_OUT_X], y=(int32_t)o[G3D_OUT_Y], z=(int32_t)o[G3D_OUT_Z], w=(int32_t)o[G3D_OUT_W];
    v->valid=w>=G3D_W_NEAR && x<=4*w && -x<=4*w && y<=4*w && -y<=4*w && z>=0 && z<=w;
    int32_t c[3];
    for (int k=0;k<3;k++) { int32_t s=(int32_t)o[G3D_OUT_R+k]>>16; c[k]=s<0?0:s>255?255:s; }
    v->r=c[0]; v->g=c[1]; v->b=c[2];
    if (!v->valid) { next_vertex(d); return; }
    d->div_num[0]=x*2560; d->div_num[1]=y*1920; d->div_num[2]=z*65535;
    start_divides(d,false,(int32_t)w);
}

static void divide_done(g3d_device *d)
{
    uint32_t k=d->div_k;
    d->div_q[k]=sat(d->div_num[k],d->div_den);
    d->divides++;
    if (!d->div_setup) {
        if (k<2) { d->div_k++; d->div_left=G3D_DIVIDE_TICKS; return; }
        g3d_vertex *v=&d->verts[d->batch*G3D_LANES+d->lane];
        v->sx=2560+d->div_q[0]; v->sy=1920-d->div_q[1]; v->z=d->div_q[2];
        next_vertex(d);
        return;
    }
    if (k<7) { d->div_k++; d->div_left=G3D_DIVIDE_TICKS; return; }
    if (d->left>d->right || d->top>d->bottom) { next_triangle(d); return; }
    d->x=d->left; d->y=d->top; d->phase=G3D_PH_TEST;
}

static void area(g3d_device *d)
{
    g3d_vertex *v=d->v;
    int64_t a=(int64_t)(v[1].sx-v[0].sx)*(v[2].sy-v[0].sy)-(int64_t)(v[1].sy-v[0].sy)*(v[2].sx-v[0].sx);
    if (a>=0) { d->culled++; next_triangle(d); return; }
    g3d_vertex t=v[1]; v[1]=v[2]; v[2]=t;
    d->area=(int32_t)-a;
    int32_t lo=v[0].sx, hi=v[0].sx, top=v[0].sy, bottom=v[0].sy;
    for (int i=1;i<3;i++) {
        if (v[i].sx<lo) lo=v[i].sx;
        if (v[i].sx>hi) hi=v[i].sx;
        if (v[i].sy<top) top=v[i].sy;
        if (v[i].sy>bottom) bottom=v[i].sy;
    }
    d->left=lo>>4<0?0:lo>>4; d->right=hi>>4>319?319:hi>>4;
    d->top=top>>4<0?0:top>>4; d->bottom=bottom>>4>239?239:bottom>>4;
    int64_t ex1=v[1].sx-v[0].sx, ey1=v[1].sy-v[0].sy, ex2=v[2].sx-v[0].sx, ey2=v[2].sy-v[0].sy;
    for (int k=0;k<4;k++) {
        int64_t a0=k==0?v[0].z:k==1?v[0].r:k==2?v[0].g:v[0].b;
        int64_t d1=(k==0?v[1].z:k==1?v[1].r:k==2?v[1].g:v[1].b)-a0;
        int64_t d2=(k==0?v[2].z:k==1?v[2].r:k==2?v[2].g:v[2].b)-a0;
        d->div_num[2*k]=(d1*ey2-d2*ey1)*65536;
        d->div_num[2*k+1]=(d2*ex1-d1*ex2)*65536;
    }
    start_divides(d,true,d->area);
}

static bool inside(const g3d_vertex *a, const g3d_vertex *b, int32_t px, int32_t py)
{
    int64_t dx=b->sx-a->sx, dy=b->sy-a->sy, e=dx*(py-a->sy)-dy*(px-a->sx);
    return e>0 || (e==0 && (dy<0 || (dy==0 && dx>0)));
}

static void test(g3d_device *d)
{
    const g3d_vertex *v=d->v;
    int32_t px=d->x*16+8, py=d->y*16+8;
    if (!inside(&v[0],&v[1],px,py) || !inside(&v[1],&v[2],px,py) || !inside(&v[2],&v[0],px,py)) {
        next_pixel(d);
        return;
    }
    int32_t base[4]={v[0].z,v[0].r,v[0].g,v[0].b}, at[4];
    for (int k=0;k<4;k++) {
        int64_t s=(int64_t)d->div_q[2*k]*(px-v[0].sx)+(int64_t)d->div_q[2*k+1]*(py-v[0].sy);
        int64_t a=base[k]+(s>>16), hi=k?255:65535;
        at[k]=(int32_t)(a<0?0:a>hi?hi:a);
    }
    d->z=at[0]; d->r=at[1]; d->g=at[2]; d->b=at[3];
    d->phase=G3D_PH_ZREAD;
}

void g3d_tick(g3d_device *d, uint8_t *ram, uint32_t ram_size, uint8_t *fb, bool hold)
{
    if (d->command_tick) { d->command_tick=false; return; }
    if (!g3d_busy(d)) return;
    d->cycles++;
    uint32_t zoff=d->zbase-RAM_ORIGIN+2u*((uint32_t)d->y*320u+(uint32_t)d->x);
    switch (d->phase) {
    case G3D_PH_VALIDATE: {
        bool z=d->zbase%4==0 && d->zbase>=RAM_ORIGIN && d->zbase-RAM_ORIGIN<=ram_size-Z_BYTES;
        if (d->clearing) { if (!z) fault(d,G3D_E_PARAM); else { d->index=0; d->phase=G3D_PH_CLEAR; } break; }
        if (!z || d->vcount<1 || d->vcount>G3D_VMAX || d->tcount>G3D_TMAX || d->limit<1 || d->limit>0xffffu) {
            fault(d,G3D_E_PARAM);
            break;
        }
        d->index=0; d->batch=0;
        d->phase=d->tcount?G3D_PH_INDEX:G3D_PH_BATCH;
        break;
    }
    case G3D_PH_INDEX: {
        uint32_t t=d->tris[d->index];
        if ((t&255u)>=d->vcount || (t>>8&255u)>=d->vcount || (t>>16&255u)>=d->vcount) { fault(d,G3D_E_INDEX); break; }
        if (++d->index==d->tcount) d->phase=G3D_PH_BATCH;
        break;
    }
    case G3D_PH_BATCH:
        memset(d->regs,0,sizeof d->regs); memset(d->out,0,sizeof d->out);
        d->pc=d->count=d->pred=d->depth=0; d->mask=0;
        for (uint32_t l=0;l<G3D_LANES;l++) if (d->batch*G3D_LANES+l<d->vcount) d->mask|=1u<<l;
        d->phase=G3D_PH_EXEC;
        break;
    case G3D_PH_EXEC:
        execute(d);
        break;
    case G3D_PH_VCHECK:
        vertex_check(d);
        break;
    case G3D_PH_DIVIDE:
        if (--d->div_left==0) divide_done(d);
        break;
    case G3D_PH_FETCH: {
        uint32_t t=d->tris[d->index];
        for (int k=0;k<3;k++) d->v[k]=d->verts[t>>(8*k)&255u];
        if (!d->v[0].valid || !d->v[1].valid || !d->v[2].valid) { d->culled++; next_triangle(d); }
        else d->phase=G3D_PH_AREA;
        break;
    }
    case G3D_PH_AREA:
        area(d);
        break;
    case G3D_PH_TEST:
        test(d);
        break;
    case G3D_PH_ZREAD: {
        if (hold) { d->stalls++; break; }
        uint32_t old=ram[zoff]|(uint32_t)ram[zoff+1]<<8;
        d->transfers++;
        if ((uint32_t)d->z<old) d->phase=G3D_PH_ZWRITE;
        else { d->zfail++; next_pixel(d); }
        break;
    }
    case G3D_PH_ZWRITE:
        if (hold) { d->stalls++; break; }
        ram[zoff]=(uint8_t)d->z; ram[zoff+1]=(uint8_t)(d->z>>8);
        d->transfers++; d->phase=G3D_PH_PWRITE;
        break;
    case G3D_PH_PWRITE: {
        if (hold) { d->stalls++; break; }
        static const uint8_t bayer[16]={0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5};
        int32_t t=bayer[(d->y&3)*4+(d->x&3)], r=d->r+2*t, g=d->g+2*t, b=d->b+4*t;
        fb[(uint32_t)d->y*320u+(uint32_t)d->x]=(uint8_t)(((r>255?255:r)&0xe0)|((g>255?255:g)>>3&0x1c)|(b>255?255:b)>>6);
        d->transfers++; d->pixels++;
        next_pixel(d);
        break;
    }
    case G3D_PH_CLEAR:
        if (hold) { d->stalls++; break; }
        memset(ram+d->zbase-RAM_ORIGIN+4u*d->index,0xff,4);
        d->transfers++;
        if (++d->index==Z_BYTES/4) d->phase=G3D_PH_FINISH;
        break;
    case G3D_PH_FINISH:
        d->status=G3D_DONE; d->phase=G3D_PH_IDLE;
        break;
    default:
        fault(d,G3D_E_INTERNAL);
    }
}
