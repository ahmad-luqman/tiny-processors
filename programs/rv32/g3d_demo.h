/* G2 menu demo: a rotating cube and three vertex shaders, device-free. The
 * host or firmware attaches the renderer: the native model renders with the C
 * reference, the firmware with the device, and both must make the same pixels. */
#ifndef RV32_G3D_DEMO_H
#define RV32_G3D_DEMO_H
#include "g3d.h"
#include "gfx.h"
enum { G3D_SHADER_DIFFUSE, G3D_SHADER_TOON, G3D_SHADER_WOBBLE, G3D_SHADERS };
#define G3D_DEMO_BACKGROUND 0x01u
#define G3D_DEMO_ZBASE 0x80040000u
/* Clear the frame to G3D_DEMO_BACKGROUND and the depth buffer to far, then draw
 * `job`; returns 0 on success and the device ERROR (or any non-zero) otherwise.
 * The firmware's renderer draws into the framebuffer and ignores `s`. */
typedef uint32_t (*g3d_renderer)(void *context, const struct g3d_job *job, const struct gfx_surface *s);
struct g3d_demo {
    uint32_t frame, shader, paused, status;
    g3d_renderer render;
    void *context;
    uint32_t consts[G3D_CONSTS];
};
void g3d_demo_init(struct g3d_demo *d);
/* Keeps the renderer attached across init, as digit_ui_attach does. */
void g3d_demo_attach(struct g3d_demo *d, g3d_renderer render, void *context);
void g3d_demo_event(struct g3d_demo *d, uint32_t code);
/* The frame's constant bank: MVP, object-space light, ambient and frame number. */
void g3d_demo_constants(uint32_t frame, uint32_t consts[G3D_CONSTS]);
void g3d_demo_job(struct g3d_demo *d, struct g3d_job *job);
void g3d_demo_draw(struct g3d_demo *d, const struct gfx_surface *s);
uint32_t g3d_demo_checksum(const struct g3d_demo *d);
#endif
