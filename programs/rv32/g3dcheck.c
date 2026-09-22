/* G2 diagnostic: every scenario's status, error, counters and image hashes
 * against the Python oracle (tools/rv32_g3d_header.py), then timeout recovery
 * and a relaunch after RESET. The same image runs on the emulator and the RTL. */
#include "g3d.h"
#include "board.h"
#include "mmio.h"
#include "console.h"
#include "g3d_scenes.h"

#define ZBASE 0x80040000u
#define FB_WORDS (320u*240u/4u)
#define Z_WORDS (320u*240u*2u/4u)

/* The word each PRESENT writes; initialized data keeps the image's .data section non-empty. */
static uint32_t present_word=1;

static int field(const char *scene, const char *name, uint32_t got, uint32_t want)
{
    if (got==want) return 1;
    rv32_puts("G2 "); rv32_puts(scene); rv32_puts(" "); rv32_puts(name);
    rv32_puts(" got "); rv32_put_hex32(got); rv32_puts(" want "); rv32_put_hex32(want); rv32_putc('\n');
    return 0;
}

static void clear_fb(void)
{
    volatile uint32_t *fb=(volatile uint32_t *)RV32_FB_BASE;
    for (uint32_t i=0;i<FB_WORDS;i++) fb[i]=0;
}

static int clear_z(void)
{
    if (!g3d_run(G3D_CLEAR_Z,0,0,ZBASE,0,200000)) return 0;
    const struct g3d_result *r=g3d_last_result();
    return field("clear","transfers",r->transfers,Z_WORDS) &&
           field("clear","cycles",r->cycles-r->stalls,G3D_CLEAR_CYCLES);
}

static int scene(const struct g3d_scene *s)
{
    clear_fb();
    if (!clear_z()) return 0;
    if (!g3d_load_program(s->program,s->program_words) || !g3d_load(G3D_CONST,s->consts,G3D_CONSTS) ||
        !g3d_load(G3D_VERTEX,s->inputs,s->vcount*G3D_SLOTS) || !g3d_load(G3D_TRIANGLE,s->triangles,s->tcount) ||
        !g3d_submit(G3D_START,s->vcount,s->tcount,s->zbase,s->limit)) {
        rv32_puts("G2 load refused\n");
        return 0;
    }
    uint32_t status=g3d_wait(s->cycles+1000u);
    const struct g3d_result *r=g3d_last_result();
    int ok=field(s->name,"status",status,s->status) & field(s->name,"error",r->error,s->error) &
           field(s->name,"fault_pc",r->fault_pc,s->fault_pc) &
           field(s->name,"instructions",r->instructions,s->instructions) &
           field(s->name,"transfers",r->transfers,s->transfers) & field(s->name,"divides",r->divides,s->divides) &
           field(s->name,"pixels",r->pixels,s->pixels) & field(s->name,"zfail",r->zfail,s->zfail) &
           field(s->name,"culled",r->culled,s->culled) & field(s->name,"cycles",r->cycles-r->stalls,s->cycles);
    ok&=field(s->name,"fb",g3d_hash((const volatile uint32_t *)RV32_FB_BASE,FB_WORDS,5381u),s->fb_hash);
    ok&=field(s->name,"z",g3d_hash((const volatile uint32_t *)ZBASE,Z_WORDS,5381u),s->z_hash);
    return ok;
}

int main(void)
{
    g3d_reset();
    for (uint32_t i=0;i<G3D_SCENES;i++) {
        if (!scene(&g3d_scenes[i])) return (int)i+1;
        mmio_write32(RV32_DISPLAY_BASE,present_word++);
    }
    /* A budget far below the frame times out: the snapshot is taken while the
     * device is busy, RESET leaves it idle, and a relaunch reproduces scene 0. */
    const struct g3d_scene *s=&g3d_scenes[0];
    if (!g3d_submit(G3D_START,s->vcount,s->tcount,s->zbase,s->limit) || g3d_submit(G3D_START,1,0,ZBASE,1) ||
        g3d_load(G3D_PROGRAM,s->program,1) || g3d_wait(40)!=G3D_TIMEOUT ||
        g3d_last_result()->status!=G3D_BUSY || g3d_last_result()->cycles==0 ||
        mmio_read32(G3D_BASE+G3D_STATUS)!=G3D_IDLE || mmio_read32(G3D_BASE+G3D_VCOUNT)!=0) {
        rv32_puts("G2 timeout recovery failed\n");
        return 20;
    }
    if (!scene(s)) return 21;
    rv32_puts("PASS G2\n");
    return 0;
}
