#ifndef RV32_GPU_DEMO_H
#define RV32_GPU_DEMO_H
#include "gpu.h"
#include "gfx.h"
struct gpu_demo { uint32_t frame, accelerated, paused; };
typedef int (*gpu_emit)(void *context,const struct gpu_command *c,const uint8_t *source);
void gpu_demo_init(struct gpu_demo *d);
void gpu_demo_event(struct gpu_demo *d,uint32_t code);
int gpu_scene(uint32_t frame,gpu_emit emit,void *context);
int gpu_software_emit(void *context,const struct gpu_command *c,const uint8_t *source);
void gpu_demo_labels(const struct gpu_demo *d,const struct gfx_surface *s);
void gpu_demo_draw(const struct gpu_demo *d,const struct gfx_surface *s);
#endif
