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
#include "rv32_dtb.h"

#include <ctype.h>
#include <errno.h>
#include <inttypes.h>
#include <limits.h>
#include <stdlib.h>
#include <string.h>
#include <fcntl.h>
#include <poll.h>
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
    CAUSE_ECALL_U = 8,
    CAUSE_ECALL_M = 11,
};

/* The only CSRs that exist; every other number is an illegal instruction. The six Zicntr
 * counters (0xc00-0xc02 and their high halves at 0xc80-0xc82) are read-only: numbers with
 * bits [11:10] set are, by the CSR address convention, and a write to one is illegal. */
enum csr { CSR_FFLAGS = 0x001, CSR_FRM = 0x002, CSR_FCSR = 0x003, CSR_MSTATUS = 0x300, CSR_MIE = 0x304, CSR_MTVEC = 0x305,
           CSR_MCOUNTEREN = 0x306, CSR_PMPCFG0 = 0x3a0, CSR_PMPCFG1 = 0x3a1, CSR_PMPADDR0 = 0x3b0, CSR_PMPADDR7 = 0x3b7,
           CSR_MSCRATCH = 0x340, CSR_MEPC = 0x341, CSR_MCAUSE = 0x342, CSR_MTVAL = 0x343, CSR_MIP = 0x344,
           CSR_CYCLE = 0xc00, CSR_TIME = 0xc01, CSR_INSTRET = 0xc02, CSR_CYCLEH = 0xc80, CSR_TIMEH = 0xc81, CSR_INSTRETH = 0xc82 };
/* mstatus and the interrupt bits (O1, docs/rv32.md "Behavior fixed in Track 2"). MPP is 3 (machine) or
 * 0 (user) since O5, kept in machine.mpp; FS reads 3 and SD 1 because floating state is always on. */
#define MSTATUS_MIE 0x8u
#define MSTATUS_MPIE 0x80u
#define MSTATUS_CONSTANT 0x80006000u /* SD, FS = 3 */
#define MSTATUS_MPP 0x1800u
#define PRIV_U 0u
#define PRIV_M 3u
/* PMP (O5): a configuration byte per entry. */
#define PMP_R 0x01u
#define PMP_W 0x02u
#define PMP_X 0x04u
#define PMP_A 0x18u
#define PMP_TOR 0x08u
#define PMP_NA4 0x10u
#define PMP_NAPOT 0x18u
#define PMP_L 0x80u
#define PMP_ENTRIES 8u
#define IRQ_MSI 3u
#define IRQ_MTI 7u
#define IRQ_MEI 11u
#define MIE_MASK ((1u << IRQ_MSI) | (1u << IRQ_MTI) | (1u << IRQ_MEI))
#define INTERRUPT 0x80000000u
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
    if(gpu_source_locked(&m->gpu,RAM_BASE+offset,width)) return ACC_FAULT;
    if(g3d_z_locked(&m->g3d,RAM_BASE+offset,width)) return ACC_FAULT;
    bytes_write(m->ram + offset, width, value);
    return ACC_OK;
}

/* Interactive console input: take whatever stdin has now, without waiting. */
static void console_poll_stdin(machine *m)
{
    if (!m->console_stdin || m->console_in_next < m->console_in_len) {
        return;
    }
    struct pollfd p = {STDIN_FILENO, POLLIN, 0};
    if (poll(&p, 1, 0) <= 0 || !(p.revents & (POLLIN | POLLHUP))) {
        return;
    }
    uint8_t buffer[256];
    ssize_t n = read(STDIN_FILENO, buffer, sizeof buffer);
    if (n <= 0) {
        m->console_stdin = false; /* end of input: nothing more will arrive */
        return;
    }
    free(m->console_in);
    m->console_in = malloc((size_t)n);
    if (!m->console_in) {
        fprintf(stderr, "%s: cannot allocate console input\n", emu_prog);
        exit(EXIT_EMULATOR_ERROR);
    }
    memcpy(m->console_in, buffer, (size_t)n);
    m->console_in_len = (size_t)n;
    m->console_in_next = 0;
}

/* The console (docs/rv32.md, "Console"): a 16550's transmit and line-status registers, and since O2
 * its receive buffer: a byte read of +0 takes the next received byte (0 when there is none) and
 * LSR bit 0 says one is waiting. */
static mem_access console_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    if (width == 1 && offset == CONSOLE_STATUS) {
        console_poll_stdin(m);
        /* always ready to transmit: every byte is accepted at once */
        *value = CONSOLE_TX_READY | (m->console_in_next < m->console_in_len ? CONSOLE_RX_READY : 0u);
        return ACC_OK;
    }
    if (width == 1 && offset == CONSOLE_TX) {
        console_poll_stdin(m);
        *value = m->console_in_next < m->console_in_len ? m->console_in[m->console_in_next++] : 0u;
        return ACC_OK;
    }
    return ACC_FAULT; /* the status and RBR are bytes; other offsets do not exist */
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

/* CLINT (docs/rv32.md, "CLINT"). Device time: a tick is one executed instruction, and a load
 * runs before its instruction is counted, so instruction N reads mtime = N - 1 plus whatever a
 * write added. A write to one half replaces that half of the current count. msip and mtimecmp
 * are plain registers until the core takes interrupts (O1). */
static uint64_t mtime_now(const machine *m)
{
    return m->steps + m->mtime_offset;
}

static mem_access clint_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    if (width != 4) {
        return ACC_FAULT;
    }
    switch (offset) {
    case CLINT_MSIP: *value = m->msip; return ACC_OK;
    case CLINT_MTIMECMP: *value = (uint32_t)m->mtimecmp; return ACC_OK;
    case CLINT_MTIMECMP + 4: *value = (uint32_t)(m->mtimecmp >> 32); return ACC_OK;
    case CLINT_MTIME: *value = (uint32_t)mtime_now(m); return ACC_OK;
    case CLINT_MTIME + 4: *value = (uint32_t)(mtime_now(m) >> 32); return ACC_OK;
    default: return ACC_FAULT;
    }
}

static mem_access clint_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    if (width != 4) {
        return ACC_FAULT;
    }
    uint64_t now = mtime_now(m);
    switch (offset) {
    case CLINT_MSIP: m->msip = value & 1u; return ACC_OK;
    case CLINT_MTIMECMP: m->mtimecmp = (m->mtimecmp & ~0xffffffffull) | value; return ACC_OK;
    case CLINT_MTIMECMP + 4: m->mtimecmp = (m->mtimecmp & 0xffffffffull) | (uint64_t)value << 32; return ACC_OK;
    case CLINT_MTIME: m->mtime_offset = ((now & ~0xffffffffull) | value) - m->steps; return ACC_OK;
    case CLINT_MTIME + 4: m->mtime_offset = ((now & 0xffffffffull) | (uint64_t)value << 32) - m->steps; return ACC_OK;
    default: return ACC_FAULT;
    }
}

/* Boot ROM: the device tree blob (tools/rv32_dtb.h), readable at every width; bytes past the
 * blob read 0. It has no store handler, so every write faults. */
static mem_access rom_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    (void)m;
    uint32_t v = 0;
    for (int i = 0; i < width; i++) {
        uint32_t at = offset + (uint32_t)i;
        v |= (uint32_t)(at < RV32_DTB_SIZE ? rv32_dtb[at] : 0) << (8 * i);
    }
    *value = v;
    return ACC_OK;
}

/* PLIC (O1; docs/rv32.md, "PLIC"): one context, hart 0 in machine mode. A wired source is pending
 * while its line is high and it is not claimed; a claim returns the pending, enabled source with the
 * highest priority above the threshold (ties to the lowest number) and marks it claimed until its
 * number is written back. Unwired sources hold no priority or enable. */
static uint32_t plic_lines(const machine *m)
{
    return (m->count ? 1u << PLIC_SOURCE_INPUT : 0u) | (m->virtio->interrupt ? 1u << PLIC_SOURCE_VIRTIO : 0u);
}

static uint32_t plic_pending(const machine *m)
{
    return plic_lines(m) & PLIC_WIRED & ~m->plic_claimed;
}

/* The source a claim would return now, or 0. */
static uint32_t plic_best(const machine *m)
{
    uint32_t candidates = m->plic_enable ? plic_pending(m) & m->plic_enable : 0u, best = 0, best_priority = m->plic_threshold;
    for (uint32_t id = 1; id < PLIC_SOURCES; id++) {
        if ((candidates >> id) & 1u && m->plic_priority[id] > best_priority) {
            best = id;
            best_priority = m->plic_priority[id];
        }
    }
    return best;
}

static mem_access plic_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    if (width != 4) {
        return ACC_FAULT;
    }
    if (offset < 4 * PLIC_SOURCES) {
        *value = m->plic_priority[offset / 4];
    } else if (offset == PLIC_PENDING) {
        *value = plic_pending(m);
    } else if (offset == PLIC_ENABLE) {
        *value = m->plic_enable;
    } else if (offset == PLIC_THRESHOLD) {
        *value = m->plic_threshold;
    } else if (offset == PLIC_CLAIM) {
        *value = plic_best(m);
        m->plic_claimed |= (1u << *value) & ~1u;
    } else {
        return ACC_FAULT;
    }
    return ACC_OK;
}

static mem_access plic_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    if (width != 4) {
        return ACC_FAULT;
    }
    if (offset < 4 * PLIC_SOURCES) {
        if ((PLIC_WIRED >> (offset / 4)) & 1u) {
            m->plic_priority[offset / 4] = (uint8_t)(value & 7u);
        }
    } else if (offset == PLIC_ENABLE) {
        m->plic_enable = value & PLIC_WIRED;
    } else if (offset == PLIC_THRESHOLD) {
        m->plic_threshold = (uint8_t)(value & 7u);
    } else if (offset == PLIC_CLAIM) {
        if (value < PLIC_SOURCES && (m->plic_enable >> value) & 1u) {
            m->plic_claimed &= ~(1u << value); /* a completion for a disabled source is ignored */
        }
    } else {
        return ACC_FAULT; /* the pending word is read-only */
    }
    return ACC_OK;
}

/* virtio-blk (O3; rtl/rv32/rv32_virtio_blk.v runs the same steps). A notify of queue 0 with the
 * queue ready and DRIVER_OK set serves every available request before the store completes; its
 * DMA reads and writes RAM directly, like the RTL's, which goes around the engines' write locks. */
#define VIRTIO_MAGIC 0x74726976u
#define VIRTIO_VENDOR 0x594e4954u
#define VIRTIO_QUEUE_MAX 8u
#define VIRTIO_NEEDS_RESET 0x40u

static bool dma_word(uint32_t addr)
{
    return addr >= RAM_BASE && addr - RAM_BASE < RAM_SIZE;
}

/* A DMA read of the word holding `addr` (the RAM ignores bits 1:0, as the RTL's does). */
static bool dma_read(machine *m, uint32_t addr, uint32_t *value)
{
    if (!dma_word(addr)) {
        return false;
    }
    *value = bytes_read(m->ram + ((addr & ~3u) - RAM_BASE), 4);
    return true;
}

static bool dma_read16(machine *m, uint32_t addr, uint16_t *value)
{
    uint32_t word;
    if (!dma_read(m, addr, &word)) {
        return false;
    }
    *value = (uint16_t)((addr & 2u) ? word >> 16 : word);
    return true;
}

/* A DMA write of the strobed bytes of the word holding `addr`. */
static bool dma_write(machine *m, uint32_t addr, uint32_t value, uint32_t strobe)
{
    if (!dma_word(addr)) {
        return false;
    }
    uint8_t *p = m->ram + ((addr & ~3u) - RAM_BASE);
    for (int i = 0; i < 4; i++) {
        if (strobe & (1u << i)) {
            p[i] = (uint8_t)(value >> (8 * i));
        }
    }
    return true;
}

static void virtio_disk_written(machine *m, uint32_t offset, uint32_t bytes)
{
    virtio_blk *v = m->virtio;
    if (v->file && (fseek(v->file, (long)offset, SEEK_SET) != 0 || fwrite(v->disk + offset, 1, bytes, v->file) != bytes ||
                    fflush(v->file) != 0)) {
        v->write_error = true;
    }
}

/* One request: false when the chain cannot be followed or an address is outside RAM. */
static bool virtio_request(machine *m)
{
    virtio_blk *v = m->virtio;
    uint32_t mask = v->queue_num - 1u;
    uint16_t head, next = 0;
    if (!dma_read16(m, v->driver_lo + 4u + 2u * (v->last_avail & mask), &head)) {
        return false;
    }
    uint32_t address[3], length = 0, words[4];
    bool data_write = false;
    for (int which = 0; which < 3; which++) {
        uint32_t base = v->desc_lo + 16u * (which == 0 ? head : next);
        for (int w = 0; w < 4; w++) { /* the RTL stops at a nonzero high word before reading on */
            if (!dma_read(m, base + 4u * (uint32_t)w, &words[w]) || (w == 1 && words[1] != 0)) {
                return false;
            }
        }
        bool has_next = words[3] & 1u, write = words[3] & 2u;
        if (has_next != (which != 2) || (which == 0 && write) || (which == 2 && !write)) {
            return false;
        }
        address[which] = words[0];
        if (which == 1) {
            length = words[2];
            data_write = write;
        }
        next = (uint16_t)(words[3] >> 16);
    }
    uint32_t type, sector, sector_hi;
    if (!dma_read(m, address[0], &type) || !dma_read(m, address[0] + 8u, &sector) || !dma_read(m, address[0] + 12u, &sector_hi)) {
        return false;
    }
    const uint32_t sectors = VIRTIO_DISK_SIZE / 512u, words_total = VIRTIO_DISK_SIZE / 4u;
    uint8_t result;
    if (type > 1) {
        result = 2; /* UNSUPP */
    } else if (sector_hi != 0 || sector >= sectors || (length & 3u) || (address[1] & 3u) ||
               (uint64_t)sector * 128u + (length >> 2) > words_total || data_write != (type == 0)) {
        result = 1; /* IOERR */
    } else {
        for (uint32_t i = 0; i < length / 4u; i++) {
            uint32_t at = sector * 512u + 4u * i, word;
            if (type == 0) {
                if (!dma_write(m, address[1] + 4u * i, bytes_read(v->disk + at, 4), 0xfu)) {
                    return false;
                }
            } else {
                if (!dma_read(m, address[1] + 4u * i, &word)) {
                    return false;
                }
                bytes_write(v->disk + at, 4, word);
            }
        }
        if (type == 1) {
            virtio_disk_written(m, sector * 512u, length);
        }
        result = 0;
    }
    uint32_t used = v->device_lo + 4u + 8u * (v->used_idx & mask);
    uint32_t written = (result == 0 && type == 0) ? length + 1u : 1u;
    if (!dma_write(m, address[2], (uint32_t)result * 0x01010101u, 1u << (address[2] & 3u)) ||
        !dma_write(m, used, head, 0xfu) || !dma_write(m, used + 4u, written, 0xfu) ||
        !dma_write(m, v->device_lo + 2u, (uint32_t)(uint16_t)(v->used_idx + 1u) * 0x10001u, (v->device_lo & 2u) ? 0x3u : 0xcu)) {
        return false;
    }
    v->used_idx++;
    v->last_avail++;
    v->interrupt = true;
    return true;
}

static void virtio_serve(machine *m)
{
    virtio_blk *v = m->virtio;
    if (v->desc_hi || v->driver_hi || v->device_hi || v->queue_num == 0) {
        v->status |= VIRTIO_NEEDS_RESET;
        return;
    }
    for (;;) {
        uint16_t available;
        if (!dma_read16(m, v->driver_lo + 2u, &available)) {
            v->status |= VIRTIO_NEEDS_RESET;
            return;
        }
        if (available == v->last_avail) {
            return;
        }
        if (!virtio_request(m)) {
            v->status |= VIRTIO_NEEDS_RESET;
            return;
        }
    }
}

static mem_access virtio_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    virtio_blk *v = m->virtio;
    if (width != 4) {
        return ACC_FAULT;
    }
    switch (offset) {
    case 0x000: *value = VIRTIO_MAGIC; break;
    case 0x004: *value = 2; break;
    case 0x008: *value = 2; break;
    case 0x00c: *value = VIRTIO_VENDOR; break;
    case 0x010: *value = v->features_sel ? 1u : 0u; break; /* feature 32: VIRTIO_F_VERSION_1 */
    case 0x034: *value = v->queue_sel_zero ? VIRTIO_QUEUE_MAX : 0u; break;
    case 0x044: *value = v->queue_ready; break;
    case 0x060: *value = v->interrupt; break;
    case 0x070: *value = v->status; break;
    case 0x080: *value = v->desc_lo; break;
    case 0x084: *value = v->desc_hi; break;
    case 0x090: *value = v->driver_lo; break;
    case 0x094: *value = v->driver_hi; break;
    case 0x0a0: *value = v->device_lo; break;
    case 0x0a4: *value = v->device_hi; break;
    case 0x0fc: *value = 0; break;
    case 0x100: *value = VIRTIO_DISK_SIZE / 512u; break;
    case 0x104: *value = 0; break;
    default: return ACC_FAULT; /* write-only registers and unused offsets */
    }
    return ACC_OK;
}

static mem_access virtio_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    virtio_blk *v = m->virtio;
    if (width != 4) {
        return ACC_FAULT;
    }
    switch (offset) {
    case 0x014: v->features_sel = value == 1; break;
    case 0x020: break; /* the driver's features are accepted and not used */
    case 0x024: v->driver_features_sel = value == 1; break;
    case 0x030: v->queue_sel_zero = value == 0; break;
    case 0x038:
        if (v->queue_sel_zero) {
            v->queue_num = (value == 1 || value == 2 || value == 4 || value == 8) ? value : 0u;
        }
        break;
    case 0x044: if (v->queue_sel_zero) { v->queue_ready = value & 1u; } break;
    case 0x050:
        if (value == 0 && v->queue_ready && (v->status & 4u) && !(v->status & VIRTIO_NEEDS_RESET)) {
            virtio_serve(m);
        }
        break;
    case 0x064: if (value & 1u) { v->interrupt = false; } break;
    case 0x070:
        v->status = (uint8_t)value;
        if ((value & 0xffu) == 0) { /* a device reset: the queue starts again */
            v->queue_ready = false;
            v->queue_num = 0;
            v->last_avail = v->used_idx = 0;
            v->interrupt = false;
            v->features_sel = false;
        }
        break;
    case 0x080: if (v->queue_sel_zero) { v->desc_lo = value; } break;
    case 0x084: if (v->queue_sel_zero) { v->desc_hi = value; } break;
    case 0x090: if (v->queue_sel_zero) { v->driver_lo = value; } break;
    case 0x094: if (v->queue_sel_zero) { v->driver_hi = value; } break;
    case 0x0a0: if (v->queue_sel_zero) { v->device_lo = value; } break;
    case 0x0a4: if (v->queue_sel_zero) { v->device_hi = value; } break;
    default: return ACC_FAULT; /* read-only registers and unused offsets */
    }
    return ACC_OK;
}

/* mip: the live interrupt levels from the CLINT and the PLIC. */
static uint32_t mip_now(const machine *m)
{
    return (m->msip ? 1u << IRQ_MSI : 0u) | (mtime_now(m) >= m->mtimecmp ? 1u << IRQ_MTI : 0u) |
           (plic_best(m) ? 1u << IRQ_MEI : 0u);
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

/* Either graphics engine owns the framebuffer while it runs. */
static bool engine_busy(const machine *m) { return gpu_busy(&m->gpu) || g3d_busy(&m->g3d); }

static mem_access fb_load(machine *m, uint32_t offset, int width, uint32_t *value)
{
    if(engine_busy(m)) return ACC_FAULT;
    *value = bytes_read(m->fb + offset, width);
    return ACC_OK;
}

static mem_access fb_store(machine *m, uint32_t offset, int width, uint32_t value)
{
    if(engine_busy(m)) return ACC_FAULT;
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
        if(engine_busy(m)) return ACC_FAULT;
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
static mem_access gpu_load(machine *m,uint32_t off,int width,uint32_t *v)
{return gpu_access(&m->gpu,off,width,false,v)?ACC_OK:ACC_FAULT;}
static mem_access gpu_store(machine *m,uint32_t off,int width,uint32_t v)
{
    /* G1 and G2 share the engine memory port: a START while G2 runs faults. */
    if(off==GPU_COMMAND && v==GPU_START && g3d_busy(&m->g3d)) return ACC_FAULT;
    return gpu_access(&m->gpu,off,width,true,&v)?ACC_OK:ACC_FAULT;
}
static mem_access g3d_mmio_load(machine *m,uint32_t off,int width,uint32_t *v)
{return g3d_access(&m->g3d,off,width,false,v,gpu_busy(&m->gpu))?ACC_OK:ACC_FAULT;}
static mem_access g3d_mmio_store(machine *m,uint32_t off,int width,uint32_t v)
{return g3d_access(&m->g3d,off,width,true,&v,gpu_busy(&m->gpu))?ACC_OK:ACC_FAULT;}
static const region REGIONS[] = {
    {"gpu", GPU_BASE, 128, gpu_load, gpu_store},
    {"g3d", G3D_BASE, G3D_SIZE, g3d_mmio_load, g3d_mmio_store},
    {"done", DONE_ADDR, 4, NULL, done_store},
    {"console", CONSOLE_BASE, 8, console_load, console_store},
    {"clint", CLINT_BASE, CLINT_SIZE, clint_load, clint_store},
    {"plic", PLIC_BASE, PLIC_SIZE, plic_load, plic_store},
    {"virtio", VIRTIO_BASE, VIRTIO_SIZE, virtio_load, virtio_store},
    {"bootrom", RV32_DTB_ROM_BASE, RV32_DTB_ROM_SIZE, rom_load, NULL},
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

/* PMP (O5; docs/rv32.md "Behavior fixed in Track 2"): the lowest-numbered entry that matches the
 * address decides. Regions are whole words (granularity 4), so an aligned access matches whole or not
 * at all. User mode needs a match with the permission; machine mode is held only to locked
 * entries and may go where nothing matches. */
static bool pmp_allows(const machine *m, uint32_t addr, uint32_t need)
{
    uint32_t word = addr >> 2;
    for (uint32_t i = 0; i < PMP_ENTRIES; i++) {
        uint32_t cfg = m->pmpcfg[i], at = m->pmpaddr[i];
        bool match;
        switch (cfg & PMP_A) {
        case PMP_TOR: match = word >= (i ? m->pmpaddr[i - 1] : 0u) && word < at; break;
        case PMP_NA4: match = word == at; break;
        case PMP_NAPOT: match = ((word ^ at) & ~(at ^ (at + 1u))) == 0; break;
        default: continue; /* OFF */
        }
        if (match) {
            return (m->priv == PRIV_M && !(cfg & PMP_L)) || (cfg & need) == need;
        }
    }
    return m->priv == PRIV_M;
}

/* Data load. Misalignment is checked before the address is decoded, and PMP before the bus. */
static mem_access load(machine *m, uint32_t addr, int width, uint32_t *value)
{
    if (addr % (uint32_t)width) {
        return ACC_MISALIGNED;
    }
    if (!pmp_allows(m, addr, PMP_R)) {
        return ACC_FAULT;
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
    if (!pmp_allows(m, addr, PMP_W)) {
        return ACC_FAULT;
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

/* Trap entry, shared by exceptions and interrupts: mepc is the instruction not executed, MPIE takes
 * MIE and MIE clears, MPP takes the privilege mode and the machine enters machine mode (O5). */
static void enter_handler(machine *m, uint32_t cause, uint32_t tval)
{
    m->mepc = m->pc;
    m->mcause = cause;
    m->mtval = tval;
    m->mstatus = (m->mstatus & MSTATUS_MIE) ? MSTATUS_MPIE : 0u;
    m->mpp = m->priv;
    m->priv = PRIV_M;
    m->pc = m->mtvec;
}

/* Deliver a trap for the instruction at m->pc. The instruction does not
 * retire. If the previous trap's handler has not yet retired an instruction,
 * the machine cannot make progress (the M1 firmware leaves mtvec at 0, so its
 * handler would be fetched from unmapped memory): halt and report both traps. */
static void trap(machine *m, uint32_t word, uint32_t cause, uint32_t tval)
{
    simd_tick(&m->simd, false);
    gpu_tick(&m->gpu,m->ram,RAM_SIZE,m->fb,false);
    g3d_tick(&m->g3d,m->ram,RAM_SIZE,m->fb,false);
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
    enter_handler(m, cause, tval);
}

/* Take an interrupt instead of executing the instruction at m->pc (O1): a step and a device tick
 * like a trap, with its own trace line and no instruction word. */
static void take_interrupt(machine *m, uint32_t code)
{
    simd_tick(&m->simd, false);
    gpu_tick(&m->gpu,m->ram,RAM_SIZE,m->fb,false);
    g3d_tick(&m->g3d,m->ram,RAM_SIZE,m->fb,false);
    m->steps++;
    if (m->trace) {
        fprintf(m->trace, "%" PRIu64 " %08" PRIx32 " 00000000 interrupt %" PRIu32 "\n", m->steps, m->pc, code);
    }
    if (m->in_trap) { /* unreachable while trap entry clears MIE; kept as the double-fault rule */
        m->halt = HALT_DOUBLE_FAULT;
        m->second_cause = INTERRUPT | code;
        m->second_tval = 0;
        return;
    }
    m->in_trap = true;
    m->interrupts++;
    enter_handler(m, INTERRUPT | code, 0);
}

static bool csr_read(const machine *m, uint32_t number, uint32_t *value)
{
    switch (number) {
    case CSR_FFLAGS: *value = m->fcsr & 31u; return true;
    case CSR_FRM: *value = m->fcsr >> 5; return true;
    case CSR_FCSR: *value = m->fcsr; return true;
    case CSR_MSTATUS: *value = m->mstatus | MSTATUS_CONSTANT | (m->mpp == PRIV_M ? MSTATUS_MPP : 0u); return true;
    case CSR_MCOUNTEREN: *value = m->mcounteren; return true;
    case CSR_PMPCFG0: case CSR_PMPCFG1: {
        const uint8_t *c = m->pmpcfg + 4 * (number - CSR_PMPCFG0);
        *value = (uint32_t)c[0] | (uint32_t)c[1] << 8 | (uint32_t)c[2] << 16 | (uint32_t)c[3] << 24;
        return true;
    }
    case CSR_MIE: *value = m->mie; return true;
    case CSR_MIP: *value = mip_now(m); return true;
    case CSR_MSCRATCH: *value = m->mscratch; return true;
    case CSR_MTVEC: *value = m->mtvec; return true;
    case CSR_MEPC: *value = m->mepc; return true;
    case CSR_MCAUSE: *value = m->mcause; return true;
    case CSR_MTVAL: *value = m->mtval; return true;
    /* Zicntr. `cycle` counts device ticks (docs/rv32.md, "Device time"): on the emulator a
     * tick is an executed instruction, so it reads the steps before this one. `time` shadows
     * the CLINT's mtime, so a write to mtime moves it. `instret` counts retired instructions
     * before this one, which is the same number on every backend. */
    case CSR_CYCLE: *value = (uint32_t)m->steps; return true;
    case CSR_CYCLEH: *value = (uint32_t)(m->steps >> 32); return true;
    case CSR_TIME: *value = (uint32_t)mtime_now(m); return true;
    case CSR_TIMEH: *value = (uint32_t)(mtime_now(m) >> 32); return true;
    case CSR_INSTRET: *value = (uint32_t)m->retired; return true;
    case CSR_INSTRETH: *value = (uint32_t)(m->retired >> 32); return true;
    default:
        if (number >= CSR_PMPADDR0 && number <= CSR_PMPADDR7) {
            *value = m->pmpaddr[number - CSR_PMPADDR0];
            return true;
        }
        return false;
    }
}

/* CSR numbers 0xc00-0xfff (bits 11:10 set) are read-only: a write traps. */
static bool csr_read_only(uint32_t number)
{
    return (number >> 10) == 3;
}

static void csr_write(machine *m, uint32_t number, uint32_t value)
{
    switch (number) {
    case CSR_FFLAGS: m->fcsr = (m->fcsr & 0xe0u) | (value & 31u); m->wr_fcsr = true; break;
    case CSR_FRM: m->fcsr = (m->fcsr & 31u) | ((value & 7u) << 5); m->wr_fcsr = true; break;
    case CSR_FCSR: m->fcsr = value & 255u; m->wr_fcsr = true; break;
    case CSR_MSTATUS:
        m->mstatus = value & (MSTATUS_MIE | MSTATUS_MPIE);
        m->mpp = (value & MSTATUS_MPP) == MSTATUS_MPP ? PRIV_M : PRIV_U; /* WARL: 3 or 0 */
        break;
    case CSR_MCOUNTEREN: m->mcounteren = value & 7u; break;
    case CSR_PMPCFG0: case CSR_PMPCFG1:
        for (uint32_t k = 0; k < 4; k++) {
            uint32_t i = 4 * (number - CSR_PMPCFG0) + k, cfg = (value >> (8 * k)) & 0x9fu; /* bits 6:5 read 0 */
            if (m->pmpcfg[i] & PMP_L) {
                continue; /* a locked entry ignores writes until reset */
            }
            if ((cfg & PMP_W) && !(cfg & PMP_R)) {
                cfg &= ~PMP_W; /* W without R is reserved: stored as neither */
            }
            m->pmpcfg[i] = (uint8_t)cfg;
        }
        break;
    case CSR_MIE: m->mie = value & MIE_MASK; break;
    case CSR_MIP: break; /* MSIP, MTIP and MEIP are read-only: the write is legal and does nothing */
    case CSR_MSCRATCH: m->mscratch = value; break;
    case CSR_MTVEC: m->mtvec = value & ~3u; break; /* direct mode only (WARL) */
    case CSR_MEPC: m->mepc = value & ~3u; break;   /* IALIGN is 32 */
    case CSR_MCAUSE: m->mcause = value; break;
    case CSR_MTVAL: m->mtval = value; break;
    default:
        if (number >= CSR_PMPADDR0 && number <= CSR_PMPADDR7) {
            uint32_t i = number - CSR_PMPADDR0;
            bool locked = (m->pmpcfg[i] & PMP_L) ||
                          (i + 1 < PMP_ENTRIES && (m->pmpcfg[i + 1] & (PMP_L | PMP_A)) == (PMP_L | PMP_TOR));
            if (!locked) {
                m->pmpaddr[i] = value;
            }
        }
        break;
    }
}

/* User mode (O5) may use the floating CSRs and, as mcounteren allows, the counters; every
 * machine CSR is an illegal instruction there. */
static bool csr_allowed(const machine *m, uint32_t number)
{
    if (m->priv == PRIV_M) {
        return true;
    }
    if ((number >> 8) & 3u) {
        return false;
    }
    if (number >= CSR_CYCLE && number <= CSR_INSTRETH) {
        return (m->mcounteren >> (number & 3u)) & 1u;
    }
    return true;
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

/* The M extension (unprivileged spec, chapter "M" Extension): the three high-half products
 * come from 64-bit arithmetic with each operand extended by its own signedness; division by
 * zero returns all ones (quotient) and the dividend (remainder), and the one signed overflow,
 * INT32_MIN / -1, returns the dividend and a zero remainder. Neither case traps. These are the
 * same definitions programs/rv32/rt/muldiv.c implements in software for RV32I builds. */
static uint32_t muldiv(uint32_t funct3, uint32_t a, uint32_t b)
{
    int64_t sa = (int32_t)a, sb = (int32_t)b;
    bool overflow = a == 0x80000000u && b == 0xffffffffu;
    switch (funct3) {
    case 0: return a * b;                                               /* MUL */
    case 1: return (uint32_t)((uint64_t)(sa * sb) >> 32);                 /* MULH */
    case 2: return (uint32_t)((uint64_t)(sa * (int64_t)b) >> 32);         /* MULHSU */
    case 3: return (uint32_t)(((uint64_t)a * b) >> 32);                   /* MULHU */
    case 4:                                                               /* DIV */
        if (b == 0) {
            return 0xffffffffu;
        }
        if (overflow) {
            return a;
        }
        return (uint32_t)((int32_t)a / (int32_t)b);
    case 5: return b == 0 ? 0xffffffffu : a / b;                          /* DIVU */
    case 6:                                                               /* REM */
        if (b == 0) {
            return a;
        }
        if (overflow) {
            return 0;
        }
        return (uint32_t)((int32_t)a % (int32_t)b);
    default: return b == 0 ? a : a % b;                                   /* REMU */
    }
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
    /* An enabled, pending interrupt is taken before the instruction (O1): MEI, then MSI, then MTI. */
    /* In user mode interrupts are always enabled (O5). */
    uint32_t pending = ((m->mstatus & MSTATUS_MIE) || m->priv == PRIV_U) && m->mie ? mip_now(m) & m->mie : 0u;
    if (pending) {
        take_interrupt(m, (pending >> IRQ_MEI) & 1u ? IRQ_MEI : (pending >> IRQ_MSI) & 1u ? IRQ_MSI : IRQ_MTI);
        return;
    }
    if (pc & 3u) { /* unreachable through the checked paths, kept as a guard */
        trap(m, 0, CAUSE_FETCH_MISALIGNED, pc);
        return;
    }
    if (!in_ram(pc, 4) || !pmp_allows(m, pc, PMP_X)) {
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
        if (funct7 == 1) { /* M extension: every funct3 is defined, and none of them traps */
            result = muldiv(funct3, a, b);
            writes_rd = true;
            break;
        }
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
                trap(m, word, m->priv == PRIV_U ? CAUSE_ECALL_U : CAUSE_ECALL_M, 0);
                return;
            }
            if (word == 0x00100073u) {
                trap(m, word, CAUSE_BREAKPOINT, pc);
                return;
            }
            if (word == 0x30200073u && m->priv == PRIV_M) {
                /* MRET: MIE from MPIE, MPIE set, the mode from MPP, MPP to user (O5) */
                m->mstatus = MSTATUS_MPIE | ((m->mstatus & MSTATUS_MPIE) ? MSTATUS_MIE : 0u);
                m->priv = m->mpp;
                m->mpp = PRIV_U;
                next = m->mepc;
                break;
            }
            if (word == 0x10500073u && m->priv == PRIV_M) { /* WFI: retires at once (O1); illegal in user mode */
                break;
            }
            goto illegal;
        }
        if (funct3 == 4) {
            goto illegal;
        }
        uint32_t number = word >> 20, old, operand = (funct3 & 4u) ? rs1 : a;
        bool writes = (funct3 & 3u) == 1 || rs1 != 0; /* csrrs/csrrc with a zero field only read */
        if (!csr_read(m, number, &old) || (writes && csr_read_only(number)) || !csr_allowed(m, number)) {
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
    gpu_tick(&m->gpu,m->ram,RAM_SIZE,m->fb,false);
    g3d_tick(&m->g3d,m->ram,RAM_SIZE,m->fb,false);
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
    fprintf(out, "mstatus %08" PRIx32 "\nmie %08" PRIx32 "\nmip %08" PRIx32 "\nmscratch %08" PRIx32 "\n",
            m->mstatus | MSTATUS_CONSTANT | (m->mpp == PRIV_M ? MSTATUS_MPP : 0u), m->mie, mip_now(m), m->mscratch);
    fprintf(out, "priv %u\n", m->priv);
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

void emu_read_console_input(machine *m, const char *path)
{
    if (strcmp(path, "-") == 0) {
        m->console_stdin = true;
        return;
    }
    FILE *in = fopen(path, "rb");
    if (!in) {
        fprintf(stderr, "%s: cannot open console input %s\n", emu_prog, path);
        exit(EXIT_EMULATOR_ERROR);
    }
    size_t capacity = 0;
    int c;
    while ((c = fgetc(in)) != EOF) {
        if (m->console_in_len == capacity) {
            capacity = capacity ? 2 * capacity : 256;
            m->console_in = realloc(m->console_in, capacity);
            if (!m->console_in) {
                fprintf(stderr, "%s: cannot allocate console input\n", emu_prog);
                exit(EXIT_EMULATOR_ERROR);
            }
        }
        m->console_in[m->console_in_len++] = (uint8_t)c;
    }
    if (ferror(in)) { /* a directory opens but does not read */
        fprintf(stderr, "%s: cannot read console input %s: %s\n", emu_prog, path, strerror(errno));
        exit(EXIT_EMULATOR_ERROR);
    }
    fclose(in);
}

void emu_open_disk(machine *m, const char *path)
{
    virtio_blk *v = m->virtio;
    v->file = fopen(path, "r+b");
    if (!v->file) {
        fprintf(stderr, "%s: cannot open disk %s: %s\n", emu_prog, path, strerror(errno));
        exit(EXIT_EMULATOR_ERROR);
    }
    size_t got = fread(v->disk, 1, VIRTIO_DISK_SIZE, v->file);
    if (ferror(v->file) || got != VIRTIO_DISK_SIZE || fgetc(v->file) != EOF) {
        fprintf(stderr, "%s: disk %s must be exactly %u bytes\n", emu_prog, path, VIRTIO_DISK_SIZE);
        exit(EXIT_EMULATOR_ERROR);
    }
}

void emu_init(machine *m)
{
    memset(m, 0, sizeof *m);
    simd_reset(&m->simd);
    gpu_device_reset(&m->gpu);
    g3d_device_reset(&m->g3d);
    m->mtimecmp = ~0ull;
    m->priv = m->mpp = PRIV_M; /* O5: machine mode from reset, MPP reading 3 until a trap or a write */
    /* Boot convention (docs/rv32.md, "Reset"): the hart id in a0, the device tree in a1. */
    m->x[10] = BOOT_HART;
    m->x[11] = RV32_DTB_ROM_BASE;
    m->limit = 100000000ull;
}

bool emu_alloc(machine *m)
{
    m->ram = calloc(RAM_SIZE, 1);
    m->fb = calloc(FB_SIZE, 1); /* unspecified by the contract; zero like the RTL testbench */
    m->virtio = calloc(1, sizeof *m->virtio);
    if (m->virtio) {
        m->virtio->queue_sel_zero = true;
    }
    if (!m->ram || !m->fb || !m->virtio) {
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
    free(m->console_in);
    m->console_in = NULL;
    if (m->virtio && m->virtio->file) {
        fclose(m->virtio->file);
    }
    free(m->virtio);
    m->virtio = NULL;
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
    if (m->virtio && m->virtio->write_error) {
        fprintf(stderr, "%s: error writing the disk\n", emu_prog);
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
    fprintf(stderr, "%s: halt=%s steps=%" PRIu64 " retired=%" PRIu64 " traps=%" PRIu64, emu_prog,
            emu_halt_name(m->halt), m->steps, m->retired, m->traps);
    if (m->interrupts) {
        fprintf(stderr, " interrupts=%" PRIu64, m->interrupts);
    }
    fprintf(stderr, " loaded=%zu", loaded);
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

/* Debugger access (docs/rv32-gdb.md). A debugger looks at the machine without being part of
 * it: reads and writes go straight to the RAM and framebuffer arrays, never through the bus
 * handlers, so reading a device cannot pop the input queue, print a console byte, present a
 * frame, or disturb an accelerator, and no G1 source or G2 depth lock refuses the debugger.
 * Device windows and unmapped addresses are refused as a whole: the access must lie entirely
 * inside RAM or entirely inside the framebuffer. The arithmetic is 64-bit so a range that
 * wraps past 0xffffffff is refused instead of aliasing low memory. */
static uint8_t *debug_bytes(const machine *m, uint32_t addr, size_t n)
{
    uint64_t end = (uint64_t)addr + n;
    if (addr >= RAM_BASE && end <= (uint64_t)RAM_BASE + RAM_SIZE) {
        return m->ram + (addr - RAM_BASE);
    }
    if (addr >= FB_BASE && end <= (uint64_t)FB_BASE + FB_SIZE) {
        return m->fb + (addr - FB_BASE);
    }
    return NULL;
}

bool emu_debug_read(const machine *m, uint32_t addr, uint8_t *out, size_t n)
{
    const uint8_t *p = debug_bytes(m, addr, n);
    if (!p) {
        return false;
    }
    memcpy(out, p, n);
    return true;
}

bool emu_debug_write(machine *m, uint32_t addr, const uint8_t *in, size_t n)
{
    uint8_t *p = debug_bytes(m, addr, n);
    if (!p) {
        return false;
    }
    memcpy(p, in, n);
    return true;
}

/* CSRs by number with the instructions' own rules: a number csr_read does not know is refused,
 * and a write applies the same WARL masks a CSRRW would (mtvec/mepc low bits clear, fcsr eight
 * bits, fflags five, frm three). The read-only Zicntr counters refuse writes, as a CSRRW traps. */
bool emu_csr_read(const machine *m, uint32_t number, uint32_t *value)
{
    return csr_read(m, number, value);
}

bool emu_csr_write(machine *m, uint32_t number, uint32_t value)
{
    uint32_t old;
    if (!csr_read(m, number, &old) || csr_read_only(number)) { /* absent, or a read-only counter */
        return false;
    }
    csr_write(m, number, value);
    return true;
}
