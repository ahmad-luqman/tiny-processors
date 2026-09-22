#ifndef RV32_G3D_DEVICE_H
#define RV32_G3D_DEVICE_H
#include <stdint.h>
#include <stdbool.h>
#include "../programs/rv32/g3d.h"
/* G2 programmable 3D device, one tick at a time (docs/rv32-3d.md "Time").
 * The phases are the RTL's states; the arithmetic is host int64, written
 * independently of programs/rv32/g3d_ref.c and tools/rv32_g3d_model.py. */
typedef enum {
    G3D_PH_IDLE, G3D_PH_VALIDATE, G3D_PH_INDEX, G3D_PH_BATCH, G3D_PH_EXEC, G3D_PH_VCHECK,
    G3D_PH_DIVIDE, G3D_PH_FETCH, G3D_PH_AREA, G3D_PH_TEST, G3D_PH_ZREAD, G3D_PH_ZWRITE,
    G3D_PH_PWRITE, G3D_PH_FINISH, G3D_PH_CLEAR
} g3d_phase;
typedef struct { int32_t sx, sy, z, r, g, b; bool valid; } g3d_vertex;
typedef struct {
    /* CPU-visible state. Windows survive RESET; parameters, outcome and counters do not. */
    uint32_t status, error, fault_pc, cycles, stalls, instructions, transfers, divides, pixels, zfail, culled;
    uint32_t vcount, tcount, zbase, limit;
    uint32_t consts[G3D_CONSTS], program[G3D_PROGRAM_WORDS], inputs[G3D_VMAX][G3D_SLOTS], tris[G3D_TMAX];
    /* Engine. */
    g3d_phase phase; bool clearing, command_tick;
    uint32_t index;                         /* triangle during INDEX/FETCH/scan; word during CLEAR */
    uint32_t batch, lane, pc, count, mask, pred, depth;
    struct { uint8_t loop, parent, taken; } stack[G3D_DEPTH];
    uint32_t regs[G3D_LANES][G3D_REGS], out[G3D_LANES][G3D_SLOTS];
    g3d_vertex verts[G3D_VMAX];
    /* Divider: 32 ticks per divide; `div_k` indexes the running sequence. */
    uint32_t div_left, div_k; bool div_setup;
    int64_t div_num[8]; int32_t div_den; int32_t div_q[8];
    /* Triangle being drawn, after the swap that makes its area positive. */
    g3d_vertex v[3]; int32_t area, left, right, top, bottom, x, y;
    int32_t z, r, g, b;
} g3d_device;
static inline bool g3d_busy(const g3d_device *d) { return d->status==G3D_BUSY; }
void g3d_device_reset(g3d_device *d);
/* CPU access to the window. `other_busy` is the G1 engine: the two share the
 * framebuffer and RAM port, so neither starts while the other runs. */
bool g3d_access(g3d_device *d, uint32_t off, int width, bool write, uint32_t *value, bool other_busy);
void g3d_tick(g3d_device *d, uint8_t *ram, uint32_t ram_size, uint8_t *fb, bool hold);
/* CPU writes to the depth buffer fault while the engine owns it. */
bool g3d_z_locked(const g3d_device *d, uint32_t addr, int width);
#endif
