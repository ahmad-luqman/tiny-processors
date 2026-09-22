/* rv32emu_core: the RV32 machine as a library; see rv32emu_core.h for the
 * API and docs/rv32-emulator.md for the record. Every device model here
 * mirrors a module of rtl/rv32; the two are compared trace for trace.
 */
#define _POSIX_C_SOURCE 200809L
#ifdef __APPLE__
#define _DARWIN_C_SOURCE 1 /* O_NOFOLLOW is hidden under the strict POSIX define */
#endif
#include "rv32emu_core.h"
#include "rv32_fp.h"

#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <sys/stat.h>
#include <unistd.h>

const char *emu_prog = "rv32emu";

enum cause {
    CAUSE_FETCH_MISALIGNED = 0,
    CAUSE_FETCH_FAULT = 1,
    CAUSE_ILLEGAL = 2,
    CAUSE_BREAKPOINT = 3,
    CAUSE_LOAD_MISALIGNED = 4,
    CAUSE_LOAD_FAULT = 5,
    CAUSE_STORE_MISALIGNED = 6,
    CAUSE_STORE_FAULT = 7,
    CAUSE_ECALL_M = 11,
};

/* The only CSRs that exist; every other number is an illegal instruction. */
enum csr { CSR_FFLAGS = 0x001, CSR_FRM = 0x002, CSR_FCSR = 0x003, CSR_MTVEC = 0x305, CSR_MEPC = 0x341, CSR_MCAUSE = 0x342, CSR_MTVAL = 0x343 };
typedef enum { ACC_OK, ACC_FAULT, ACC_MISALIGNED } mem_access; /* not `access`: unistd.h owns that name */

/* Every window of the memory map is a region with a load and a store
 * handler, or NULL when that direction is undefined. A handler receives the
 * offset inside the window and decides the width and offset rules of its
 * device; anything it refuses, and every address outside every window, is an
 * access fault. This mirrors rtl/rv32/rv32_bus.v: one comparator per window,
 * then the device's own decode. */
typedef mem_access (*load_handler)(machine *m, uint32_t offset, int width, uint32_t *value);
typedef mem_access (*store_handler)(machine *m, uint32_t offset, int width, uint32_t value);

typedef struct {
    const char *name;
    uint32_t base, size;
    load_handler load;
    store_handler store;
} region;

static uint32_t bytes_read(const uint8_t *p, int width)
{
    uint32_t value = 0;
    for (int i = width - 1; i >= 0; i--) {
        value = (value << 8) | p[i];
    }
    return value;
}

static void bytes_write(uint8_t *p, int width, uint32_t value)
{
    for (int i = 0; i < width; i++) {
        p[i] = (uint8_t)(value >> (8 * i));
    }
}

static mem_access ram_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    *value = bytes_read(m->ram + offset, width);
    return ACC_OK;
}

static mem_access ram_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    bytes_write(m->ram + offset, width, value);
    return ACC_OK;
}

static mem_access console_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    (void)m;
    if (width == 1 && offset == CONSOLE_STATUS) {
        *value = CONSOLE_TX_READY; /* always ready: every byte is accepted at once */
        return ACC_OK;
    }
    return ACC_FAULT; /* TX is write-only; the status is a byte; other offsets do not exist */
}

static mem_access console_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    (void)m;
    if (width == 1 && offset == CONSOLE_TX) {
        fputc((int)(value & 0xff), stdout);
        return ACC_OK;
    }
    return ACC_FAULT; /* the status is read-only; TX takes bytes only */
}

static mem_access done_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    if (width == 4 && offset == 0) {
        m->halt = HALT_DONE; /* the store still retires; the loop stops afterwards */
        m->done_word = value;
        return ACC_OK;
    }
    return ACC_FAULT; /* byte and halfword writes */
}

/* Device time (docs/rv32.md): a tick is one executed instruction. The load
 * runs before this instruction is counted, so instruction N reads N - 1
 * plus whatever a write added. */
static mem_access timer_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    if (width == 4 && offset == TIMER_TICKS) {
        *value = (uint32_t)m->steps + m->timer_offset;
        return ACC_OK;
    }
    return ACC_FAULT;
}

static mem_access timer_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    if (width == 4 && offset == TIMER_TICKS) {
        m->timer_offset = value - (uint32_t)m->steps;
        return ACC_OK;
    }
    return ACC_FAULT;
}

/* Input (docs/rv32.md): the host queues an event when its frame is reached,
 * or drops it and says so when the queue is full; KEYS follows arrivals. The
 * record, when there is one, gets every offered event as a script line
 * before the queue decides, so replaying it reproduces the drops too. */
void emu_queue_event(machine *m, uint32_t frame, uint32_t event)
{
    if (m->record) {
        const char *name = emu_key_name((int)(event & 31u));
        if (name) {
            fprintf(m->record, "frame %" PRIu32 " %s %s\n", frame, (event & EVENT_PRESS) ? "down" : "up", name);
        } else {
            fprintf(m->record, "frame %" PRIu32 " %s %" PRIu32 "\n", frame, (event & EVENT_PRESS) ? "down" : "up", event & 31u);
        }
    }
    if (m->count == INPUT_QUEUE) {
        fprintf(stderr, "%s: input queue full: dropped frame %" PRIu32 " event %08" PRIx32 "\n", emu_prog, frame, event);
        m->dropped++;
        return;
    }
    m->queue[(m->head + m->count) % INPUT_QUEUE] = event;
    m->count++;
    uint32_t bit = 1u << (event & 31u);
    m->keys = (event & EVENT_PRESS) ? (m->keys | bit) : (m->keys & ~bit);
}

void emu_deliver_events(machine *m)
{
    while (m->next_scripted < m->scripted && m->script[m->next_scripted].frame <= m->frames) {
        emu_queue_event(m, m->script[m->next_scripted].frame, m->script[m->next_scripted].event);
        m->next_scripted++;
    }
}

static mem_access input_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    if (width != 4) {
        return ACC_FAULT;
    }
    switch (offset) {
    case INPUT_EVENT:
        if (m->count == 0) {
            *value = 0;
        } else {
            *value = m->queue[m->head];
            m->head = (m->head + 1) % INPUT_QUEUE;
            m->count--;
        }
        return ACC_OK;
    case INPUT_COUNT: *value = m->count; return ACC_OK;
    case INPUT_KEYS: *value = m->keys; return ACC_OK;
    default: return ACC_FAULT;
    }
}

static mem_access fb_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    *value = bytes_read(m->fb + offset, width);
    return ACC_OK;
}

static mem_access fb_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    bytes_write(m->fb + offset, width, value);
    return ACC_OK;
}

/* The checkpoint hash (docs/rv32.md, "Display"): h = ((h << 5) + h) ^ word
 * from 5381 over the framebuffer's little-endian words in address order;
 * shift, add, xor, no multiply, so the firmware can compute the same value
 * over a frame it reads back. */
uint32_t emu_frame_hash(const uint8_t *pixels)
{
    uint32_t h = 5381u;
    for (uint32_t i = 0; i < FB_SIZE; i += 4) {
        h = ((h << 5) + h) ^ bytes_read(pixels + i, 4);
    }
    return h;
}

/* The fixed RGB332 mapping (bits 7:5 red, 4:2 green, 1:0 blue, each scaled
 * to 0..255), shared by the PPM writer and the window's table. */
void emu_rgb332(uint8_t pixel, uint8_t rgb[3])
{
    rgb[0] = (uint8_t)(((pixel >> 5) & 7u) * 255u / 7u);
    rgb[1] = (uint8_t)(((pixel >> 2) & 7u) * 255u / 7u);
    rgb[2] = (uint8_t)((pixel & 3u) * 255u / 3u);
}

/* Write the frame as a binary PPM so it can be looked at without the window. The file is created
 * exclusively: a frame file that already exists, whatever it is (a stale frame, a link to one of
 * the run's own files), is never overwritten, so the run is rejected instead. The runner deletes
 * the previous run's frames before it starts. */
static bool write_ppm(const machine *m, const char *path)
{
    FILE *out = fopen(path, "wbx");
    if (!out) {
        return false;
    }
    fprintf(out, "P6\n%u %u\n255\n", FB_COLUMNS, FB_ROWS);
    for (uint32_t i = 0; i < FB_SIZE; i++) {
        uint8_t rgb[3];
        emu_rgb332(m->fb[i], rgb);
        fwrite(rgb, 1, 3, out);
    }
    bool ok = !ferror(out);
    return fclose(out) == 0 && ok;
}

/* A present: the snapshot is taken now, before the storing instruction
 * retires, and becomes checkpoint `frame N <hash>` and, when asked for, a
 * picture. Nothing about the framebuffer changes. */
static void present(machine *m)
{
    m->frames++;
    emu_deliver_events(m); /* this frame's scripted events arrive with the snapshot */
    if (m->checkpoints) {
        fprintf(m->checkpoints, "frame %" PRIu32 " %08" PRIx32 "\n", m->frames, emu_frame_hash(m->fb));
    }
    if (m->frames_dir) {
        char path[4096];
        int n = snprintf(path, sizeof path, "%s/frame-%04" PRIu32 ".ppm", m->frames_dir, m->frames);
        if (n < 0 || (size_t)n >= sizeof path || !write_ppm(m, path)) {
            fprintf(stderr, "%s: cannot write frame %" PRIu32 " to %s (%s; an existing frame file is never overwritten)\n",
                    emu_prog, m->frames, m->frames_dir, strerror(errno));
            m->output_error = true;
        }
    }
}

static mem_access display_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    if (width != 4) {
        return ACC_FAULT;
    }
    switch (offset) {
    case DISPLAY_FRAMES: *value = m->frames; return ACC_OK;
    case DISPLAY_WIDTH: *value = FB_COLUMNS; return ACC_OK;
    case DISPLAY_HEIGHT: *value = FB_ROWS; return ACC_OK;
    default: return ACC_FAULT; /* PRESENT is write-only */
    }
}

static mem_access display_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    (void)value; /* any word presents */
    if (width == 4 && offset == DISPLAY_PRESENT) {
        present(m);
        return ACC_OK;
    }
    return ACC_FAULT; /* FRAMES, WIDTH, and HEIGHT are read-only */
}

/* Region callbacks normalize their offsets to the command-window base. */
static mem_access simd_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    return simd_access(&m->simd, SIMD_BASE + offset, width, false, value) ? ACC_OK : ACC_FAULT;
}

static mem_access simd_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    return simd_access(&m->simd, SIMD_BASE + offset, width, true, &value) ? ACC_OK : ACC_FAULT;
}

static mem_access simd_program_load(machine *m, uint32_t offset, int width, uint32_t *value)
{ return simd_load(m, SIMD_PROGRAM - SIMD_BASE + offset, width, value); }
static mem_access simd_program_store(machine *m, uint32_t offset, int width, uint32_t value)
{ return simd_store(m, SIMD_PROGRAM - SIMD_BASE + offset, width, value); }
static mem_access simd_data_load(machine *m, uint32_t offset, int width, uint32_t *value)
{ return simd_load(m, SIMD_DATA - SIMD_BASE + offset, width, value); }
static mem_access simd_data_store(machine *m, uint32_t offset, int width, uint32_t value)
{ return simd_store(m, SIMD_DATA - SIMD_BASE + offset, width, value); }

/* The memory map (docs/rv32.md). RAM is last only for readability; the
 * windows are disjoint so the order does not matter. */
static const region REGIONS[] = {
    {"done", DONE_ADDR, 4, NULL, done_store},
    {"console", CONSOLE_BASE, 8, console_load, console_store},
    {"timer", TIMER_BASE, 16, timer_load, timer_store},
    {"input", INPUT_BASE, 16, input_load, NULL},
    {"display", DISPLAY_BASE, 16, display_load, display_store},
    {"framebuffer", FB_BASE, FB_SIZE, fb_load, fb_store},
    {"simd4", SIMD_BASE, 32, simd_load, simd_store},
    {"simd4_program", SIMD_PROGRAM, 1024, simd_program_load, simd_program_store},
    {"simd4_data", SIMD_DATA, 1024, simd_data_load, simd_data_store},
    {"ram", RAM_BASE, RAM_SIZE, ram_load, ram_store},
};

/* The window that holds every byte of the access, or NULL. */
static const region *find_region(uint32_t addr, int width)
{
    for (size_t i = 0; i < sizeof REGIONS / sizeof REGIONS[0]; i++) {
        const region *r = &REGIONS[i];
        if (addr >= r->base && addr - r->base + (uint32_t)width <= r->size) {
            return r;
        }
    }
    return NULL;
}

static bool in_ram(uint32_t addr, int width)
{
    return addr >= RAM_BASE && addr - RAM_BASE + (uint32_t)width <= RAM_SIZE;
}

static uint32_t ram_read(const machine *m, uint32_t addr, int width)
{
    return bytes_read(m->ram + (addr - RAM_BASE), width);
}

/* Data load. Misalignment is checked before the address is decoded. */
static mem_access load(machine *m, uint32_t addr, int width, uint32_t *value)
{
    if (addr % (uint32_t)width) {
        return ACC_MISALIGNED;
    }
    const region *r = find_region(addr, width);
    if (!r || !r->load) {
        return ACC_FAULT; /* unmapped, or a write-only window such as the done register */
    }
    return r->load(m, addr - r->base, width, value);
}

static mem_access store(machine *m, uint32_t addr, int width, uint32_t value)
{
    if (addr % (uint32_t)width) {
        return ACC_MISALIGNED;
    }
    const region *r = find_region(addr, width);
    if (!r || !r->store) {
        return ACC_FAULT; /* unmapped, or a read-only window */
    }
    return r->store(m, addr - r->base, width, value);
}

static void trace_effects(const machine *m)
{
    if (!m->trace) {
        return;
    }
    if (m->wr_reg > 0) {
        fprintf(m->trace, " x%d=%08" PRIx32, m->wr_reg, m->wr_value);
    }
    if (m->wr_freg >= 0) fprintf(m->trace, " f%d=%08" PRIx32, m->wr_freg, m->f[m->wr_freg]);
    if (m->wr_fcsr) fprintf(m->trace, " fcsr=%02x", m->fcsr);
    if (m->mem_write) {
        fprintf(m->trace, " mem[%08" PRIx32 "]<-%08" PRIx32 "/%d", m->mem_addr, m->mem_value, m->mem_width);
    }
    if (m->mem_read) {
        fprintf(m->trace, " mem[%08" PRIx32 "]->%08" PRIx32 "/%d", m->mem_addr, m->mem_value, m->mem_width);
    }
}

/* Deliver a trap for the instruction at m->pc. The instruction does not
 * retire. If the previous trap's handler has not yet retired an instruction,
 * the machine cannot make progress (the M1 firmware leaves mtvec at 0, so its
 * handler would be fetched from unmapped memory): halt and report both traps. */
static void trap(machine *m, uint32_t word, uint32_t cause, uint32_t tval)
{
    simd_tick(&m->simd, false);
    m->steps++;
    if (m->trace) {
        fprintf(m->trace, "%" PRIu64 " %08" PRIx32 " %08" PRIx32 " trap %" PRIu32 " %08" PRIx32 "\n",
                m->steps, m->pc, word, cause, tval);
    }
    if (m->in_trap) {
        m->halt = HALT_DOUBLE_FAULT;
        m->second_cause = cause;
        m->second_tval = tval;
        return;
    }
    m->in_trap = true;
    m->traps++;
    m->mepc = m->pc;
    m->mcause = cause;
    m->mtval = tval;
    m->pc = m->mtvec;
}

static bool csr_read(const machine *m, uint32_t number, uint32_t *value)
{
    switch (number) {
    case CSR_FFLAGS: *value = m->fcsr & 31u; return true;
    case CSR_FRM: *value = m->fcsr >> 5; return true;
    case CSR_FCSR: *value = m->fcsr; return true;
    case CSR_MTVEC: *value = m->mtvec; return true;
    case CSR_MEPC: *value = m->mepc; return true;
    case CSR_MCAUSE: *value = m->mcause; return true;
    case CSR_MTVAL: *value = m->mtval; return true;
    default: return false;
    }
}

static void csr_write(machine *m, uint32_t number, uint32_t value)
{
    switch (number) {
    case CSR_FFLAGS: m->fcsr = (m->fcsr & 0xe0u) | (value & 31u); m->wr_fcsr = true; break;
    case CSR_FRM: m->fcsr = (m->fcsr & 31u) | ((value & 7u) << 5); m->wr_fcsr = true; break;
    case CSR_FCSR: m->fcsr = value & 255u; m->wr_fcsr = true; break;
    case CSR_MTVEC: m->mtvec = value & ~3u; break; /* direct mode only (WARL) */
    case CSR_MEPC: m->mepc = value & ~3u; break;   /* IALIGN is 32 */
    case CSR_MCAUSE: m->mcause = value; break;
    case CSR_MTVAL: m->mtval = value; break;
    }
}

/* Branch and jump targets must be word aligned: there are no compressed
 * instructions. The trap reports the jump's own PC in mepc and the target in
 * mtval, and the jump writes nothing. */
static bool jump(machine *m, uint32_t word, uint32_t target, uint32_t *next)
{
    if (target & 3u) {
        trap(m, word, CAUSE_FETCH_MISALIGNED, target);
        return false;
    }
    *next = target;
    return true;
}

static int width_of(uint32_t funct3)
{
    return 1 << (funct3 & 3u);
}

static void step(machine *m)
{
    uint32_t pc = m->pc, word, next = pc + 4;
    m->wr_reg = m->wr_freg = -1;
    m->wr_fcsr = false;
    m->mem_read = m->mem_write = false;
    if (pc & 3u) { /* unreachable through the checked paths, kept as a guard */
        trap(m, 0, CAUSE_FETCH_MISALIGNED, pc);
        return;
    }
    if (!in_ram(pc, 4)) {
        trap(m, 0, CAUSE_FETCH_FAULT, pc);
        return;
    }
    word = ram_read(m, pc, 4);
    uint32_t opcode = word & 0x7fu, rd = (word >> 7) & 31u, funct3 = (word >> 12) & 7u;
    uint32_t rs1 = (word >> 15) & 31u, rs2 = (word >> 20) & 31u, funct7 = word >> 25;
    uint32_t a = m->x[rs1], b = m->x[rs2];
    int32_t imm_i = (int32_t)word >> 20;
    int32_t imm_s = ((int32_t)(word & 0xfe000000u) >> 20) | (int32_t)((word >> 7) & 0x1fu);
    int32_t imm_b = (int32_t)(((int32_t)(word & 0x80000000u) >> 19) | (int32_t)((word & 0x80u) << 4)
                              | (int32_t)((word >> 20) & 0x7e0u) | (int32_t)((word >> 7) & 0x1eu));
    int32_t imm_j = (int32_t)(((int32_t)(word & 0x80000000u) >> 11) | (int32_t)(word & 0xff000u)
                              | (int32_t)((word >> 9) & 0x800u) | (int32_t)((word >> 20) & 0x7feu));
    uint32_t result = 0;
    bool writes_rd = false, writes_fd = false;
    mem_access status;

    switch (opcode) {
    case 0x37: /* LUI */
        result = word & 0xfffff000u;
        writes_rd = true;
        break;
    case 0x17: /* AUIPC */
        result = pc + (word & 0xfffff000u);
        writes_rd = true;
        break;
    case 0x6f: /* JAL */
        if (!jump(m, word, pc + (uint32_t)imm_j, &next)) {
            return;
        }
        result = pc + 4;
        writes_rd = true;
        break;
    case 0x67: /* JALR */
        if (funct3 != 0) {
            goto illegal;
        }
        if (!jump(m, word, (a + (uint32_t)imm_i) & ~1u, &next)) {
            return;
        }
        result = pc + 4;
        writes_rd = true;
        break;
    case 0x63: { /* branches */
        bool taken;
        switch (funct3) {
        case 0: taken = a == b; break;
        case 1: taken = a != b; break;
        case 4: taken = (int32_t)a < (int32_t)b; break;
        case 5: taken = (int32_t)a >= (int32_t)b; break;
        case 6: taken = a < b; break;
        case 7: taken = a >= b; break;
        default: goto illegal;
        }
        if (taken && !jump(m, word, pc + (uint32_t)imm_b, &next)) {
            return;
        }
        break;
    }
    case 0x07: /* FLW */
    case 0x03: { /* loads */
        if ((opcode == 0x07 && funct3 != 2) || funct3 == 3 || funct3 > 5) {
            goto illegal;
        }
        int width = width_of(funct3);
        uint32_t addr = a + (uint32_t)imm_i, value;
        status = load(m, addr, width, &value);
        if (status != ACC_OK) {
            trap(m, word, status == ACC_MISALIGNED ? CAUSE_LOAD_MISALIGNED : CAUSE_LOAD_FAULT, addr);
            return;
        }
        m->mem_read = true;
        m->mem_addr = addr;
        m->mem_value = value;
        m->mem_width = width;
        if (funct3 == 0) {
            value = (uint32_t)(int32_t)(int8_t)value;
        } else if (funct3 == 1) {
            value = (uint32_t)(int32_t)(int16_t)value;
        }
        result = value;
        writes_rd = opcode == 0x03;
        writes_fd = opcode == 0x07;
        break;
    }
    case 0x27: /* FSW */
    case 0x23: { /* stores */
        if ((opcode == 0x27 && funct3 != 2) || funct3 > 2) {
            goto illegal;
        }
        int width = width_of(funct3);
        uint32_t addr = a + (uint32_t)imm_s;
        if (opcode == 0x27) b = m->f[rs2];
        uint32_t value = width == 4 ? b : b & ((1u << (8 * width)) - 1u);
        status = store(m, addr, width, value);
        if (status != ACC_OK) {
            trap(m, word, status == ACC_MISALIGNED ? CAUSE_STORE_MISALIGNED : CAUSE_STORE_FAULT, addr);
            return;
        }
        m->mem_write = true;
        m->mem_addr = addr;
        m->mem_value = value;
        m->mem_width = width;
        break;
    }
    case 0x13: { /* OP-IMM */
        uint32_t imm = (uint32_t)imm_i, shamt = rs2;
        switch (funct3) {
        case 0: result = a + imm; break;
        case 1:
            if (funct7 != 0) {
                goto illegal;
            }
            result = a << shamt;
            break;
        case 2: result = (int32_t)a < imm_i; break;
        case 3: result = a < imm; break;
        case 4: result = a ^ imm; break;
        case 5:
            if (funct7 == 0) {
                result = a >> shamt;
            } else if (funct7 == 0x20) {
                result = (uint32_t)((int32_t)a >> shamt);
            } else {
                goto illegal;
            }
            break;
        case 6: result = a | imm; break;
        case 7: result = a & imm; break;
        }
        writes_rd = true;
        break;
    }
    case 0x33: { /* OP */
        if (funct7 != 0 && !(funct7 == 0x20 && (funct3 == 0 || funct3 == 5))) {
            goto illegal;
        }
        uint32_t shamt = b & 31u;
        switch (funct3) {
        case 0: result = funct7 ? a - b : a + b; break;
        case 1: result = a << shamt; break;
        case 2: result = (int32_t)a < (int32_t)b; break;
        case 3: result = a < b; break;
        case 4: result = a ^ b; break;
        case 5: result = funct7 ? (uint32_t)((int32_t)a >> shamt) : a >> shamt; break;
        case 6: result = a | b; break;
        case 7: result = a & b; break;
        }
        writes_rd = true;
        break;
    }
    case 0x0f: /* FENCE: one hart, no caches, nothing to order. FENCE.I is not in the contract. */
        if (funct3 != 0) {
            goto illegal;
        }
        break;
    case 0x43: case 0x47: case 0x4b: case 0x4f: case 0x53: {
        uint32_t fa = m->f[rs1], fb = m->f[rs2], fc = m->f[word >> 27];
        unsigned op = OP_ADD, rm = 0;
        bool arithmetic = true, rounded = false;
        writes_fd = true;
        if (opcode != 0x53) {
            if (((word >> 25) & 3u) != 0) goto illegal;
            op = OP_FMADD + ((opcode - 0x43) >> 2);
            rounded = true;
        } else switch (funct7) {
        case 0x00: op = OP_ADD; rounded = true; break;
        case 0x04: op = OP_SUB; rounded = true; break;
        case 0x08: op = OP_MUL; rounded = true; break;
        case 0x0c: op = OP_DIV; rounded = true; break;
        case 0x2c:
            if (rs2) goto illegal;
            op = OP_SQRT; rounded = true; break;
        case 0x60: case 0x68:
            if (rs2 > 1) goto illegal;
            rounded = true;
            if (funct7 == 0x60) {
                op = rs2 ? OP_F32_TO_U32 : OP_F32_TO_I32;
                writes_fd = false; writes_rd = true;
            } else { op = rs2 ? OP_U32_TO_F32 : OP_I32_TO_F32; fa = a; }
            break;
        case 0x50:
            if (funct3 > 2) goto illegal;
            op = funct3 == 2 ? OP_EQ : funct3 == 1 ? OP_LT : OP_LE;
            writes_fd = false; writes_rd = true; break;
        case 0x14:
            if (funct3 > 1) goto illegal;
            op = funct3 ? OP_MAX : OP_MIN; break;
        case 0x10:
            if (funct3 > 2) goto illegal;
            arithmetic = false;
            result = (fa & 0x7fffffffu) | ((funct3 == 0 ? fb : funct3 == 1 ? ~fb : fa ^ fb) & 0x80000000u);
            break;
        case 0x70: {
            if (rs2 || funct3 > 1) goto illegal;
            arithmetic = false; writes_fd = false; writes_rd = true;
            result = fa;
            if (funct3) {
                unsigned exp = (fa >> 23) & 255u, frac = fa & 0x7fffffu, sign = fa >> 31;
                unsigned bit = exp == 255 ? (frac ? ((frac & 0x400000u) ? 9 : 8) : (sign ? 0 : 7))
                    : exp == 0 ? (frac ? (sign ? 2 : 5) : (sign ? 3 : 4)) : (sign ? 1 : 6);
                result = 1u << bit;
            }
            break;
        }
        case 0x78:
            if (rs2 || funct3) goto illegal;
            arithmetic = false; result = a; break;
        default: goto illegal;
        }
        if (rounded) { rm = funct3 == 7 ? m->fcsr >> 5 : funct3; if (rm > 4) goto illegal; }
        if (arithmetic) {
            uint8_t flags;
            result = rv32_fp(op, rm, fa, fb, fc, &flags);
            m->fcsr |= flags;
            m->wr_fcsr = flags != 0;
        }
        break;
    }
    case 0x73: { /* SYSTEM */
        if (funct3 == 0) {
            if (word == 0x00000073u) {
                trap(m, word, CAUSE_ECALL_M, 0);
                return;
            }
            if (word == 0x00100073u) {
                trap(m, word, CAUSE_BREAKPOINT, pc);
                return;
            }
            if (word == 0x30200073u) { /* MRET: machine mode only, so just return */
                next = m->mepc;
                break;
            }
            goto illegal;
        }
        if (funct3 == 4) {
            goto illegal;
        }
        uint32_t number = word >> 20, old, operand = (funct3 & 4u) ? rs1 : a;
        if (!csr_read(m, number, &old)) {
            goto illegal;
        }
        switch (funct3 & 3u) {
        case 1: csr_write(m, number, operand); break;                 /* CSRRW */
        case 2: if (rs1) { csr_write(m, number, old | operand); } break;  /* CSRRS */
        case 3: if (rs1) { csr_write(m, number, old & ~operand); } break; /* CSRRC */
        }
        result = old;
        writes_rd = true;
        break;
    }
    default:
        goto illegal;
    }

    if (writes_fd) { m->f[rd] = result; m->wr_freg = (int)rd; }
    if (writes_rd && rd != 0) { /* x0 stays zero: the write is discarded, not stored */
        m->x[rd] = result;
        m->wr_reg = (int)rd;
        m->wr_value = result;
    }
    simd_tick(&m->simd, false);
    m->steps++;
    m->retired++;
    m->in_trap = false;
    if (m->trace) {
        fprintf(m->trace, "%" PRIu64 " %08" PRIx32 " %08" PRIx32, m->steps, pc, word);
        trace_effects(m);
        fputc('\n', m->trace);
    }
    m->pc = next;
    return;

illegal:
    trap(m, word, CAUSE_ILLEGAL, word);
}

const char *emu_halt_name(enum halt halt)
{
    switch (halt) {
    case HALT_DONE: return "done";
    case HALT_DOUBLE_FAULT: return "double-fault";
    case HALT_LIMIT: return "limit";
    case HALT_STOPPED: return "stopped";
    default: return "running";
    }
}

void emu_dump_state(const machine *m, FILE *out)
{
    fprintf(out, "pc %08" PRIx32 "\n", m->pc);
    for (int i = 0; i < 32; i++) {
        fprintf(out, "x%d %08" PRIx32 "\n", i, m->x[i]);
    }
    for (int i = 0; i < 32; ++i) fprintf(out, "f%d %08" PRIx32 "\n", i, m->f[i]);
    fprintf(out, "fcsr %02x\n", m->fcsr);
    fprintf(out, "mtvec %08" PRIx32 "\nmepc %08" PRIx32 "\nmcause %08" PRIx32 "\nmtval %08" PRIx32 "\n",
            m->mtvec, m->mepc, m->mcause, m->mtval);
    fprintf(out, "steps %" PRIu64 "\nretired %" PRIu64 "\ntraps %" PRIu64 "\nframes %" PRIu32 "\nevents %u\nhalt %s\n",
            m->steps, m->retired, m->traps, m->frames, m->count, emu_halt_name(m->halt));
    if (m->halt == HALT_DONE) {
        fprintf(out, "done %08" PRIx32 "\n", m->done_word);
    }
}

/* Strict unsigned parse: must start with a digit (strtoull would skip whitespace and accept a
 * sign), may use 0x/0 prefixes, and must have no trailing text and no overflow. */
uint64_t emu_parse_u64(const char *text, uint64_t max, const char *what)
{
    char *end;
    errno = 0;
    unsigned long long value = strtoull(text, &end, 0);
    if (!isdigit((unsigned char)*text) || *end != '\0' || errno == ERANGE || value > max) {
        fprintf(stderr, "%s: bad %s: %s\n", emu_prog, what, text);
        exit(EXIT_EMULATOR_ERROR);
    }
    return (uint64_t)value;
}

uint32_t emu_parse_u32(const char *text, const char *what)
{
    return (uint32_t)emu_parse_u64(text, 0xffffffffull, what);
}

/* The canonical spelling of a path that may not exist yet: the real path of its directory (which
 * must exist for the file to be created) joined with its last component. `out`, `./out`, `x//out`,
 * and `link/out` for a symlinked directory all become one string. Returns false when the directory
 * cannot be resolved; the caller then falls back to the spelling. */
static bool canonical(const char *path, char *out, size_t size)
{
    const char *slash = strrchr(path, '/');
    char directory[PATH_MAX], resolved[PATH_MAX];
    const char *name = slash ? slash + 1 : path;
    if (slash == NULL) {
        strcpy(directory, ".");
    } else if (slash == path) {
        strcpy(directory, "/");
    } else if ((size_t)(slash - path) >= sizeof directory) {
        return false;
    } else {
        memcpy(directory, path, (size_t)(slash - path));
        directory[slash - path] = '\0';
    }
    if (*name == '\0' || realpath(directory, resolved) == NULL) {
        return false;
    }
    int n = snprintf(out, size, "%s/%s", resolved, name);
    return n > 0 && (size_t)n < size;
}

/* True when both names refer to one file: same spelling, same canonical spelling, or same device
 * and inode when both exist. The canonical form catches two spellings of a file that does not
 * exist yet, which is the case for every output before the run. */
static bool same_file(const char *a, const char *b)
{
    struct stat sa, sb;
    char ca[PATH_MAX], cb[PATH_MAX];
    if (!a || !b) {
        return false;
    }
    if (strcmp(a, b) == 0) {
        return true;
    }
    if (canonical(a, ca, sizeof ca) && canonical(b, cb, sizeof cb) && strcmp(ca, cb) == 0) {
        return true;
    }
    return stat(a, &sa) == 0 && stat(b, &sb) == 0 && sa.st_dev == sb.st_dev && sa.st_ino == sb.st_ino;
}

void emu_require_distinct(const char *path, const char *what, const char *other_path, const char *other)
{
    if (same_file(path, other_path)) {
        fprintf(stderr, "%s: %s %s would overwrite the %s\n", emu_prog, what, path, other);
        exit(EXIT_EMULATOR_ERROR);
    }
}

/* Create an output file without following a symbolic link: a link can point anywhere, including
 * at a file that does not exist yet and that another output's link also points at, which no
 * comparison of names can see. Outputs are real files. NULL and a message on failure. */
FILE *emu_open_output(const char *path, const char *what)
{
    int fd = open(path, O_WRONLY | O_CREAT | O_TRUNC | O_NOFOLLOW | O_CLOEXEC, 0666);
    if (fd < 0) {
        if (errno == ELOOP || errno == EMLINK) {
            fprintf(stderr, "%s: %s %s is a symbolic link; outputs are written to real files\n", emu_prog, what, path);
        } else {
            fprintf(stderr, "%s: cannot write %s\n", emu_prog, path);
        }
        return NULL;
    }
    FILE *stream = fdopen(fd, "w");
    if (!stream) {
        fprintf(stderr, "%s: cannot write %s\n", emu_prog, path);
        close(fd);
    }
    return stream;
}

/* After the outputs are open, the last word on aliasing: two open streams on one file (same device
 * and inode) would overwrite each other however they were named. Exits with a message. */
void emu_require_distinct_streams(FILE *a, const char *a_path, const char *a_what, FILE *b, const char *b_path, const char *b_what)
{
    struct stat sa, sb;
    if (!a || !b) {
        return;
    }
    if (fstat(fileno(a), &sa) == 0 && fstat(fileno(b), &sb) == 0 && sa.st_dev == sb.st_dev && sa.st_ino == sb.st_ino) {
        fprintf(stderr, "%s: %s %s and %s %s are one file\n", emu_prog, a_what, a_path, b_what, b_path);
        exit(EXIT_EMULATOR_ERROR);
    }
}

/* Refuse a file inside a directory the run writes frames into: a present would overwrite an
 * output or an input named like a frame there, and the pairwise check above only sees a file
 * against a directory. The directory must exist for frames to be written at all; if it does not
 * resolve, the run fails on the first frame instead. */
void emu_require_outside(const char *path, const char *what, const char *directory, const char *other)
{
    char inside[PATH_MAX], resolved[PATH_MAX];
    if (!path || !directory || realpath(directory, resolved) == NULL || !canonical(path, inside, sizeof inside)) {
        return;
    }
    size_t length = strlen(resolved);
    if (strncmp(inside, resolved, length) == 0 && inside[length] == '/' && strchr(inside + length + 1, '/') == NULL) {
        fprintf(stderr, "%s: %s %s is inside the %s %s, where a frame could overwrite it\n", emu_prog, what, path, other, directory);
        exit(EXIT_EMULATOR_ERROR);
    }
}

/* Close an output stream, reporting any write error that reached it. The flush comes first so
 * the errno reported is the write's own, not whatever the last unrelated call left behind. */
bool emu_close_output(FILE *stream, const char *path)
{
    errno = 0;
    bool ok = fflush(stream) == 0 && !ferror(stream);
    int reason = errno;
    if (fclose(stream) != 0) {
        ok = false;
        reason = errno;
    }
    if (!ok) {
        fprintf(stderr, "%s: error writing %s: %s\n", emu_prog, path, reason ? strerror(reason) : "write error");
    }
    return ok;
}

/* A decimal field of the input script: one to nine ASCII digits (docs/rv32.md, "Input"), so
 * every reader, including the testbench's 32-bit arithmetic, agrees on what a number is. */
static bool decimal_ok(const char *text)
{
    size_t len = strlen(text);
    if (len == 0 || len > 9) {
        return false;
    }
    for (size_t i = 0; i < len; i++) {
        if (!isdigit((unsigned char)text[i])) {
            return false;
        }
    }
    return true;
}

/* The key table of programs/rv32/board.h; tests/test_rv32_tools.py pins the copies. */
static const struct { const char *name; int code; } KEY_NAMES[] = {
    {"LEFT", 1}, {"RIGHT", 2}, {"UP", 3}, {"DOWN", 4}, {"SPACE", 5}, {"ENTER", 6}, {"ESCAPE", 7},
    {"A", 8}, {"D", 9}, {"W", 10}, {"S", 11}, {"P", 12}, {"Q", 13}, {"R", 14},
};

const char *emu_key_name(int code)
{
    for (size_t i = 0; i < sizeof KEY_NAMES / sizeof KEY_NAMES[0]; i++) {
        if (KEY_NAMES[i].code == code) {
            return KEY_NAMES[i].name;
        }
    }
    return NULL;
}

/* A key name from programs/rv32/board.h, any case, or a number 0..31; -1 otherwise. */
int emu_key_code(const char *text)
{
    char upper[16];
    size_t len = strlen(text);
    if (len == 0 || len >= sizeof upper) {
        return -1;
    }
    for (size_t i = 0; i <= len; i++) {
        upper[i] = (char)toupper((unsigned char)text[i]);
    }
    for (size_t i = 0; i < sizeof KEY_NAMES / sizeof KEY_NAMES[0]; i++) {
        if (strcmp(upper, KEY_NAMES[i].name) == 0) {
            return KEY_NAMES[i].code;
        }
    }
    if (!decimal_ok(text)) {
        return -1;
    }
    unsigned long value = strtoul(text, NULL, 10);
    return value < 32 ? (int)value : -1;
}

/* Read the input script (docs/rv32.md, "Input"): `frame N down|up KEY` lines with frames
 * never decreasing; blank lines and `#` comments are skipped. Any other line is an error. */
void emu_read_input_script(machine *m, const char *path)
{
    FILE *in = fopen(path, "r");
    if (!in) {
        fprintf(stderr, "%s: cannot open input script %s\n", emu_prog, path);
        exit(EXIT_EMULATOR_ERROR);
    }
    char line[256];
    size_t capacity = 0;
    uint32_t last_frame = 0;
    for (unsigned number = 1; fgets(line, sizeof line, in); number++) {
        char keyword[16], frame_text[16], direction[16], key[16], rest[16];
        if (strchr(line, '\n') == NULL && !feof(in)) {
            fprintf(stderr, "%s: input script %s line %u is too long\n", emu_prog, path, number);
            exit(EXIT_EMULATOR_ERROR);
        }
        if (sscanf(line, " %15s", keyword) != 1 || keyword[0] == '#') {
            continue;
        }
        int fields = sscanf(line, " %15s %15s %15s %15s %15s", keyword, frame_text, direction, key, rest);
        unsigned long frame = 0;
        int code = -1;
        if (fields == 4 && decimal_ok(frame_text)) { /* the buffers are only filled when every field was read */
            frame = strtoul(frame_text, NULL, 10);
            code = emu_key_code(key);
        }
        if (fields != 4 || strcmp(keyword, "frame") != 0 || (strcmp(direction, "down") != 0 && strcmp(direction, "up") != 0) ||
            !decimal_ok(frame_text) || code < 0 || frame < last_frame) {
            fprintf(stderr, "%s: input script %s line %u: expected `frame N down|up KEY` with frames in order: %s", emu_prog,
                    path, number, line);
            exit(EXIT_EMULATOR_ERROR);
        }
        if (m->scripted == capacity) {
            capacity = capacity ? 2 * capacity : 64;
            m->script = realloc(m->script, capacity * sizeof *m->script);
            if (!m->script) {
                fprintf(stderr, "%s: cannot allocate the input script\n", emu_prog);
                exit(EXIT_EMULATOR_ERROR);
            }
        }
        m->script[m->scripted].frame = (uint32_t)frame;
        m->script[m->scripted].event = EVENT_VALID | (direction[0] == 'd' ? EVENT_PRESS : 0u) | (uint32_t)code;
        m->scripted++;
        last_frame = (uint32_t)frame;
    }
    /* fopen succeeds on a directory and fgets then fails at once: without this check such a
     * script would be an empty one, and a diagnostic waiting for its events would fail on a
     * device check instead of on the path. */
    if (ferror(in)) {
        fprintf(stderr, "%s: cannot read input script %s: %s\n", emu_prog, path, strerror(errno));
        exit(EXIT_EMULATOR_ERROR);
    }
    fclose(in);
}

void emu_init(machine *m)
{
    memset(m, 0, sizeof *m);
    simd_reset(&m->simd);
    m->limit = 100000000ull;
}

bool emu_alloc(machine *m)
{
    m->ram = calloc(RAM_SIZE, 1);
    m->fb = calloc(FB_SIZE, 1); /* unspecified by the contract; zero like the RTL testbench */
    if (!m->ram || !m->fb) {
        fprintf(stderr, "%s: cannot allocate memory\n", emu_prog);
        return false;
    }
    return true;
}

bool emu_load_image(machine *m, const char *path, uint32_t base, size_t *loaded)
{
    FILE *image = fopen(path, "rb");
    if (!image) {
        fprintf(stderr, "%s: cannot open %s\n", emu_prog, path);
        return false;
    }
    if (base < RAM_BASE || base - RAM_BASE >= RAM_SIZE) {
        fprintf(stderr, "%s: base %08" PRIx32 " is outside RAM\n", emu_prog, base);
        fclose(image);
        return false;
    }
    *loaded = fread(m->ram + (base - RAM_BASE), 1, RAM_SIZE - (base - RAM_BASE), image);
    if (ferror(image)) { /* a directory opens but does not read; without this it would be an empty image */
        fprintf(stderr, "%s: cannot read %s: %s\n", emu_prog, path, strerror(errno));
        fclose(image);
        return false;
    }
    if (*loaded == 0) { /* an empty image would run as an illegal instruction at the reset PC */
        fprintf(stderr, "%s: %s is empty\n", emu_prog, path);
        fclose(image);
        return false;
    }
    if (fgetc(image) != EOF) {
        fprintf(stderr, "%s: %s does not fit in RAM at %08" PRIx32 "\n", emu_prog, path, base);
        fclose(image);
        return false;
    }
    fclose(image);
    return true;
}

void emu_free(machine *m)
{
    free(m->ram);
    free(m->fb);
    free(m->script);
    m->ram = m->fb = NULL;
    m->script = NULL;
}

/* The run loop. A present is reported after the presenting store's step, so a
 * host that queues its events on return does so before any further
 * instruction: to the guest that is the same moment the scripted events of
 * the frame arrived. Budget exhaustion lets a host pump its own events while
 * a guest that never presents keeps running. */
emu_stop emu_run_until(machine *m, uint64_t budget)
{
    uint32_t frames = m->frames;
    uint64_t end = budget > UINT64_MAX - m->steps ? UINT64_MAX : m->steps + budget;
    while (m->halt == RUNNING) {
        if (m->steps >= m->limit) {
            m->halt = HALT_LIMIT;
            break;
        }
        if (m->steps >= end) {
            return EMU_STOP_BUDGET;
        }
        step(m);
        if (m->frames != frames) {
            return EMU_STOP_PRESENTED;
        }
    }
    return EMU_STOP_HALTED;
}

bool emu_finish_outputs(machine *m, const char *trace_path, const char *checkpoints_path, const char *record_path)
{
    bool ok = true;
    if (fflush(stdout) != 0 || ferror(stdout)) {
        fprintf(stderr, "%s: error writing console output: %s\n", emu_prog, strerror(errno));
        ok = false;
    }
    if (m->trace && !emu_close_output(m->trace, trace_path)) {
        ok = false;
    }
    if (m->checkpoints && !emu_close_output(m->checkpoints, checkpoints_path)) {
        ok = false;
    }
    if (m->record && !emu_close_output(m->record, record_path)) {
        ok = false;
    }
    m->trace = m->checkpoints = m->record = NULL;
    if (m->output_error) {
        ok = false;
    }
    return ok;
}

int emu_report_halt(const machine *m, size_t loaded)
{
    if (m->next_scripted < m->scripted) {
        fprintf(stderr, "%s: %zu scripted event(s) never delivered (first: frame %" PRIu32 ")\n", emu_prog,
                m->scripted - m->next_scripted, m->script[m->next_scripted].frame);
    }
    int status = EXIT_EMULATOR_ERROR;
    fprintf(stderr, "%s: halt=%s steps=%" PRIu64 " retired=%" PRIu64 " traps=%" PRIu64 " loaded=%zu", emu_prog,
            emu_halt_name(m->halt), m->steps, m->retired, m->traps, loaded);
    switch (m->halt) {
    case HALT_DONE:
        fprintf(stderr, " done=%08" PRIx32, m->done_word);
        if (m->done_word == DONE_PASS) {
            fputs(" pass\n", stderr);
            status = 0;
        } else if ((m->done_word & 0xffffu) == DONE_FAIL && (m->done_word >> 16) >= 1 && (m->done_word >> 16) <= 255) {
            status = (int)(m->done_word >> 16);
            fprintf(stderr, " fail=%d\n", status);
        } else if (m->done_word == DONE_RESET) {
            fputs(" error=reserved-reset-word\n", stderr);
        } else {
            fputs(" error=undefined-done-word\n", stderr);
        }
        break;
    case HALT_DOUBLE_FAULT:
        fprintf(stderr, " error=unhandled-trap mcause=%" PRIu32 " mepc=%08" PRIx32 " mtval=%08" PRIx32
                " then mcause=%" PRIu32 " mtval=%08" PRIx32 " at pc=%08" PRIx32 "\n",
                m->mcause, m->mepc, m->mtval, m->second_cause, m->second_tval, m->pc);
        break;
    case HALT_LIMIT:
        fprintf(stderr, " error=instruction-limit pc=%08" PRIx32 "\n", m->pc);
        break;
    case HALT_STOPPED:
        fprintf(stderr, " error=host-stopped pc=%08" PRIx32 "\n", m->pc);
        break;
    default:
        fputc('\n', stderr);
    }
    return status;
}

int emu_exit_status(const machine *m, int status, bool outputs_ok, bool allow_lost_events)
{
    if (!outputs_ok) {
        fprintf(stderr, "%s: outputs incomplete, run rejected\n", emu_prog);
        return EXIT_EMULATOR_ERROR;
    }
    /* The script and the program disagreed: the guest's pass says nothing about the events it
     * never saw, so a passing run is rejected unless the caller meant it (a drop test). A guest
     * that failed or was halted keeps its own status; the events it missed are reported above. */
    size_t lost = m->dropped + (m->scripted - m->next_scripted);
    if (lost > 0 && !allow_lost_events && status == 0) {
        fprintf(stderr, "%s: %zu input event(s) lost, run rejected (--allow-lost-events accepts this)\n", emu_prog, lost);
        return EXIT_EMULATOR_ERROR;
    }
    return status;
}
