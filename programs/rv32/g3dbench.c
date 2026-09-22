/* G2 cost measurement: the menu demo's frames drawn by the C reference on the
 * RV32I CPU and by the device. Built at two G3D_BENCH_COUNT values so their
 * difference cancels startup; tools/rv32_g3d_bench.py divides it down to one
 * frame. A frame is the whole job either way: clear the picture and the depth
 * buffer, then shade, project and draw the cube. The sink and the device
 * counters print as fixed-width hex so both builds do identical console work
 * (see programs/rv32/digitbench.c). A measurement, not a test.
 */
#include "g3d_demo.h"
#include "gpu.h"
#include "board.h"
#include "console.h"

#ifndef G3D_BENCH_COUNT
#define G3D_BENCH_COUNT 1u
#endif

static uint32_t sink = 1;          /* non-empty .data, and a result the optimizer keeps */
static struct g3d_demo demo;

#ifdef G3D_BENCH_CPU
static uint32_t render(void *context, const struct g3d_job *job, const struct gfx_surface *s)
{
    (void)context;
    struct g3d_counts counts;
    uint16_t *zbuf = (uint16_t *)G3D_DEMO_ZBASE;
    gfx_clear(s, G3D_DEMO_BACKGROUND);
    for (uint32_t i = 0; i < 320u*240u; i++) zbuf[i] = 0xffff;
    uint32_t e = g3d_reference(s->pixels, zbuf, job, &counts);
    sink += counts.pixels;
    return e;
}
#else
static uint32_t render(void *context, const struct g3d_job *job, const struct gfx_surface *s)
{
    (void)context; (void)s;
    struct gpu_command c;
    gpu_command_init(&c, GPU_FILL, G3D_DEMO_BACKGROUND); c.p[GP_W] = 320; c.p[GP_H] = 240;
    if (!gpu_run(&c, 2000000) || !g3d_run(G3D_CLEAR_Z, 0, 0, G3D_DEMO_ZBASE, 0, 200000)) return 1;
    if (!g3d_load(G3D_PROGRAM, job->program, job->program_words) || !g3d_load(G3D_CONST, job->consts, G3D_CONSTS) ||
        !g3d_load(G3D_VERTEX, job->inputs[0], job->vcount*G3D_SLOTS) || !g3d_load(G3D_TRIANGLE, job->triangles, job->tcount) ||
        !g3d_run(G3D_START, job->vcount, job->tcount, G3D_DEMO_ZBASE, job->limit, 2000000)) return 1;
    sink += g3d_last_result()->pixels;
    return 0;
}
#endif

int main(void)
{
    static const struct gfx_surface screen = {(uint8_t *)RV32_FB_BASE, 320, 240};
    g3d_demo_init(&demo);
    g3d_demo_attach(&demo, render, 0);
    for (uint32_t i = 0; i < G3D_BENCH_COUNT; i++) {
        demo.frame = 16u * i;
        struct g3d_job job;
        g3d_demo_job(&demo, &job);
        if (render(0, &job, &screen)) return 1;
    }
    rv32_puts("bench ");
    rv32_put_hex32(sink);
    rv32_putc('\n');
    return 0;
}
