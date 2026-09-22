/* G2 software reference: the vertex stage and the fixed-function pipeline in
 * integer C. It never multiplies or divides an int64 with an operator, because
 * the RV32I runtime has neither __muldi3 nor __divdi3: products are built from
 * 16-bit halves and division is the restoring loop the RTL divider uses. */
#include "g3d.h"

#define WIDTH 320
#define HEIGHT 240
#define MASK_ALL 15u

static int64_t smul64(int32_t a, int32_t b)
{
    uint32_t ua=a<0?0u-(uint32_t)a:(uint32_t)a, ub=b<0?0u-(uint32_t)b:(uint32_t)b;
    uint32_t al=ua&0xffffu, ah=ua>>16, bl=ub&0xffffu, bh=ub>>16;
    uint64_t p=(uint64_t)(al*bl)+((uint64_t)(al*bh)<<16)+((uint64_t)(ah*bl)<<16)+((uint64_t)(ah*bh)<<32);
    return (a<0)!=(b<0)?(int64_t)(0u-p):(int64_t)p;
}
static int64_t shl64(int64_t v, unsigned s) { return (int64_t)((uint64_t)v<<s); }
static int32_t qmul(uint32_t a, uint32_t b) { return (int32_t)(uint32_t)(uint64_t)(smul64((int32_t)a,(int32_t)b)>>16); }

int32_t g3d_div_sat(int64_t n, int32_t d)
{
    uint64_t mag=n<0?0u-(uint64_t)n:(uint64_t)n;
    /* A quotient of 2^31 or more cannot be represented; the RTL checks the
     * same condition before its 32 iterations. */
    if ((mag>>31)>=(uint64_t)(uint32_t)d) return n<0?INT32_MIN:INT32_MAX;
    uint64_t rem=mag>>32;
    uint32_t q=0;
    for (int i=31;i>=0;i--) {
        rem=rem<<1|(mag>>i&1u);
        q<<=1;
        if (rem>=(uint64_t)(uint32_t)d) { rem-=(uint32_t)d; q|=1u; }
    }
    return n<0?(int32_t)(0u-q):(int32_t)q;
}

struct entry { uint8_t loop, parent, taken; };

/* BREAK: active lanes leave the innermost loop. Every IF entry above that
 * LOOP forgets them so a later ELSE or ENDIF cannot revive them; the LOOP
 * entry keeps its parent, which ENDLOOP restores once no lane is iterating. */
static int break_lanes(struct entry *stack, uint32_t depth, uint32_t *mask)
{
    uint32_t i=depth;
    while (i>0 && !stack[i-1].loop) i--;
    if (i==0) return 0;
    for (uint32_t j=i;j<depth;j++) stack[j].parent&=(uint8_t)~*mask;
    *mask=0;
    return 1;
}

static uint32_t run_shader(const struct g3d_job *job, uint32_t out[][G3D_SLOTS], struct g3d_counts *c)
{
    for (uint32_t v=0;v<job->vcount;v++) for (uint32_t s=0;s<G3D_SLOTS;s++) out[v][s]=0;
    for (uint32_t batch=0;batch*G3D_LANES<job->vcount;batch++) {
        /* Explicit zeroing: an aggregate initializer would call memset, which the guest lacks. */
        uint32_t regs[G3D_LANES][G3D_REGS];
        for (uint32_t l=0;l<G3D_LANES;l++) for (uint32_t i=0;i<G3D_REGS;i++) regs[l][i]=0;
        struct entry stack[G3D_DEPTH];
        uint32_t depth=0, mask=0, pred=0, pc=0, count=0;
        for (uint32_t l=0;l<G3D_LANES;l++) if (batch*G3D_LANES+l<job->vcount) mask|=1u<<l;
        for (;;) {
            c->fault_pc=pc|batch<<8;
            if (count==job->limit) return G3D_E_LIMIT;
            if (pc>=G3D_PROGRAM_WORDS) return G3D_E_PC;
            uint32_t w=job->program[pc], op=w>>26, rd=w>>22&15u, ra=w>>18&15u, rb=w>>14&15u;
            uint32_t rc=w>>10&15u, imm=w&0x3fffu, target=w&0x7fu, next=pc+1;
            if (op>=G3D_OP_COUNT) return G3D_E_ILLEGAL;
            count++; c->instructions++;
            switch (op) {
            case G3D_OP_END:
                if (depth) return G3D_E_MISMATCH;
                goto batch_done;
            case G3D_OP_IF:
                if (depth==G3D_DEPTH) return G3D_E_OVERFLOW;
                stack[depth].loop=0; stack[depth].parent=(uint8_t)mask; stack[depth].taken=(uint8_t)(mask&pred);
                depth++; mask&=pred;
                if (!mask) next=target;
                break;
            case G3D_OP_ELSE:
                if (!depth || stack[depth-1].loop) return G3D_E_MISMATCH;
                mask=stack[depth-1].parent&~(uint32_t)stack[depth-1].taken&MASK_ALL;
                if (!mask) next=target;
                break;
            case G3D_OP_ENDIF:
                if (!depth || stack[depth-1].loop) return G3D_E_MISMATCH;
                mask=stack[--depth].parent;
                break;
            case G3D_OP_LOOP:
                if (depth==G3D_DEPTH) return G3D_E_OVERFLOW;
                stack[depth].loop=1; stack[depth].parent=(uint8_t)mask; stack[depth].taken=0;
                depth++;
                break;
            case G3D_OP_ENDLOOP:
                if (!depth || !stack[depth-1].loop) return G3D_E_MISMATCH;
                if (mask) next=target; else mask=stack[--depth].parent;
                break;
            case G3D_OP_BREAK:
                if (!break_lanes(stack,depth,&mask)) return G3D_E_MISMATCH;
                break;
            default:
                for (uint32_t l=0;l<G3D_LANES;l++) {
                    if (!(mask>>l&1u)) continue;
                    uint32_t *r=regs[l], a=r[ra], b=r[rb], vid=batch*G3D_LANES+l, v=0;
                    switch (op) {
                    case G3D_OP_SLT: pred=(pred&~(1u<<l))|(uint32_t)((int32_t)a<(int32_t)b)<<l; continue;
                    case G3D_OP_SEQ: pred=(pred&~(1u<<l))|(uint32_t)(a==b)<<l; continue;
                    case G3D_OP_OUT: out[vid][imm&7u]=a; continue;
                    case G3D_OP_LDI: v=(w&0x200000u)?(w&0x3fffffu)|0xffc00000u:w&0x3fffffu; break;
                    case G3D_OP_LDC: v=job->consts[imm&31u]; break;
                    case G3D_OP_IN: v=job->inputs[vid][imm&7u]; break;
                    case G3D_OP_SPC: v=(imm&3u)==0?l:(imm&3u)==1?vid:(imm&3u)==2?job->vcount:0; break;
                    case G3D_OP_MOV: v=a; break;
                    case G3D_OP_ADD: v=a+b; break;
                    case G3D_OP_SUB: v=a-b; break;
                    case G3D_OP_MUL: v=(uint32_t)qmul(a,b); break;
                    case G3D_OP_MAD: v=r[rc]+(uint32_t)qmul(a,b); break;
                    case G3D_OP_MIN: v=(int32_t)a<(int32_t)b?a:b; break;
                    case G3D_OP_MAX: v=(int32_t)a>(int32_t)b?a:b; break;
                    case G3D_OP_ABS: v=(int32_t)a<0?0u-a:a; break;
                    case G3D_OP_AND: v=a&b; break;
                    case G3D_OP_OR: v=a|b; break;
                    case G3D_OP_XOR: v=a^b; break;
                    case G3D_OP_SHL: v=a<<(imm&31u); break;
                    case G3D_OP_SRA: v=(uint32_t)((int32_t)a>>(imm&31u)); break;
                    case G3D_OP_ADDI: v=a+((imm&0x2000u)?imm|0xffffc000u:imm); break;
                    default: return G3D_E_INTERNAL;
                    }
                    r[rd]=v;
                }
            }
            pc=next;
        }
batch_done:;
    }
    return G3D_E_NONE;
}

struct vert { int32_t a[6]; int valid; };   /* sx, sy (S12.4), z16, r, g, b */

static int32_t clamp(int32_t v, int32_t lo, int32_t hi) { return v<lo?lo:v>hi?hi:v; }

static void project(const uint32_t *o, struct vert *v, struct g3d_counts *c)
{
    int32_t x=(int32_t)o[G3D_OUT_X], y=(int32_t)o[G3D_OUT_Y], z=(int32_t)o[G3D_OUT_Z], w=(int32_t)o[G3D_OUT_W];
    int64_t guard=shl64(w,2);
    v->valid=w>=G3D_W_NEAR && x<=guard && -(int64_t)x<=guard && y<=guard && -(int64_t)y<=guard && z>=0 && z<=w;
    if (!v->valid) return;
    c->divides+=3;
    v->a[0]=WIDTH/2*G3D_SUB+g3d_div_sat(smul64(x,WIDTH/2*G3D_SUB),w);
    v->a[1]=HEIGHT/2*G3D_SUB-g3d_div_sat(smul64(y,HEIGHT/2*G3D_SUB),w);
    v->a[2]=g3d_div_sat(smul64(z,(int32_t)G3D_Z_MAX),w);
    for (int k=0;k<3;k++) v->a[3+k]=clamp((int32_t)o[G3D_OUT_R+k]>>16,0,255);
}

static int covers(const int32_t *a, const int32_t *b, int32_t px, int32_t py)
{
    int32_t dx=b[0]-a[0], dy=b[1]-a[1], e=dx*(py-a[1])-dy*(px-a[0]);
    return e>0 || (e==0 && (dy<0 || (dy==0 && dx>0)));
}

static int32_t floor16(int32_t v) { return v>>4; }   /* arithmetic: floor for S12.4 */

static void draw(uint8_t *fb, uint16_t *zbuf, const struct vert *p0, const struct vert *p1,
                 const struct vert *p2, struct g3d_counts *c)
{
    const int32_t *v0=p0->a, *v1=p1->a, *v2=p2->a;
    c->cycles++;   /* area and scan box */
    int32_t area=(v1[0]-v0[0])*(v2[1]-v0[1])-(v1[1]-v0[1])*(v2[0]-v0[0]);
    if (area>=0) { c->culled++; return; }
    { const int32_t *t=v1; v1=v2; v2=t; area=-area; }
    int32_t ex1=v1[0]-v0[0], ey1=v1[1]-v0[1], ex2=v2[0]-v0[0], ey2=v2[1]-v0[1];
    int32_t gx[4], gy[4];
    for (int k=0;k<4;k++) {
        int32_t d1=v1[2+k]-v0[2+k], d2=v2[2+k]-v0[2+k];
        gx[k]=g3d_div_sat(shl64(smul64(d1,ey2)-smul64(d2,ey1),16),area);
        gy[k]=g3d_div_sat(shl64(smul64(d2,ex1)-smul64(d1,ex2),16),area);
    }
    c->divides+=8;
    int32_t left=v0[0],right=v0[0],top=v0[1],bottom=v0[1];
    const int32_t *vs[2]={v1,v2};
    for (int i=0;i<2;i++) {
        if (vs[i][0]<left) left=vs[i][0];
        if (vs[i][0]>right) right=vs[i][0];
        if (vs[i][1]<top) top=vs[i][1];
        if (vs[i][1]>bottom) bottom=vs[i][1];
    }
    /* The scan box is the pixels whose floor contains a vertex extent,
     * clipped to the screen; it may be empty. */
    left=floor16(left); right=floor16(right); top=floor16(top); bottom=floor16(bottom);
    if (left<0) left=0;
    if (top<0) top=0;
    if (right>WIDTH-1) right=WIDTH-1;
    if (bottom>HEIGHT-1) bottom=HEIGHT-1;
    for (int32_t y=top;y<=bottom;y++) for (int32_t x=left;x<=right;x++) {
        int32_t px=x*G3D_SUB+G3D_SUB/2, py=y*G3D_SUB+G3D_SUB/2;
        c->cycles++;   /* one test tick per scanned pixel */
        if (!covers(v0,v1,px,py) || !covers(v1,v2,px,py) || !covers(v2,v0,px,py)) continue;
        int32_t at[4];
        for (int k=0;k<4;k++) {
            int64_t s=smul64(gx[k],px-v0[0])+smul64(gy[k],py-v0[1]);
            at[k]=clamp(v0[2+k]+(int32_t)(s>>16),0,k?255:(int32_t)G3D_Z_MAX);
        }
        uint32_t i=(uint32_t)y*WIDTH+(uint32_t)x;
        c->transfers++;
        if ((uint32_t)at[0]<zbuf[i]) {
            zbuf[i]=(uint16_t)at[0];
            /* Ordered 4x4 Bayer dither, up to one step of each channel. */
            static const uint8_t bayer[16]={0,8,2,10,12,4,14,6,3,11,1,9,15,7,13,5};
            int32_t t=bayer[(y&3)*4+(x&3)], r=at[1]+2*t, g=at[2]+2*t, b=at[3]+4*t;
            r=r>255?255:r; g=g>255?255:g; b=b>255?255:b;
            fb[i]=(uint8_t)((r&0xe0)|(g>>3&0x1c)|b>>6);
            c->pixels++; c->transfers+=2;
        } else c->zfail++;
    }
}

uint32_t g3d_reference(uint8_t *fb, uint16_t *zbuf, const struct g3d_job *job, struct g3d_counts *c)
{
    static uint32_t out[G3D_VMAX][G3D_SLOTS];
    static struct vert verts[G3D_VMAX];
    c->error=c->fault_pc=c->instructions=c->transfers=0;
    c->divides=c->pixels=c->zfail=c->culled=0;
    c->cycles=1;   /* validate */
    if (job->vcount<1 || job->vcount>G3D_VMAX || job->tcount>G3D_TMAX || job->limit<1 || job->limit>0xffffu)
        return c->error=G3D_E_PARAM;
    for (uint32_t t=0;t<job->tcount;t++) {
        c->cycles++;   /* one index tick per triangle */
        for (uint32_t k=0;k<3;k++) if ((job->triangles[t]>>(8*k)&0xffu)>=job->vcount) return c->error=G3D_E_INDEX;
    }
    uint32_t e=run_shader(job,out,c);
    /* Each batch is projected as soon as it ends, so a fault in batch b
     * follows the projection of every vertex before 4b. */
    uint32_t done=e?(c->fault_pc>>8)*G3D_LANES:job->vcount;
    if (done>job->vcount) done=job->vcount;
    uint32_t batches=(done+G3D_LANES-1)/G3D_LANES;
    c->cycles+=batches+c->instructions+done;
    if (e) c->cycles+=1u+(e==G3D_E_LIMIT || e==G3D_E_PC || e==G3D_E_ILLEGAL);
    for (uint32_t v=0;v<done;v++) project(out[v],&verts[v],c);
    if (e) { c->cycles+=G3D_DIVIDE_TICKS*c->divides; return c->error=e; }
    c->fault_pc=0;
    for (uint32_t t=0;t<job->tcount;t++) {
        const struct vert *p[3];
        int ok=1;
        c->cycles++;   /* fetch */
        for (uint32_t k=0;k<3;k++) { p[k]=&verts[job->triangles[t]>>(8*k)&0xffu]; ok&=p[k]->valid; }
        if (!ok) { c->culled++; continue; }
        draw(fb,zbuf,p[0],p[1],p[2],c);
    }
    c->cycles+=G3D_DIVIDE_TICKS*c->divides+c->transfers+1u;   /* finish */
    return G3D_E_NONE;
}
