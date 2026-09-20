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
    /* Effects of the current step, for the trace line. */
    int wr_reg;
    uint32_t wr_value;
    bool mem_read, mem_write;
    uint32_t mem_addr, mem_value;
    int mem_width;
} machine;

typedef enum { ACC_OK, ACC_FAULT, ACC_MISALIGNED } access;

static bool in_ram(uint32_t addr, int width)
{
    return addr >= RAM_BASE && addr - RAM_BASE + (uint32_t)width <= RAM_SIZE;
}

static uint32_t ram_read(const machine *m, uint32_t addr, int width)
{
    const uint8_t *p = m->ram + (addr - RAM_BASE);
    uint32_t value = 0;
    for (int i = width - 1; i >= 0; i--) {
        value = (value << 8) | p[i];
    }
    return value;
}

static void ram_write(machine *m, uint32_t addr, int width, uint32_t value)
{
    uint8_t *p = m->ram + (addr - RAM_BASE);
    for (int i = 0; i < width; i++) {
        p[i] = (uint8_t)(value >> (8 * i));
    }
}

/* Data load. Misalignment is checked before the address is decoded. */
static access load(machine *m, uint32_t addr, int width, uint32_t *value)
{
    if (addr % (uint32_t)width) {
        return ACC_MISALIGNED;
    }
    if (in_ram(addr, width)) {
        *value = ram_read(m, addr, width);
        return ACC_OK;
    }
    if (width == 1 && addr == CONSOLE_BASE + CONSOLE_STATUS) {
        *value = CONSOLE_TX_READY; /* always ready: every byte is accepted at once */
        return ACC_OK;
    }
    /* Console TX and other console offsets, the done register, and every
     * unmapped address are not readable. */
    return ACC_FAULT;
}

static access store(machine *m, uint32_t addr, int width, uint32_t value)
{
    if (addr % (uint32_t)width) {
        return ACC_MISALIGNED;
    }
    if (in_ram(addr, width)) {
        ram_write(m, addr, width, value);
        return ACC_OK;
    }
    if (width == 1 && addr == CONSOLE_BASE + CONSOLE_TX) {
        fputc((int)(value & 0xff), stdout);
        return ACC_OK;
    }
    if (width == 4 && addr == DONE_ADDR) {
        m->halt = HALT_DONE; /* the store still retires; the loop stops afterwards */
        m->done_word = value;
        return ACC_OK;
    }
    /* Byte or halfword writes to the done register, the console status
     * register, other console offsets, and unmapped addresses fault. */
    return ACC_FAULT;
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
    fprintf(out, "steps %" PRIu64 "\nretired %" PRIu64 "\ntraps %" PRIu64 "\nhalt %s\n",
            m->steps, m->retired, m->traps, halt_name(m->halt));
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

static void usage(void)
{
    fputs("usage: rv32emu --image FILE [--base ADDR] [--pc ADDR] [--trace FILE] [--dump-state FILE]\n"
          "               [--max-instructions N]\n"
          "Loads FILE at ADDR (default 0x80000000), starts at --pc (default the base), and runs\n"
          "until the done register is written. Console bytes go to stdout, the trace and state\n"
          "to their files, and a final 'rv32emu: halt=...' line to stderr.\n",
          stderr);
    exit(EXIT_EMULATOR_ERROR);
}

int main(int argc, char **argv)
{
    const char *image_path = NULL, *trace_path = NULL, *state_path = NULL;
    uint32_t base = RAM_BASE, start = 0;
    bool start_given = false;
    machine m;
    memset(&m, 0, sizeof m);
    m.limit = 100000000ull;
    for (int i = 1; i < argc; i += 2) {
        const char *arg = argv[i], *value = i + 1 < argc ? argv[i + 1] : NULL;
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
        } else {
            usage();
        }
    }
    if (!image_path) {
        usage();
    }
    m.ram = calloc(RAM_SIZE, 1);
    if (!m.ram) {
        fputs("rv32emu: cannot allocate RAM\n", stderr);
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
    if (fgetc(image) != EOF) {
        fprintf(stderr, "rv32emu: %s does not fit in RAM at %08" PRIx32 "\n", image_path, base);
        return EXIT_EMULATOR_ERROR;
    }
    fclose(image);
    m.pc = start_given ? start : RAM_BASE; /* reset PC from the contract */
    if (trace_path) {
        require_distinct(trace_path, "trace file", image_path, "image");
        m.trace = fopen(trace_path, "w");
        if (!m.trace) {
            fprintf(stderr, "rv32emu: cannot write %s\n", trace_path);
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
    if (state_path) {
        require_distinct(state_path, "state file", image_path, "image");
        require_distinct(state_path, "state file", trace_path, "trace file");
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
    if (!outputs_ok) {
        fputs("rv32emu: outputs incomplete, run rejected\n", stderr);
        return EXIT_EMULATOR_ERROR;
    }
    return status;
}
