/* G2 programmable 3D public contract; docs/rv32-3d.md fixes the semantics.
 * tools/rv32_g3d_model.py is the independent Python oracle and
 * tests/test_rv32_3d.py pins these numbers against it. */
#ifndef RV32_G3D_H
#define RV32_G3D_H
#include <stdint.h>

#define G3D_BASE 0x20008000u
#define G3D_SIZE 0x2000u
#define G3D_COMMAND 0x00u
#define G3D_STATUS 0x04u
#define G3D_ERROR 0x08u
#define G3D_FAULT_PC 0x0cu
#define G3D_CYCLES 0x10u
#define G3D_STALLS 0x14u
#define G3D_INSTRUCTIONS 0x18u
#define G3D_TRANSFERS 0x1cu
#define G3D_DIVIDES 0x20u
#define G3D_PIXELS 0x24u
#define G3D_ZFAIL 0x28u
#define G3D_CULLED 0x2cu
#define G3D_VCOUNT 0x40u
#define G3D_TCOUNT 0x44u
#define G3D_ZBASE 0x48u
#define G3D_LIMIT 0x4cu
#define G3D_CONST 0x400u
#define G3D_PROGRAM 0x800u
#define G3D_VERTEX 0x1000u
#define G3D_TRIANGLE 0x1800u
#define G3D_START 1u
#define G3D_RESET 2u
#define G3D_CLEAR_Z 3u
/* Status is a whole-value enumeration, as in G1. */
#define G3D_IDLE 0u
#define G3D_BUSY 1u
#define G3D_DONE 2u
#define G3D_FAULT 4u
/* ERROR values. */
enum { G3D_E_NONE, G3D_E_PARAM, G3D_E_INTERNAL, G3D_E_ILLEGAL, G3D_E_OVERFLOW,
       G3D_E_MISMATCH, G3D_E_LIMIT, G3D_E_PC, G3D_E_INDEX };

#define G3D_LANES 4u
#define G3D_REGS 16u
#define G3D_PROGRAM_WORDS 128u
#define G3D_CONSTS 32u
#define G3D_SLOTS 8u
#define G3D_VMAX 32u
#define G3D_TMAX 64u
#define G3D_DEPTH 8u
#define G3D_ONE 0x10000
#define G3D_W_NEAR (G3D_ONE >> 4)
#define G3D_GUARD 4
#define G3D_SUB 16
#define G3D_Z_MAX 0xffffu
enum { G3D_OUT_X, G3D_OUT_Y, G3D_OUT_Z, G3D_OUT_W, G3D_OUT_R, G3D_OUT_G, G3D_OUT_B };

/* Opcodes: word[31:26]; rd [25:22], ra [21:18], rb [17:14], rc [13:10], imm [13:0]. */
enum { G3D_OP_END, G3D_OP_LDI, G3D_OP_LDC, G3D_OP_IN, G3D_OP_OUT, G3D_OP_SPC, G3D_OP_MOV,
       G3D_OP_ADD, G3D_OP_SUB, G3D_OP_MUL, G3D_OP_MAD, G3D_OP_MIN, G3D_OP_MAX, G3D_OP_ABS,
       G3D_OP_AND, G3D_OP_OR, G3D_OP_XOR, G3D_OP_SHL, G3D_OP_SRA, G3D_OP_ADDI, G3D_OP_SLT,
       G3D_OP_SEQ, G3D_OP_IF, G3D_OP_ELSE, G3D_OP_ENDIF, G3D_OP_LOOP, G3D_OP_ENDLOOP,
       G3D_OP_BREAK, G3D_OP_COUNT };

/* One START job as the device windows hold it. A triangle word packs vertex
 * indices i0 | i1 << 8 | i2 << 16; the top byte is ignored. */
struct g3d_job {
    const uint32_t *program;                 /* G3D_PROGRAM_WORDS */
    const uint32_t *consts;                  /* G3D_CONSTS */
    const uint32_t (*inputs)[G3D_SLOTS];     /* vcount rows */
    const uint32_t *triangles;               /* tcount words */
    uint32_t vcount, tcount, limit;
};
struct g3d_counts {
    uint32_t error, fault_pc, instructions, transfers, divides, pixels, zfail, culled;
};
/* Software reference over a caller-owned 320x240 framebuffer and Z buffer.
 * Returns the ERROR value (0 on success); every fault precedes the first
 * framebuffer or Z access, so a fault leaves both untouched. */
uint32_t g3d_reference(uint8_t *fb, uint16_t *zbuf, const struct g3d_job *job, struct g3d_counts *counts);
/* The fixed-function divider: truncating n/d for d > 0, saturated to int32. */
int32_t g3d_div_sat(int64_t n, int32_t d);
#endif
