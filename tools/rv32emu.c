/* rv32emu: headless emulator for the RV32 machine (docs/rv32.md, docs/rv32-emulator.md).
 *
 * Loads a flat image (the .bin that tools/rv32_image.py flattens from the ELF)
 * into RAM, then executes RV32I plus the CSR subset of the trap contract until
 * the guest writes the done register, a trap cannot be delivered, or the
 * instruction bound is reached. Guest console bytes go to stdout and nothing
 * else does; diagnostics go to stderr; the retirement trace goes to a file.
 *
 * Build: cc -std=c11 -O2 -Wall -Wextra -Werror -o rv32emu rv32emu.c
 */
#define _POSIX_C_SOURCE 200809L
#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

/* Machine contract constants; keep in step with programs/rv32/board.h. */
#define RAM_BASE 0x80000000u
#define RAM_SIZE 0x00400000u
#define CONSOLE_BASE 0x10000000u
#define CONSOLE_TX 0x0u
#define CONSOLE_STATUS 0x5u
#define CONSOLE_TX_READY 0x20u
#define DONE_ADDR 0x00100000u
#define DONE_PASS 0x5555u
#define DONE_FAIL 0x3333u
#define DONE_RESET 0x7777u
#define TIMER_BASE 0x20000000u
#define TIMER_TICKS 0x0u
#define INPUT_BASE 0x20001000u
#define INPUT_EVENT 0x0u
#define INPUT_COUNT 0x4u
#define INPUT_KEYS 0x8u
#define INPUT_QUEUE 16u
#define EVENT_VALID 0x80000000u
#define EVENT_PRESS 0x100u
#define DISPLAY_BASE 0x20002000u
#define DISPLAY_PRESENT 0x0u
#define DISPLAY_FRAMES 0x4u
#define DISPLAY_WIDTH 0x8u
#define DISPLAY_HEIGHT 0xCu
#define FB_BASE 0x30000000u
#define FB_COLUMNS 320u
#define FB_ROWS 240u
#define FB_SIZE (FB_COLUMNS * FB_ROWS)

/* mcause values (RISC-V privileged specification, machine mode, no interrupts). */
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
enum csr { CSR_MTVEC = 0x305, CSR_MEPC = 0x341, CSR_MCAUSE = 0x342, CSR_MTVAL = 0x343 };

enum halt { RUNNING, HALT_DONE, HALT_DOUBLE_FAULT, HALT_LIMIT };

/* One line of the input script: the event and the frame it arrives at. */
typedef struct {
    uint32_t frame;
    uint32_t event;
} scripted_event;

/* Process exit status when the run did not end with a pass/fail done word. */
#define EXIT_EMULATOR_ERROR 2

typedef struct {
    uint32_t x[32];
    uint32_t pc;
    uint32_t mtvec, mepc, mcause, mtval;
    uint8_t *ram;
    uint64_t steps;   /* instructions executed: retired plus trapped */
    uint64_t retired; /* instructions whose architectural effects committed */
    uint64_t traps;
    uint64_t limit;
    FILE *trace;
    bool in_trap;     /* trap taken and no instruction of the handler has retired yet */
    enum halt halt;
    uint32_t done_word;
    uint32_t second_cause, second_tval; /* the trap that could not be delivered */
    uint32_t timer_offset; /* TICKS = steps + timer_offset; a write sets the offset */
    uint8_t *fb;           /* the framebuffer window, FB_SIZE bytes */
    uint32_t frames;       /* presents since reset */
    FILE *checkpoints;     /* one `frame N <hash>` line per present, or NULL */
    const char *frames_dir; /* directory for frame-NNNN.ppm, or NULL */
    bool output_error;     /* a frame file could not be written; the run is rejected */
    uint32_t queue[INPUT_QUEUE]; /* the input device: a ring of events */
    unsigned head, count;
    uint32_t keys;         /* one bit per key code, as events arrive */
    scripted_event *script; /* --input, in script order with frames never decreasing (the parser
                             * enforces it); delivered as frames are reached */
    size_t scripted, next_scripted;
    size_t dropped;         /* events the full queue refused; the run is rejected unless allowed */
    /* Effects of the current step, for the trace line. */
    int wr_reg;
    uint32_t wr_value;
    bool mem_read, mem_write;
    uint32_t mem_addr, mem_value;
    int mem_width;
} machine;

typedef enum { ACC_OK, ACC_FAULT, ACC_MISALIGNED } access;

/* Every window of the memory map is a region with a load and a store
 * handler, or NULL when that direction is undefined. A handler receives the
 * offset inside the window and decides the width and offset rules of its
 * device; anything it refuses, and every address outside every window, is an
 * access fault. This mirrors rtl/rv32/rv32_bus.v: one comparator per window,
 * then the device's own decode. */
typedef access (*load_handler)(machine *m, uint32_t offset, int width, uint32_t *value);
typedef access (*store_handler)(machine *m, uint32_t offset, int width, uint32_t value);

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

static access ram_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    *value = bytes_read(m->ram + offset, width);
    return ACC_OK;
}

static access ram_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    bytes_write(m->ram + offset, width, value);
    return ACC_OK;
}

static access console_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    (void)m;
    if (width == 1 && offset == CONSOLE_STATUS) {
        *value = CONSOLE_TX_READY; /* always ready: every byte is accepted at once */
        return ACC_OK;
    }
    return ACC_FAULT; /* TX is write-only; the status is a byte; other offsets do not exist */
}

static access console_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    (void)m;
    if (width == 1 && offset == CONSOLE_TX) {
        fputc((int)(value & 0xff), stdout);
        return ACC_OK;
    }
    return ACC_FAULT; /* the status is read-only; TX takes bytes only */
}

static access done_store(machine *m, uint32_t offset, int width, uint32_t value)
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
static access timer_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    if (width == 4 && offset == TIMER_TICKS) {
        *value = (uint32_t)m->steps + m->timer_offset;
        return ACC_OK;
    }
    return ACC_FAULT;
}

static access timer_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    if (width == 4 && offset == TIMER_TICKS) {
        m->timer_offset = value - (uint32_t)m->steps;
        return ACC_OK;
    }
    return ACC_FAULT;
}

/* Input (docs/rv32.md): the host queues an event when its frame is reached,
 * or drops it and says so when the queue is full; KEYS follows arrivals. */
static void queue_event(machine *m, uint32_t frame, uint32_t event)
{
    if (m->count == INPUT_QUEUE) {
        fprintf(stderr, "rv32emu: input queue full: dropped frame %" PRIu32 " event %08" PRIx32 "\n", frame, event);
        m->dropped++;
        return;
    }
    m->queue[(m->head + m->count) % INPUT_QUEUE] = event;
    m->count++;
    uint32_t bit = 1u << (event & 31u);
    m->keys = (event & EVENT_PRESS) ? (m->keys | bit) : (m->keys & ~bit);
}

static void deliver_events(machine *m)
{
    while (m->next_scripted < m->scripted && m->script[m->next_scripted].frame <= m->frames) {
        queue_event(m, m->script[m->next_scripted].frame, m->script[m->next_scripted].event);
        m->next_scripted++;
    }
}

static access input_load(machine *m, uint32_t offset, int width, uint32_t *value)
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

static access fb_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    *value = bytes_read(m->fb + offset, width);
    return ACC_OK;
}

static access fb_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    bytes_write(m->fb + offset, width, value);
    return ACC_OK;
}

/* The checkpoint hash (docs/rv32.md, "Display"): h = ((h << 5) + h) ^ word
 * from 5381 over the framebuffer's little-endian words in address order;
 * shift, add, xor, no multiply, so the firmware can compute the same value
 * over a frame it reads back. */
static uint32_t frame_hash(const uint8_t *pixels)
{
    uint32_t h = 5381u;
    for (uint32_t i = 0; i < FB_SIZE; i += 4) {
        h = ((h << 5) + h) ^ bytes_read(pixels + i, 4);
    }
    return h;
}

/* Write the frame as a binary PPM with the fixed RGB332 mapping (bits 7:5
 * red, 4:2 green, 1:0 blue, each scaled to 0..255) so a frame can be looked
 * at before M6 shows it in a window. */
static bool write_ppm(const machine *m, const char *path)
{
    FILE *out = fopen(path, "wb");
    if (!out) {
        return false;
    }
    fprintf(out, "P6\n%u %u\n255\n", FB_COLUMNS, FB_ROWS);
    for (uint32_t i = 0; i < FB_SIZE; i++) {
        uint8_t p = m->fb[i];
        uint8_t rgb[3] = {(uint8_t)(((p >> 5) & 7u) * 255u / 7u), (uint8_t)(((p >> 2) & 7u) * 255u / 7u),
                          (uint8_t)((p & 3u) * 255u / 3u)};
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
    deliver_events(m); /* this frame's scripted events arrive with the snapshot */
    if (m->checkpoints) {
        fprintf(m->checkpoints, "frame %" PRIu32 " %08" PRIx32 "\n", m->frames, frame_hash(m->fb));
    }
    if (m->frames_dir) {
        char path[4096];
        int n = snprintf(path, sizeof path, "%s/frame-%04" PRIu32 ".ppm", m->frames_dir, m->frames);
        if (n < 0 || (size_t)n >= sizeof path || !write_ppm(m, path)) {
            fprintf(stderr, "rv32emu: cannot write frame %" PRIu32 " to %s\n", m->frames, m->frames_dir);
            m->output_error = true;
        }
    }
}

static access display_load(machine *m, uint32_t offset, int width, uint32_t *value)
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

static access display_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    (void)value; /* any word presents */
    if (width == 4 && offset == DISPLAY_PRESENT) {
        present(m);
        return ACC_OK;
    }
    return ACC_FAULT; /* FRAMES, WIDTH, and HEIGHT are read-only */
}

/* The memory map (docs/rv32.md). RAM is last only for readability; the
 * windows are disjoint so the order does not matter. */
static const region REGIONS[] = {
    {"done", DONE_ADDR, 4, NULL, done_store},
    {"console", CONSOLE_BASE, 8, console_load, console_store},
    {"timer", TIMER_BASE, 16, timer_load, timer_store},
    {"input", INPUT_BASE, 16, input_load, NULL},
    {"display", DISPLAY_BASE, 16, display_load, display_store},
    {"framebuffer", FB_BASE, FB_SIZE, fb_load, fb_store},
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
static access load(machine *m, uint32_t addr, int width, uint32_t *value)
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

static access store(machine *m, uint32_t addr, int width, uint32_t value)
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
    m->wr_reg = -1;
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
    bool writes_rd = false;
    access status;

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
    case 0x03: { /* loads */
        if (funct3 == 3 || funct3 > 5) {
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
        writes_rd = true;
        break;
    }
    case 0x23: { /* stores */
        if (funct3 > 2) {
            goto illegal;
        }
        int width = width_of(funct3);
        uint32_t addr = a + (uint32_t)imm_s;
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

    if (writes_rd && rd != 0) { /* x0 stays zero: the write is discarded, not stored */
        m->x[rd] = result;
        m->wr_reg = (int)rd;
        m->wr_value = result;
    }
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

static const char *halt_name(enum halt halt)
{
    switch (halt) {
    case HALT_DONE: return "done";
    case HALT_DOUBLE_FAULT: return "double-fault";
    case HALT_LIMIT: return "limit";
    default: return "running";
    }
}

static void dump_state(const machine *m, FILE *out)
{
    fprintf(out, "pc %08" PRIx32 "\n", m->pc);
    for (int i = 0; i < 32; i++) {
        fprintf(out, "x%d %08" PRIx32 "\n", i, m->x[i]);
    }
    fprintf(out, "mtvec %08" PRIx32 "\nmepc %08" PRIx32 "\nmcause %08" PRIx32 "\nmtval %08" PRIx32 "\n",
            m->mtvec, m->mepc, m->mcause, m->mtval);
    fprintf(out, "steps %" PRIu64 "\nretired %" PRIu64 "\ntraps %" PRIu64 "\nframes %" PRIu32 "\nevents %u\nhalt %s\n",
            m->steps, m->retired, m->traps, m->frames, m->count, halt_name(m->halt));
    if (m->halt == HALT_DONE) {
        fprintf(out, "done %08" PRIx32 "\n", m->done_word);
    }
}

/* Strict unsigned parse: must start with a digit (strtoull would skip whitespace and accept a
 * sign), may use 0x/0 prefixes, and must have no trailing text and no overflow. */
static uint64_t parse_u64(const char *text, uint64_t max, const char *what)
{
    char *end;
    errno = 0;
    unsigned long long value = strtoull(text, &end, 0);
    if (!isdigit((unsigned char)*text) || *end != '\0' || errno == ERANGE || value > max) {
        fprintf(stderr, "rv32emu: bad %s: %s\n", what, text);
        exit(EXIT_EMULATOR_ERROR);
    }
    return (uint64_t)value;
}

static uint32_t parse_u32(const char *text, const char *what)
{
    return (uint32_t)parse_u64(text, 0xffffffffull, what);
}

/* True when both names refer to one file: same spelling, or same device and inode when both exist. */
static bool same_file(const char *a, const char *b)
{
    struct stat sa, sb;
    if (!a || !b) {
        return false;
    }
    if (strcmp(a, b) == 0) {
        return true;
    }
    return stat(a, &sa) == 0 && stat(b, &sb) == 0 && sa.st_dev == sb.st_dev && sa.st_ino == sb.st_ino;
}

static void require_distinct(const char *path, const char *what, const char *other_path, const char *other)
{
    if (same_file(path, other_path)) {
        fprintf(stderr, "rv32emu: %s %s would overwrite the %s\n", what, path, other);
        exit(EXIT_EMULATOR_ERROR);
    }
}

/* Close an output stream, reporting any write error that reached it. */
static bool close_output(FILE *stream, const char *path)
{
    bool ok = !ferror(stream);
    if (fclose(stream) != 0) {
        ok = false;
    }
    if (!ok) {
        fprintf(stderr, "rv32emu: error writing %s: %s\n", path, strerror(errno));
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

/* A key name from programs/rv32/board.h, any case, or a number 0..31; -1 otherwise. */
static int key_code(const char *text)
{
    static const struct { const char *name; int code; } names[] = {
        {"LEFT", 1}, {"RIGHT", 2}, {"UP", 3}, {"DOWN", 4}, {"SPACE", 5}, {"ENTER", 6}, {"ESCAPE", 7},
        {"A", 8}, {"D", 9}, {"W", 10}, {"S", 11}, {"P", 12}, {"Q", 13}, {"R", 14},
    };
    char upper[16];
    size_t len = strlen(text);
    if (len == 0 || len >= sizeof upper) {
        return -1;
    }
    for (size_t i = 0; i <= len; i++) {
        upper[i] = (char)toupper((unsigned char)text[i]);
    }
    for (size_t i = 0; i < sizeof names / sizeof names[0]; i++) {
        if (strcmp(upper, names[i].name) == 0) {
            return names[i].code;
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
static void read_input_script(machine *m, const char *path)
{
    FILE *in = fopen(path, "r");
    if (!in) {
        fprintf(stderr, "rv32emu: cannot open input script %s\n", path);
        exit(EXIT_EMULATOR_ERROR);
    }
    char line[256];
    size_t capacity = 0;
    uint32_t last_frame = 0;
    for (unsigned number = 1; fgets(line, sizeof line, in); number++) {
        char keyword[16], frame_text[16], direction[16], key[16], rest[16];
        if (strchr(line, '\n') == NULL && !feof(in)) {
            fprintf(stderr, "rv32emu: input script %s line %u is too long\n", path, number);
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
            code = key_code(key);
        }
        if (fields != 4 || strcmp(keyword, "frame") != 0 || (strcmp(direction, "down") != 0 && strcmp(direction, "up") != 0) ||
            !decimal_ok(frame_text) || code < 0 || frame < last_frame) {
            fprintf(stderr, "rv32emu: input script %s line %u: expected `frame N down|up KEY` with frames in order: %s",
                    path, number, line);
            exit(EXIT_EMULATOR_ERROR);
        }
        if (m->scripted == capacity) {
            capacity = capacity ? 2 * capacity : 64;
            m->script = realloc(m->script, capacity * sizeof *m->script);
            if (!m->script) {
                fputs("rv32emu: cannot allocate the input script\n", stderr);
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
        fprintf(stderr, "rv32emu: cannot read input script %s: %s\n", path, strerror(errno));
        exit(EXIT_EMULATOR_ERROR);
    }
    fclose(in);
}

static void usage(void)
{
    fputs("usage: rv32emu --image FILE [--base ADDR] [--pc ADDR] [--trace FILE] [--dump-state FILE]\n"
          "               [--max-instructions N] [--checkpoints FILE] [--frames DIR] [--input FILE]\n"
          "               [--allow-lost-events]\n"
          "Loads FILE at ADDR (default 0x80000000), starts at --pc (default the base), and runs\n"
          "until the done register is written. Console bytes go to stdout, the trace and state\n"
          "to their files, and a final 'rv32emu: halt=...' line to stderr. Each present appends\n"
          "'frame N <hash>' to the checkpoints file and writes DIR/frame-NNNN.ppm. The input\n"
          "script's `frame N down|up KEY` events arrive when frame N is presented; an event the\n"
          "full queue dropped or the guest never reached turns a pass into an error unless\n"
          "--allow-lost-events says it was expected.\n",
          stderr);
    exit(EXIT_EMULATOR_ERROR);
}

int main(int argc, char **argv)
{
    const char *image_path = NULL, *trace_path = NULL, *state_path = NULL, *checkpoints_path = NULL;
    const char *input_path = NULL;
    uint32_t base = RAM_BASE, start = 0;
    bool start_given = false, allow_lost_events = false;
    machine m;
    memset(&m, 0, sizeof m);
    m.limit = 100000000ull;
    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];
        if (!strcmp(arg, "--allow-lost-events")) { /* the one flag without a value */
            allow_lost_events = true;
            continue;
        }
        const char *value = i + 1 < argc ? argv[++i] : NULL;
        if (!value) {
            usage();
        }
        if (!strcmp(arg, "--image")) {
            image_path = value;
        } else if (!strcmp(arg, "--base")) {
            base = parse_u32(value, "base address");
        } else if (!strcmp(arg, "--pc")) {
            start = parse_u32(value, "start pc");
            start_given = true;
        } else if (!strcmp(arg, "--trace")) {
            trace_path = value;
        } else if (!strcmp(arg, "--dump-state")) {
            state_path = value;
        } else if (!strcmp(arg, "--max-instructions")) {
            m.limit = parse_u64(value, UINT64_MAX, "instruction limit");
        } else if (!strcmp(arg, "--checkpoints")) {
            checkpoints_path = value;
        } else if (!strcmp(arg, "--frames")) {
            m.frames_dir = value;
        } else if (!strcmp(arg, "--input")) {
            input_path = value;
        } else {
            usage();
        }
    }
    if (!image_path) {
        usage();
    }
    m.ram = calloc(RAM_SIZE, 1);
    m.fb = calloc(FB_SIZE, 1); /* unspecified by the contract; zero like the RTL testbench */
    if (!m.ram || !m.fb) {
        fputs("rv32emu: cannot allocate memory\n", stderr);
        return EXIT_EMULATOR_ERROR;
    }
    FILE *image = fopen(image_path, "rb");
    if (!image) {
        fprintf(stderr, "rv32emu: cannot open %s\n", image_path);
        return EXIT_EMULATOR_ERROR;
    }
    if (base < RAM_BASE || base - RAM_BASE >= RAM_SIZE) {
        fprintf(stderr, "rv32emu: base %08" PRIx32 " is outside RAM\n", base);
        return EXIT_EMULATOR_ERROR;
    }
    size_t loaded = fread(m.ram + (base - RAM_BASE), 1, RAM_SIZE - (base - RAM_BASE), image);
    if (ferror(image)) { /* a directory opens but does not read; without this it would be an empty image */
        fprintf(stderr, "rv32emu: cannot read %s: %s\n", image_path, strerror(errno));
        return EXIT_EMULATOR_ERROR;
    }
    if (loaded == 0) { /* an empty image would run as an illegal instruction at the reset PC */
        fprintf(stderr, "rv32emu: %s is empty\n", image_path);
        return EXIT_EMULATOR_ERROR;
    }
    if (fgetc(image) != EOF) {
        fprintf(stderr, "rv32emu: %s does not fit in RAM at %08" PRIx32 "\n", image_path, base);
        return EXIT_EMULATOR_ERROR;
    }
    fclose(image);
    m.pc = start_given ? start : RAM_BASE; /* reset PC from the contract */
    if (input_path) {
        read_input_script(&m, input_path);
        deliver_events(&m); /* frame 0's events are queued before the first instruction */
    }
    if (trace_path) {
        require_distinct(trace_path, "trace file", image_path, "image");
        require_distinct(trace_path, "trace file", input_path, "input script");
        m.trace = fopen(trace_path, "w");
        if (!m.trace) {
            fprintf(stderr, "rv32emu: cannot write %s\n", trace_path);
            return EXIT_EMULATOR_ERROR;
        }
    }
    if (checkpoints_path) {
        require_distinct(checkpoints_path, "checkpoints file", image_path, "image");
        require_distinct(checkpoints_path, "checkpoints file", trace_path, "trace file");
        require_distinct(checkpoints_path, "checkpoints file", input_path, "input script");
        m.checkpoints = fopen(checkpoints_path, "w");
        if (!m.checkpoints) {
            fprintf(stderr, "rv32emu: cannot write %s\n", checkpoints_path);
            return EXIT_EMULATOR_ERROR;
        }
    }

    while (m.halt == RUNNING) {
        if (m.steps >= m.limit) {
            m.halt = HALT_LIMIT;
            break;
        }
        step(&m);
    }
    bool outputs_ok = true;
    if (fflush(stdout) != 0 || ferror(stdout)) {
        fprintf(stderr, "rv32emu: error writing console output: %s\n", strerror(errno));
        outputs_ok = false;
    }
    if (m.trace && !close_output(m.trace, trace_path)) {
        outputs_ok = false;
    }
    if (m.checkpoints && !close_output(m.checkpoints, checkpoints_path)) {
        outputs_ok = false;
    }
    if (m.output_error) {
        outputs_ok = false;
    }
    if (state_path) {
        require_distinct(state_path, "state file", image_path, "image");
        require_distinct(state_path, "state file", trace_path, "trace file");
        require_distinct(state_path, "state file", checkpoints_path, "checkpoints file");
        require_distinct(state_path, "state file", input_path, "input script");
        FILE *out = fopen(state_path, "w");
        if (!out) {
            fprintf(stderr, "rv32emu: cannot write %s\n", state_path);
            return EXIT_EMULATOR_ERROR;
        }
        dump_state(&m, out);
        if (!close_output(out, state_path)) {
            outputs_ok = false;
        }
    }

    if (m.next_scripted < m.scripted) {
        fprintf(stderr, "rv32emu: %zu scripted event(s) never delivered (first: frame %" PRIu32 ")\n",
                m.scripted - m.next_scripted, m.script[m.next_scripted].frame);
    }
    size_t lost = m.dropped + (m.scripted - m.next_scripted);
    int status = EXIT_EMULATOR_ERROR;
    fprintf(stderr, "rv32emu: halt=%s steps=%" PRIu64 " retired=%" PRIu64 " traps=%" PRIu64 " loaded=%zu",
            halt_name(m.halt), m.steps, m.retired, m.traps, loaded);
    switch (m.halt) {
    case HALT_DONE:
        fprintf(stderr, " done=%08" PRIx32, m.done_word);
        if (m.done_word == DONE_PASS) {
            fputs(" pass\n", stderr);
            status = 0;
        } else if ((m.done_word & 0xffffu) == DONE_FAIL && (m.done_word >> 16) >= 1 && (m.done_word >> 16) <= 255) {
            status = (int)(m.done_word >> 16);
            fprintf(stderr, " fail=%d\n", status);
        } else if (m.done_word == DONE_RESET) {
            fputs(" error=reserved-reset-word\n", stderr);
        } else {
            fputs(" error=undefined-done-word\n", stderr);
        }
        break;
    case HALT_DOUBLE_FAULT:
        fprintf(stderr, " error=unhandled-trap mcause=%" PRIu32 " mepc=%08" PRIx32 " mtval=%08" PRIx32
                " then mcause=%" PRIu32 " mtval=%08" PRIx32 " at pc=%08" PRIx32 "\n",
                m.mcause, m.mepc, m.mtval, m.second_cause, m.second_tval, m.pc);
        break;
    case HALT_LIMIT:
        fprintf(stderr, " error=instruction-limit pc=%08" PRIx32 "\n", m.pc);
        break;
    default:
        fputc('\n', stderr);
    }
    free(m.ram);
    free(m.fb);
    free(m.script);
    if (!outputs_ok) {
        fputs("rv32emu: outputs incomplete, run rejected\n", stderr);
        return EXIT_EMULATOR_ERROR;
    }
    /* The script and the program disagreed: the guest's pass says nothing about the events it
     * never saw, so a passing run is rejected unless the caller meant it (a drop test). A guest
     * that failed or was halted keeps its own status; the events it missed are reported above. */
    if (lost > 0 && !allow_lost_events && status == 0) {
        fprintf(stderr, "rv32emu: %zu scripted event(s) lost, run rejected (--allow-lost-events accepts this)\n", lost);
        return EXIT_EMULATOR_ERROR;
    }
    return status;
}
