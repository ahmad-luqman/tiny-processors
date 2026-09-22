/* G2 menu demo. Everything here is integer and device-free: the renderer the
 * host attached turns a job into pixels. */
#include "g3d_demo.h"
#include "board.h"
#include "g3d_shaders.h"

/* Camera, in Q16.16 (tools/rv32_g3d_scene.py documents the same composition):
 * spin about y by the frame's angle, tilt 0.5 rad about x, move 3 away, then
 * a 60-degree perspective with near 1 and far 6 mapping z_clip into [0, w]. */
#define FX 85134        /* f * 240/320, f = 1/tan(30 degrees) */
#define FY 113512       /* f */
#define ZA (-78643)     /* far / (near - far) */
#define ZT 157286       /* translation of z_clip: -2 * ZA */
#define WT 196608       /* translation of w_clip: the distance, 3 */
#define COS_TILT 57513
#define SIN_TILT 31420
#define AMBIENT 16384
static const int32_t light[3] = {18832, 37663, 50218};   /* (0.3, 0.6, 0.8), normalized */

static const uint32_t *const programs[G3D_SHADERS] = {g3d_diffuse_program, g3d_toon_program, g3d_wobble_program};
static const uint32_t program_words[G3D_SHADERS] = {G3D_DIFFUSE_WORDS, G3D_TOON_WORDS, G3D_WOBBLE_WORDS};
static const char *const titles[G3D_SHADERS] = {"3D DIFFUSE", "3D TOON", "3D WOBBLE"};

void g3d_demo_init(struct g3d_demo *d)
{
    d->frame = d->shader = d->paused = d->status = 0;
}

void g3d_demo_attach(struct g3d_demo *d, g3d_renderer render, void *context)
{
    d->render = render;
    d->context = context;
}

void g3d_demo_event(struct g3d_demo *d, uint32_t code)
{
    if (code == RV32_KEY_SPACE) d->shader = d->shader + 1 == G3D_SHADERS ? 0 : d->shader + 1;
    if (code == RV32_KEY_P) d->paused ^= 1;
    if (code == RV32_KEY_R) d->frame = d->paused = d->status = 0;
}

void g3d_demo_constants(uint32_t frame, uint32_t k[G3D_CONSTS])
{
    uint32_t turn = (frame * 2u) & 255u;
    int32_t c = g3d_sine[(turn + 64u) & 255u], s = g3d_sine[turn];
    int32_t m[3][3] = {
        {c, 0, s},
        {g3d_qmul(SIN_TILT, s), COS_TILT, -g3d_qmul(SIN_TILT, c)},
        {-g3d_qmul(COS_TILT, s), SIN_TILT, g3d_qmul(COS_TILT, c)},
    };
    for (uint32_t i = 0; i < G3D_CONSTS; i++) k[i] = 0;
    for (uint32_t j = 0; j < 3; j++) {
        k[j] = (uint32_t)g3d_qmul(FX, m[0][j]);
        k[4 + j] = (uint32_t)g3d_qmul(FY, m[1][j]);
        k[8 + j] = (uint32_t)g3d_qmul(ZA, m[2][j]);
        k[12 + j] = (uint32_t)-m[2][j];
        /* The model is a rotation, so its transpose carries the light into object space. */
        k[16 + j] = (uint32_t)(g3d_qmul(m[0][j], light[0]) + g3d_qmul(m[1][j], light[1]) + g3d_qmul(m[2][j], light[2]));
    }
    k[11] = ZT;
    k[15] = WT;
    k[19] = AMBIENT;
    k[20] = frame & 1023u;
}

void g3d_demo_job(struct g3d_demo *d, struct g3d_job *job)
{
    g3d_demo_constants(d->frame, d->consts);
    job->program = programs[d->shader];
    job->consts = d->consts;
    job->inputs = g3d_cube_inputs;
    job->triangles = g3d_cube_triangles;
    job->vcount = G3D_CUBE_VERTICES;
    job->tcount = G3D_CUBE_TRIANGLES;
    job->limit = 4096;
    job->program_words = program_words[d->shader];
}

void g3d_demo_draw(struct g3d_demo *d, const struct gfx_surface *s)
{
    struct g3d_job job;
    g3d_demo_job(d, &job);
    if (d->render) d->status = d->render(d->context, &job, s);
    else gfx_clear(s, G3D_DEMO_BACKGROUND);
    gfx_draw_text(s, 12, 12, titles[d->shader], 3, 0xff);
    if (d->status) gfx_draw_text(s, 12, 40, "DEVICE ERROR", 2, 0xe0);
    if (d->paused) gfx_draw_text(s, 245, 12, "PAUSED", 2, 0xfc);
    gfx_draw_text(s, 12, 207, "SPACE SHADER  P PAUSE  R RESTART", 1, 0xff);
    gfx_draw_text(s, 12, 222, "ESC MENU  Q QUIT", 1, 0x92);
}

uint32_t g3d_demo_checksum(const struct g3d_demo *d)
{
    return (d->frame * 16777619u) ^ d->shader ^ (d->paused << 8) ^ (d->status << 16);
}
