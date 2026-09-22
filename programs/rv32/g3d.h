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
    /* Words of `program`; every word past them reads as 0 (END), as the device
     * window does after g3d_load_program, which zero-fills the rest. */
    uint32_t program_words;
    uint32_t zbase;                          /* validated like the device's ZBASE */
};
/* `cycles` is the device's busy-tick count with no memory stalls, from the
 * tick schedule in docs/rv32-3d.md "Time". */
struct g3d_counts {
    uint32_t error, fault_pc, instructions, transfers, divides, pixels, zfail, culled, cycles;
};
#define G3D_DIVIDE_TICKS 35u   /* LOAD, PREP, 32 restoring steps, FINISH */
#define G3D_CLEAR_CYCLES (1u+320u*240u*2u/4u+1u)
/* Software reference over a caller-owned 320x240 framebuffer and Z buffer
 * (`zbuf` is the buffer ZBASE names; the reference only validates the address).
 * Returns the ERROR value (0 on success); every fault precedes the first
 * framebuffer or Z access, so a fault leaves both untouched. */
uint32_t g3d_reference(uint8_t *fb, uint16_t *zbuf, const struct g3d_job *job, struct g3d_counts *counts);
/* The fixed-function divider: truncating n/d for d > 0, saturated to int32. */
int32_t g3d_div_sat(int64_t n, int32_t d);
/* Q16.16 product: bits 47..16 of the exact 64-bit product (floor). */
int32_t g3d_qmul(int32_t a, int32_t b);

/* Driver (programs/rv32/g3d.c), shaped like G1's. Every call that writes the
 * device refuses without mutation while it is BUSY, because those writes would
 * fault. `g3d_wait` returns the final status (IDLE, DONE or FAULT) or the
 * driver-only G3D_TIMEOUT; its budget counts status polls, not device ticks.
 * Every wait ends with a snapshot in g3d_last_result(), taken before a timeout's
 * RESET, so the counters of a lost job survive. The snapshot reads registers
 * one at a time and is atomic only once the device has stopped. */
#define G3D_TIMEOUT 8u
struct g3d_result {
    uint32_t status, error, fault_pc, cycles, stalls, instructions, transfers, divides, pixels, zfail, culled;
};
/* Copy `count` words into a window (G3D_CONST, G3D_PROGRAM, G3D_VERTEX or
 * G3D_TRIANGLE plus a word offset); 0 while BUSY or when the words would run
 * past the end of the window (the gap after it faults). */
int g3d_load(uint32_t offset, const uint32_t *words, uint32_t count);
/* Load a program and zero the rest of the window, so words past its end read
 * as END exactly as the reference and the oracle assume. */
int g3d_load_program(const uint32_t *words, uint32_t count);
int g3d_submit(uint32_t command, uint32_t vcount, uint32_t tcount, uint32_t zbase, uint32_t limit);
uint32_t g3d_wait(uint32_t budget);
const struct g3d_result *g3d_last_result(void);
/* G1 must be idle: its engine shares the memory port, and the COMMAND store
 * faults while it runs. Submit, wait, and print the snapshot on anything but DONE. */
int g3d_run(uint32_t command, uint32_t vcount, uint32_t tcount, uint32_t zbase, uint32_t limit, uint32_t budget);
/* Legal while BUSY; clears parameters, outcome and counters, keeps the windows
 * and every memory write already accepted. */
void g3d_reset(void);
/* The display checkpoint hash (docs/rv32.md "Display") over `count` words:
 * h = ((h << 5) + h) ^ word from 5381. It needs no multiply. */
uint32_t g3d_hash(const volatile uint32_t *words, uint32_t count, uint32_t h);
#endif
