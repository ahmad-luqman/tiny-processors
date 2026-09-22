/* G2 driver: window loads, one command at a time, polling with a budget. */
#include "g3d.h"
#include "mmio.h"
#include "console.h"

static struct g3d_result last_result;

static uint32_t reg(uint32_t off) { return mmio_read32(G3D_BASE+off); }

static void capture(uint32_t status)
{
    last_result.status=status;
    last_result.error=reg(G3D_ERROR);
    last_result.fault_pc=reg(G3D_FAULT_PC);
    last_result.cycles=reg(G3D_CYCLES);
    last_result.stalls=reg(G3D_STALLS);
    last_result.instructions=reg(G3D_INSTRUCTIONS);
    last_result.transfers=reg(G3D_TRANSFERS);
    last_result.divides=reg(G3D_DIVIDES);
    last_result.pixels=reg(G3D_PIXELS);
    last_result.zfail=reg(G3D_ZFAIL);
    last_result.culled=reg(G3D_CULLED);
}

const struct g3d_result *g3d_last_result(void) { return &last_result; }

void g3d_reset(void) { mmio_write32(G3D_BASE+G3D_COMMAND,G3D_RESET); }

static uint32_t window_end(uint32_t offset)
{
    if (offset>=G3D_CONST && offset<G3D_CONST+4*G3D_CONSTS) return G3D_CONST+4*G3D_CONSTS;
    if (offset>=G3D_PROGRAM && offset<G3D_PROGRAM+4*G3D_PROGRAM_WORDS) return G3D_PROGRAM+4*G3D_PROGRAM_WORDS;
    if (offset>=G3D_VERTEX && offset<G3D_VERTEX+4*G3D_VMAX*G3D_SLOTS) return G3D_VERTEX+4*G3D_VMAX*G3D_SLOTS;
    if (offset>=G3D_TRIANGLE && offset<G3D_TRIANGLE+4*G3D_TMAX) return G3D_TRIANGLE+4*G3D_TMAX;
    return 0;
}

int g3d_load(uint32_t offset, const uint32_t *words, uint32_t count)
{
    /* Outside every window window_end is 0: refuse before the subtraction can wrap. */
    uint32_t end=window_end(offset);
    if (reg(G3D_STATUS)==G3D_BUSY || !end || (offset&3u) || count>(end-offset)/4u) return 0;
    for (uint32_t i=0;i<count;i++) mmio_write32(G3D_BASE+offset+4*i,words[i]);
    return 1;
}

int g3d_load_program(const uint32_t *words, uint32_t count)
{
    if (!g3d_load(G3D_PROGRAM,words,count)) return 0;
    for (uint32_t i=count;i<G3D_PROGRAM_WORDS;i++) mmio_write32(G3D_BASE+G3D_PROGRAM+4*i,0);
    return 1;
}

int g3d_submit(uint32_t command, uint32_t vcount, uint32_t tcount, uint32_t zbase, uint32_t limit)
{
    if (reg(G3D_STATUS)==G3D_BUSY) return 0;
    mmio_write32(G3D_BASE+G3D_VCOUNT,vcount);
    mmio_write32(G3D_BASE+G3D_TCOUNT,tcount);
    mmio_write32(G3D_BASE+G3D_ZBASE,zbase);
    mmio_write32(G3D_BASE+G3D_LIMIT,limit);
    mmio_write32(G3D_BASE+G3D_COMMAND,command);
    return 1;
}

uint32_t g3d_wait(uint32_t budget)
{
    while (budget--) {
        uint32_t status=reg(G3D_STATUS);
        if (status!=G3D_BUSY) { capture(status); return status; }
    }
    /* The job may have finished between the last poll and the snapshot: keep
     * that result rather than resetting it away. */
    uint32_t status=reg(G3D_STATUS);
    capture(status);
    if (status!=G3D_BUSY) return status;
    g3d_reset();
    return G3D_TIMEOUT;
}

static void field(const char *name, uint32_t value) { rv32_puts(name); rv32_put_hex32(value); }

int g3d_run(uint32_t command, uint32_t vcount, uint32_t tcount, uint32_t zbase, uint32_t limit, uint32_t budget)
{
    uint32_t outcome;
    if (!g3d_submit(command,vcount,tcount,zbase,limit)) { outcome=G3D_BUSY; capture(reg(G3D_STATUS)); }
    else outcome=g3d_wait(budget);
    if (outcome==G3D_DONE) return 1;
    const struct g3d_result *r=&last_result;
    field("G2 failure command=",command); field(" outcome=",outcome); field(" status=",r->status);
    field(" error=",r->error); field(" fault_pc=",r->fault_pc); field(" cycles=",r->cycles);
    field(" stalls=",r->stalls); field(" instructions=",r->instructions); field(" transfers=",r->transfers);
    field(" divides=",r->divides); field(" pixels=",r->pixels); field(" zfail=",r->zfail);
    field(" culled=",r->culled); rv32_putc('\n');
    return 0;
}

uint32_t g3d_hash(const volatile uint32_t *words, uint32_t count, uint32_t h)
{
    for (uint32_t i=0;i<count;i++) h=((h<<5)+h)^words[i];
    return h;
}
