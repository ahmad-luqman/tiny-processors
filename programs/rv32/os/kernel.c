/* The kernel (Track 2, O2; docs/rv32-os.md).
 *
 * It boots from the device tree it is given in a1, loads programs from the RAM
 * disk bundled into its image into fixed slots of 128 KiB (a program may span
 * several; sys.h), and serves their
 * system calls (sys.h). Every context, each process and the idle loop, has a
 * frame; kentry.S saves the running one on every trap and resumes whichever
 * kernel_trap returns. The kernel runs with interrupts off and must never trap
 * itself: while it runs mscratch is 0, and a trap that finds it so is a fault
 * in the kernel, which kentry.S reports through kernel_fault() before stopping
 * the machine, rather than saving registers into a frame that is not there.
 *
 * Devices come only from the tree: the console, the done register, the CLINT
 * and the PLIC on every platform, and our input, display and accelerators
 * where the tree lists them. On QEMU the tree lies in RAM a program slot
 * covers, so everything is read from it before the first program is loaded.
 *
 * Since O4 the timer interrupt also preempts: when another process is ready,
 * the running one goes to the back of the round and the next one runs.
 *
 * Since O5 programs run in user mode. PMP grants the running process its own
 * slots, the framebuffer and, to a program that drives them, the accelerators;
 * anything else it touches, and any privileged instruction, is a fault, and a
 * fault kills the process. The kernel (machine mode, no locked entries) is not
 * held by PMP, so system calls still read and write the caller's memory.
 *
 * PMP holds the CPU, not the accelerators: G1 and G2 read and write memory by
 * DMA wherever their registers point (G2's depth buffer, G1's blit source). So
 * the kernel also sets the DMA window (issue #20) to the running process's
 * slots, and the engines refuse a job that would reach the kernel or another
 * slot. The window's page lies outside the accelerators' PMP region, so a
 * program cannot move it. While an engine is busy it owns the framebuffer (a
 * present or a CPU store to it faults), so only a flagged program runs until
 * the engines are idle again, and nobody else can fault on its behalf.
 *
 * Blocking is by retry: a call that cannot finish yet (read with no byte
 * waiting, wait for a child still running) leaves the process's pc on its
 * ecall and marks it blocked; wake_blocked() makes it ready again when the
 * condition may hold, and the ecall runs again. A timer interrupt every
 * KERNEL_TICK device ticks makes sure a blocked process is looked at even
 * while the machine idles.
 */
#include <stddef.h>
#include <stdint.h>

#include "board.h"
#include "clint.h"
#include "csr.h"
#include "fdt.h"
#include "fs.h"
#include "g3d.h"
#include "gpu.h"
#include "mmio.h"
#include "sys.h"
#include "virtio.h"

#define MAX_PROCS 8
#define KERNEL_TICK 10000u      /* device ticks between timer interrupts, where ticks have no rate */
#define KEY_BUFFER 64u          /* input events waiting for a program */
#define ARGS_MAX 64u            /* bytes of argument string, NUL included */
#define MSTATUS_MPP_M 0x1800u  /* the idle loop; a process's MPP is 0, user mode (O5) */
#define MSTATUS_FS_INITIAL 0x2000u /* QEMU's FPU is off until FS is set; ours is always on */
#define CAUSE_ECALL_M 11u
#define CAUSE_ECALL_U 8u
#define RAMDISK_MAGIC 0x4b534452u /* "RDSK" */
#define OPEN_FILES 4u              /* descriptors 3..6 */

/* A context: kentry.S knows these offsets. */
struct frame {
    uint32_t x[32]; /* x[0] is never read */
    uint32_t pc;
    uint32_t mstatus;
};
_Static_assert(offsetof(struct frame, pc) == 128 && offsetof(struct frame, mstatus) == 132, "kentry.S offsets");

enum state { FREE, READY, RUNNING, BLOCKED_READ, BLOCKED_WAIT, BLOCKED_SLEEP, ZOMBIE };

struct open_file {
    uint32_t mode; /* 0 (closed), O_READ or O_WRITE */
    int file;      /* fs.c's index */
    uint32_t position;
};

struct proc {
    struct frame f;
    struct open_file files[OPEN_FILES];
    enum state state;
    uint32_t pid, parent, base, span, brk, exit_code, wait_pid, flags, switches;
    uint64_t wake;
    char name[24];
};

/* A RAM disk entry (tools/rv32_ramdisk.py). */
struct program {
    char name[24];
    uint32_t load, entry, file_size, memory_size, offset, flags, span; /* span in bytes, whole slots */
};
_Static_assert(sizeof(struct program) == 52, "tools/rv32_ramdisk.py's <24s7I entry");
#define PROGRAM_ACCELERATORS 1u /* it drives SIMD4, G1 and G2 itself */

extern void trap_vector(void);
extern void kernel_idle(void);
extern _Noreturn void kernel_resume(struct frame *f);
extern const uint8_t ramdisk[], ramdisk_end[];

static struct proc procs[MAX_PROCS];
static struct proc *current; /* 0 while the idle loop runs */
static struct frame idle_frame;
static uint32_t idle_stack[64];
static uint32_t next_pid = 1, exits, exit_sum;

static uint32_t console, done_register, clint, plic;
static uint32_t input, input_source, display, framebuffer, framebuffer_size, gpu, g3d, disk;
static uint32_t accelerators, accelerators_end; /* O5: the window PMP grants a PROGRAM_ACCELERATORS program */
static uint32_t dma_window;                     /* issue #20: the engines' DMA bound, 0 where there is none (QEMU) */
static uint32_t protected_pid;                  /* O5: the process PMP is set up for */
static char model[48];
static uint32_t tick = KERNEL_TICK; /* O4: 100 µs where the tree gives a timebase (QEMU), else KERNEL_TICK */

static int faulted; /* kernel_fault ran: halt must not touch the disk again */

static uint32_t keys[KEY_BUFFER];
static uint32_t key_head, key_count, keys_dropped;

void *memcpy(void *dst, const void *src, size_t n); /* mem.c */
void *memset(void *dst, int value, size_t n);

/* The console, polled: LSR bit 5 before each byte, as on a 16550. */
static void kputc(char c)
{
    while (!(mmio_read8(console + RV32_CONSOLE_STATUS) & RV32_CONSOLE_TX_READY)) {
    }
    mmio_write8(console + RV32_CONSOLE_TX, (uint8_t)c);
}

static void kputs(const char *s)
{
    while (*s) {
        kputc(*s++);
    }
}

/* The decimal digits of `value`, ending at digits[10] = 0; returns where they start. */
static uint32_t kdecimal(uint32_t value, char digits[11])
{
    uint32_t i = 10;
    digits[i] = 0;
    do {
        digits[--i] = (char)('0' + value % 10u);
        value /= 10u;
    } while (value);
    return i;
}

static void kputdec(uint32_t value)
{
    char digits[11];
    kputs(digits + kdecimal(value, digits));
}

static void kputhex(uint32_t value)
{
    for (int shift = 28; shift >= 0; shift -= 4) {
        uint32_t digit = (value >> shift) & 15u;
        kputc((char)(digit < 10 ? '0' + digit : 'a' + digit - 10));
    }
}

/* Hexadecimal without leading zeros. */
static void kputhex_short(uint32_t value)
{
    int shift = 28;
    while (shift > 0 && !(value >> shift)) {
        shift -= 4;
    }
    for (; shift >= 0; shift -= 4) {
        uint32_t digit = (value >> shift) & 15u;
        kputc((char)(digit < 10 ? '0' + digit : 'a' + digit - 10));
    }
}

static int console_ready(void)
{
    return mmio_read8(console + RV32_CONSOLE_STATUS) & 1u; /* LSR.DR */
}

static uint64_t mtime(void)
{
    return clint_mtime(clint);
}

static void set_timer(uint64_t when)
{
    clint_set_mtimecmp(clint, when);
}

static uint32_t fold_name(const char *s)
{
    uint32_t h = 2166136261u;
    while (*s) {
        h = (h ^ (uint8_t)*s++) * 16777619u;
    }
    return h;
}

/* ---- Stopping the machine ---- */

static _Noreturn void halt(uint32_t code)
{
    /* A file's size reaches the disk only when the directory is written back: do it for files a
     * process (a background writer, say) still has open for writing. */
    int writing = 0;
    for (uint32_t i = 0; i < MAX_PROCS; i++) {
        for (uint32_t k = 0; k < OPEN_FILES; k++) {
            writing |= procs[i].state != FREE && procs[i].files[k].mode == O_WRITE;
        }
    }
    if (writing && !faulted && !fs_flush()) {
        kputs("kernel: halt: the directory could not be written\n");
    }
    if (keys_dropped) {
        kputs("kernel: ");
        kputdec(keys_dropped);
        kputs(" input events dropped: the buffer was full\n");
    }
    if (code == 0) {
        kputs("kernel: halt, ");
        kputdec(exits);
        kputs(" exits\nPASS ");
        kputhex(exit_sum);
        kputc('\n');
        mmio_write32(done_register, RV32_DONE_PASS);
    } else {
        kputs("kernel: halt ");
        kputdec(code);
        kputc('\n');
        mmio_write32(done_register, (code & 0xffu) << 16 | RV32_DONE_FAIL);
    }
    for (;;) {
    }
}

static _Noreturn void panic(const char *why)
{
    kputs("kernel: panic: ");
    kputs(why);
    kputc('\n');
    halt(255);
}

/* ---- The device tree, read once at boot ---- */

static uint32_t find(const fdt *t, const char *compatible, uint32_t index, int required)
{
    uint32_t base = 0, size;
    fdt_status status = fdt_find(t, "compatible", compatible, index, &base, &size);
    if (status == FDT_NOT_FOUND && !required) {
        return 0;
    }
    if (status != FDT_OK) {
        kputs("kernel: device tree: ");
        kputs(compatible);
        kputs(" status ");
        kputdec((uint32_t)status);
        kputc('\n');
        panic("device tree");
    }
    return base;
}

static void discover(uintptr_t address)
{
    fdt t;
    if (fdt_open(&t, address) != FDT_OK) {
        /* No console yet: the done register is the only way to say so, at virt's address. */
        mmio_write32(RV32_DONE, 254u << 16 | RV32_DONE_FAIL);
        for (;;) {
        }
    }
    done_register = find(&t, "sifive,test0", 0, 1);
    console = find(&t, "tiny-processors,console", 0, 0);
    if (!console) {
        console = find(&t, "ns16550a", 0, 1);
    }
    clint = find(&t, "riscv,clint0", 0, 1);
    plic = find(&t, "riscv,plic0", 0, 1);
    input = find(&t, "tiny-processors,input", 0, 0);
    if (input && fdt_cell(&t, "compatible", "tiny-processors,input", "interrupts", 0, &input_source) != FDT_OK) {
        panic("input without interrupts");
    }
    display = find(&t, "tiny-processors,display", 0, 0);
    if (display && fdt_find(&t, "compatible", "tiny-processors,display", 1, &framebuffer, &framebuffer_size) != FDT_OK) {
        panic("display without a framebuffer");
    }
    /* The disk: the first "virtio,mmio" node with a block device behind it. QEMU lists all eight
     * of virt's slots, most of them empty (DeviceID 0); our tree lists the one we have. */
    for (uint32_t node = 0;; node++) {
        uint32_t base, size;
        fdt_status status = fdt_find_nth(&t, "compatible", "virtio,mmio", node, 0, &base, &size);
        if (status == FDT_NOT_FOUND) {
            break;
        }
        if (status != FDT_OK) {
            panic("virtio node");
        }
        if (mmio_read32(base + 8) == 2) {
            disk = base;
            break;
        }
    }
    gpu = find(&t, "tiny-processors,g1", 0, 0);
    g3d = find(&t, "tiny-processors,g2", 0, 0);
    /* O5: one PMP region from the lowest accelerator window to the end of the highest. */
    static const struct { const char *compatible; uint32_t index; } windows[] = {
        {"tiny-processors,simd4", 0}, {"tiny-processors,simd4", 1}, {"tiny-processors,simd4", 2},
        {"tiny-processors,g1", 0}, {"tiny-processors,g2", 0},
    };
    for (uint32_t w = 0; w < sizeof windows / sizeof windows[0]; w++) {
        uint32_t base, size;
        if (fdt_find(&t, "compatible", windows[w].compatible, windows[w].index, &base, &size) == FDT_OK) {
            if (!accelerators_end || base < accelerators) {
                accelerators = base;
            }
            if (base + size > accelerators_end) {
                accelerators_end = base + size;
            }
        }
    }
    /* The DMA window: required wherever G1 or G2 is (without it the engines reach all of RAM),
     * and outside the region PMP grants, or a program could widen it. QEMU has neither. */
    uint32_t window_size = 0;
    if (fdt_find(&t, "compatible", "tiny-processors,dma-window", 0, &dma_window, &window_size) != FDT_OK) {
        dma_window = 0;
    }
    if ((gpu || g3d) && !dma_window) {
        panic("g1 or g2 without a DMA window");
    }
    if (dma_window && accelerators_end && dma_window + window_size > accelerators && dma_window < accelerators_end) {
        panic("the DMA window inside the accelerators' region");
    }
    uint32_t timebase;
    if (fdt_cell(&t, "@name", "cpus", "timebase-frequency", 0, &timebase) == FDT_OK && timebase >= 10000u) {
        tick = timebase / 10000u;
    }
    const char *name = "unknown";
    (void)fdt_root_string(&t, "model", &name);
    uint32_t i = 0;
    for (; name[i] && i + 1 < sizeof model; i++) {
        model[i] = name[i];
    }
    model[i] = 0;
}

/* ---- Processes ---- */

static struct proc *proc_of(struct frame *f)
{
    return f == &idle_frame ? 0 : (struct proc *)((uint8_t *)f - offsetof(struct proc, f));
}

/* A user range is `len` bytes inside the process's own slots. */
static int user_range(const struct proc *p, uint32_t address, uint32_t len)
{
    return address >= p->base && len <= p->span && address - p->base <= p->span - len;
}

/* A NUL-terminated user string of at most `max` bytes, copied out; 0 on a bad pointer or length. */
static int user_string(const struct proc *p, uint32_t address, char *out, uint32_t max)
{
    for (uint32_t i = 0; i < max; i++) {
        if (!user_range(p, address + i, 1)) {
            return 0;
        }
        out[i] = *(const char *)(uintptr_t)(address + i);
        if (!out[i]) {
            return 1;
        }
    }
    return 0;
}

static const struct program *programs(uint32_t *count)
{
    const uint32_t *header = (const uint32_t *)ramdisk;
    if ((uint32_t)(ramdisk_end - ramdisk) < 16 || header[0] != RAMDISK_MAGIC) {
        panic("no RAM disk");
    }
    *count = header[1];
    return (const struct program *)(ramdisk + 16);
}

/* The RAM disk is part of the kernel's image, but spawn trusts its table, so it is checked once
 * at boot for what tools/rv32_ramdisk.py promises: every entry inside the disk and its slots,
 * the file no larger than the memory it loads into, the entry point inside the file. */
static void check_programs(void)
{
    uint32_t count, size = (uint32_t)(ramdisk_end - ramdisk);
    const struct program *list = programs(&count);
    if (count > (size - 16) / sizeof *list) {
        panic("RAM disk table larger than the disk");
    }
    for (uint32_t i = 0; i < count; i++) {
        const struct program *e = &list[i];
        uint32_t slots_end = OS_SLOT_BASE + OS_SLOTS * OS_SLOT_SIZE;
        if (e->name[sizeof e->name - 1] || e->offset > size || e->file_size > size - e->offset ||
            e->file_size > e->memory_size || e->span == 0 || e->span % OS_SLOT_SIZE ||
            e->span > OS_SLOTS * OS_SLOT_SIZE || e->load < OS_SLOT_BASE || (e->load - OS_SLOT_BASE) % OS_SLOT_SIZE ||
            e->load > slots_end - e->span || e->memory_size > e->span - OS_STACK_SIZE ||
            e->entry < e->load || e->entry - e->load >= e->file_size) {
            kputs("kernel: RAM disk entry ");
            kputdec(i);
            kputc('\n');
            panic("bad RAM disk entry");
        }
    }
}

static const struct program *program_named(const char *name)
{
    uint32_t count;
    const struct program *list = programs(&count);
    for (uint32_t i = 0; i < count; i++) {
        if (fdt_same(list[i].name, name)) {
            return &list[i];
        }
    }
    return 0;
}

static int alive(const struct proc *p)
{
    return p->state != FREE && p->state != ZOMBIE;
}

/* Load a program into its slots and make it ready; returns the process or 0. */
static struct proc *spawn(const char *name, const char *args, uint32_t parent)
{
    const struct program *program = program_named(name);
    if (!program) {
        return 0;
    }
    struct proc *p = 0;
    for (uint32_t i = 0; i < MAX_PROCS; i++) {
        const struct proc *q = &procs[i];
        if (alive(q) && q->base < program->load + program->span && program->load < q->base + q->span) {
            return 0; /* its slots are in use: one instance at a time */
        }
        if (!p && procs[i].state == FREE) {
            p = &procs[i];
        }
    }
    if (!p) {
        return 0;
    }
    memset(p, 0, sizeof *p);
    p->base = program->load;
    p->span = program->span;
    p->flags = program->flags;
    uint8_t *base = (uint8_t *)(uintptr_t)program->load;
    memcpy(base, ramdisk + program->offset, program->file_size);
    memset(base + program->file_size, 0, program->memory_size - program->file_size);
    uint32_t top = p->base + p->span;
    char *copy = (char *)(uintptr_t)(top - ARGS_MAX);
    uint32_t i = 0;
    for (; args[i] && i + 1 < ARGS_MAX; i++) {
        copy[i] = args[i];
    }
    copy[i] = 0;
    p->f.x[2] = (top - ARGS_MAX - 16u) & ~15u; /* sp */
    p->f.x[10] = (uint32_t)(uintptr_t)copy;      /* a0: the arguments */
    p->f.pc = program->entry;
    p->f.mstatus = MSTATUS_MPIE | MSTATUS_FS_INITIAL; /* MPP 0: mret enters user mode (O5) */
    p->brk = (program->load + program->memory_size + 15u) & ~15u;
    p->pid = next_pid++;
    p->parent = parent;
    p->state = READY;
    for (i = 0; program->name[i] && i + 1 < sizeof p->name; i++) {
        p->name[i] = program->name[i];
    }
    return p;
}

/* Descriptor `fd` of `p` when it is open in `mode` (0: either), else 0. */
static struct open_file *open_file_of(struct proc *p, uint32_t fd, uint32_t mode)
{
    if (fd < 3 || fd >= 3 + OPEN_FILES || !p->files[fd - 3].mode || (mode && p->files[fd - 3].mode != mode)) {
        return 0;
    }
    return &p->files[fd - 3];
}

/* Whether file `file` may be opened in `mode`: one writer at a time, and no readers while it is
 * written, since writing truncates it first. */
static int may_open(int file, uint32_t mode)
{
    for (uint32_t i = 0; i < MAX_PROCS; i++) {
        for (uint32_t k = 0; procs[i].state != FREE && k < OPEN_FILES; k++) {
            const struct open_file *o = &procs[i].files[k];
            if (o->mode && o->file == file && (mode == O_WRITE || o->mode == O_WRITE)) {
                return 0;
            }
        }
    }
    return 1;
}

static uint32_t close_file(struct open_file *o)
{
    uint32_t mode = o->mode;
    o->mode = 0;
    return mode == O_WRITE && !fs_flush() ? SYS_ERROR : 0; /* a written file's size reaches the disk */
}

/* A program that drives the engines has ended. Unless another such program lives (whose job it
 * may be), stop any job still running: its blit source or depth buffer lies in slots about to be
 * freed, and the next program there must not have an engine writing into it. */
static void stop_orphaned_engines(const struct proc *p)
{
    if (!(p->flags & PROGRAM_ACCELERATORS)) {
        return;
    }
    for (uint32_t i = 0; i < MAX_PROCS; i++) {
        if (&procs[i] != p && alive(&procs[i]) && (procs[i].flags & PROGRAM_ACCELERATORS)) {
            return;
        }
    }
    if (gpu && (mmio_read32(gpu + GPU_STATUS) & GPU_BUSY)) {
        mmio_write32(gpu + GPU_COMMAND, GPU_RESET);
    }
    if (g3d && (mmio_read32(g3d + G3D_STATUS) & G3D_BUSY)) {
        mmio_write32(g3d + G3D_COMMAND, G3D_RESET);
    }
}

static void finish(struct proc *p, uint32_t code)
{
    stop_orphaned_engines(p);
    for (uint32_t i = 0; i < OPEN_FILES; i++) {
        if (close_file(&p->files[i]) == SYS_ERROR) {
            kputs("kernel: pid ");
            kputdec(p->pid);
            kputs(": a file's size could not be written\n");
        }
    }
    /* Children it never waited for: nobody else can, so the finished ones are freed now and the
     * running ones will be when they finish (their parent is gone). */
    for (uint32_t i = 0; i < MAX_PROCS; i++) {
        if (procs[i].state == ZOMBIE && procs[i].parent == p->pid) {
            procs[i].state = FREE;
        }
    }
    p->exit_code = code;
    exits++;
    exit_sum += fold_name(p->name) ^ code; /* a sum: the same whatever order processes finish in */
    struct proc *parent = 0;
    for (uint32_t i = 0; i < MAX_PROCS; i++) {
        if (alive(&procs[i]) && procs[i].pid == p->parent) {
            parent = &procs[i];
        }
    }
    if (!parent) {
        if (p->pid == 1) {
            halt(code); /* the first program, the shell, ended the session */
        }
        p->state = FREE; /* nobody will wait for it */
        return;
    }
    p->state = ZOMBIE;
    if (parent->state == BLOCKED_WAIT && parent->wait_pid == p->pid) {
        parent->state = READY;
    }
}

/* ---- Input ---- */

static void drain_input(void)
{
    for (uint32_t event = mmio_read32(input + RV32_INPUT_EVENT); event; event = mmio_read32(input + RV32_INPUT_EVENT)) {
        if (key_count == KEY_BUFFER) {
            keys_dropped++;
            continue;
        }
        keys[(key_head + key_count) % KEY_BUFFER] = event;
        key_count++;
    }
}

static void external_interrupt(void)
{
    uint32_t source = mmio_read32(plic + RV32_PLIC_CLAIM);
    if (source && input && source == input_source) {
        drain_input();
    }
    if (source) {
        mmio_write32(plic + RV32_PLIC_CLAIM, source);
    }
}

/* ---- Scheduling ---- */

/* Blocked processes whose condition may hold become ready; their ecall runs again. */
static void wake_blocked(void)
{
    uint64_t now = 0;
    int ready = -1;
    for (uint32_t i = 0; i < MAX_PROCS; i++) {
        struct proc *p = &procs[i];
        if (p->state == BLOCKED_READ) {
            if (ready < 0) {
                ready = console_ready();
            }
            if (ready) {
                p->state = READY;
            }
        } else if (p->state == BLOCKED_SLEEP) {
            if (!now) {
                now = mtime();
            }
            if (now >= p->wake) {
                p->state = READY;
            }
        }
    }
}

/* Whether G1 or G2 is busy. Only a program flagged `accelerators` starts them, so until one has run
 * the answer is no without a look; once one has, the kernel looks until it finds them idle with no
 * such program alive. A session without the menu never touches an engine register, and so stays
 * trace-comparable in step-tick mode (engines advance per clock). */
static int engines_used;

static uint32_t engines_busy(void)
{
    if (!engines_used) {
        return 0;
    }
    if ((gpu && (mmio_read32(gpu + GPU_STATUS) & GPU_BUSY)) || (g3d && (mmio_read32(g3d + G3D_STATUS) & G3D_BUSY))) {
        return 1;
    }
    engines_used = 0;
    for (uint32_t i = 0; i < MAX_PROCS; i++) {
        engines_used |= alive(&procs[i]) && (procs[i].flags & PROGRAM_ACCELERATORS);
    }
    return 0;
}

/* The next ready process after `after` in table order, round robin, or 0. While G1 or G2 is busy
 * only a program that drives them may run: anyone else's present or framebuffer store would fault. */
static struct proc *next_ready(const struct proc *after)
{
    uint32_t start = after ? (uint32_t)(after - procs) + 1 : 0;
    int busy = -1;
    for (uint32_t k = 0; k < MAX_PROCS; k++) {
        struct proc *p = &procs[(start + k) % MAX_PROCS];
        if (p->state != READY) {
            continue;
        }
        if (!(p->flags & PROGRAM_ACCELERATORS)) {
            if (busy < 0) {
                busy = (int)engines_busy();
            }
            if (busy) {
                continue;
            }
        }
        return p;
    }
    return 0;
}

/* O5: PMP for the process about to run, in TOR pairs: entries 0-1 its slots (RWX), 2-3 the
 * framebuffer (RW), 4-5 the accelerators (RW) for a program that drives them. Entries 6-7 stay
 * off. A user access nothing matches faults; the idle loop runs in machine mode and ignores it. */
static void protect(const struct proc *p)
{
    if (p->pid == protected_pid) {
        return;
    }
    protected_pid = p->pid;
    uint32_t tor_rwx = PMP_TOR | PMP_R | PMP_W | PMP_X, tor_rw = PMP_TOR | PMP_R | PMP_W;
    csr_write(CSR_PMPADDR0, p->base >> 2);
    csr_write(CSR_PMPADDR1, (p->base + p->span) >> 2);
    csr_write(CSR_PMPADDR2, framebuffer >> 2);
    csr_write(CSR_PMPADDR3, (framebuffer + framebuffer_size) >> 2);
    csr_write(CSR_PMPADDR4, accelerators >> 2);
    csr_write(CSR_PMPADDR5, accelerators_end >> 2);
    csr_write(CSR_PMPCFG0, tor_rwx << 8 | (framebuffer ? tor_rw << 24 : 0u));
    csr_write(CSR_PMPCFG1, (p->flags & PROGRAM_ACCELERATORS) && accelerators_end ? tor_rw << 8 : 0u);
    /* The engines may reach only this process's slots. A job validated earlier keeps running
     * (a flagged program's, while the switch goes to another flagged one or the idle loop). */
    if (dma_window) {
        mmio_write32(dma_window + RV32_DMA_WINDOW_START, p->base);
        mmio_write32(dma_window + RV32_DMA_WINDOW_END, p->base + p->span);
    }
}

static struct frame *schedule(void)
{
    wake_blocked();
    if (!current || current->state != RUNNING) {
        current = next_ready(current);
        if (!current) {
            return &idle_frame;
        }
        current->state = RUNNING;
    }
    if (current->flags & PROGRAM_ACCELERATORS) {
        engines_used = 1;
    }
    protect(current);
    return &current->f;
}

/* ---- System calls ---- */

/* Returns 1 when the call finished (pc moves past the ecall), 0 to run it again later. */
static int syscall(struct proc *p)
{
    struct frame *f = &p->f;
    uint32_t a0 = f->x[10], a1 = f->x[11], a2 = f->x[12], result = SYS_ERROR;
    char name[24], args[ARGS_MAX];
    switch (f->x[17]) {
    case SYS_EXIT:
        finish(p, a0);
        return 1;
    case SYS_WRITE: {
        struct open_file *o = open_file_of(p, a0, O_WRITE);
        if (o && user_range(p, a1, a2)) {
            result = fs_write(o->file, o->position, (const uint8_t *)(uintptr_t)a1, a2);
            if (result != FS_ERROR) {
                o->position += result;
            }
        } else if ((a0 == 1 || a0 == 2) && user_range(p, a1, a2)) {
            for (uint32_t i = 0; i < a2; i++) {
                kputc(*(const char *)(uintptr_t)(a1 + i));
            }
            result = a2;
        }
        break;
    }
    case SYS_READ: {
        struct open_file *o = open_file_of(p, a0, O_READ);
        if (o && user_range(p, a1, a2)) {
            result = fs_read(o->file, o->position, (uint8_t *)(uintptr_t)a1, a2);
            if (result != FS_ERROR) {
                o->position += result;
            }
        } else if (a0 == 0 && a2 && user_range(p, a1, a2)) {
            if (!console_ready()) {
                p->state = BLOCKED_READ;
                return 0;
            }
            result = 0;
            while (result < a2 && console_ready()) {
                *(uint8_t *)(uintptr_t)(a1 + result++) = mmio_read8(console + RV32_CONSOLE_TX);
            }
        }
        break;
    }
    case SYS_EVENT:
        result = 0;
        if (key_count) {
            result = keys[key_head];
            key_head = (key_head + 1) % KEY_BUFFER;
            key_count--;
        }
        break;
    case SYS_KEYS:
        result = input ? mmio_read32(input + RV32_INPUT_KEYS) : 0;
        break;
    case SYS_PRESENT:
        /* A present while an engine owns the framebuffer faults, and the kernel must not fault:
         * the call waits, running again, until the engines are idle. Only a program that drives
         * them runs meanwhile (next_ready), so this is the caller's own work finishing. */
        if (display) {
            if (engines_busy()) {
                return 0;
            }
            mmio_write32(display + RV32_DISPLAY_PRESENT, 1);
            result = mmio_read32(display + RV32_DISPLAY_FRAMES);
        }
        break;
    case SYS_SBRK:
        if (a0 <= p->base + p->span - OS_STACK_SIZE - p->brk) {
            result = p->brk;
            p->brk += a0;
        }
        break;
    case SYS_SPAWN:
        if (user_string(p, a0, name, sizeof name) && user_string(p, a1, args, sizeof args)) {
            struct proc *child = spawn(name, args, p->pid);
            result = child ? child->pid : SYS_ERROR;
        }
        break;
    case SYS_WAIT:
        for (uint32_t i = 0; i < MAX_PROCS; i++) {
            struct proc *child = &procs[i];
            if (child->state == FREE || child->pid != a0 || child->parent != p->pid) {
                continue;
            }
            if (child->state != ZOMBIE) {
                p->state = BLOCKED_WAIT;
                p->wait_pid = a0;
                return 0;
            }
            result = child->exit_code;
            child->state = FREE;
        }
        break;
    case SYS_LIST: {
        uint32_t count;
        const struct program *list = programs(&count);
        if (a0 < count && user_range(p, a1, a2)) {
            uint32_t n = 0;
            while (list[a0].name[n]) {
                n++;
            }
            if (n < a2) {
                memcpy((void *)(uintptr_t)a1, list[a0].name, n + 1);
                result = n;
            }
        }
        break;
    }
    case SYS_YIELD:
        p->state = READY;
        result = 0;
        break;
    case SYS_SLEEP:
        p->wake = mtime() + a0;
        p->state = BLOCKED_SLEEP;
        result = 0;
        break;
    case SYS_HALT:
        halt(a0);
    case SYS_TIME:
        result = (uint32_t)mtime();
        break;
    case SYS_GETPID:
        result = p->pid;
        break;
    case SYS_DISPLAY:
        result = framebuffer;
        break;
    case SYS_SWITCHES:
        result = p->switches;
        break;
    case SYS_PS:
        if (a0 < MAX_PROCS && procs[a0].state != FREE && user_range(p, a1, a2) && a2 >= 48) {
            static const char *const states[] = {"free", "ready", "running", "reading", "waiting", "sleeping", "zombie"};
            const struct proc *q = &procs[a0];
            char line[48];
            uint32_t n = 0;
            const char *parts[3] = {q->name, " ", states[q->state]};
            for (uint32_t k = 0; k < 3; k++) {
                for (const char *c = parts[k]; *c && n + 12 < sizeof line; c++) {
                    line[n++] = *c;
                }
            }
            line[n++] = ' ';
            char digits[11];
            uint32_t d = kdecimal(q->pid, digits);
            while (digits[d] && n + 1 < sizeof line) {
                line[n++] = digits[d++];
            }
            line[n] = 0;
            memcpy((void *)(uintptr_t)a1, line, n + 1);
            result = n;
        }
        break;
    case SYS_OPEN: {
        uint32_t mode = a1 & (O_READ | O_WRITE);
        if ((mode == O_READ || mode == O_WRITE) && !(a1 & ~(O_READ | O_WRITE | O_CREATE)) &&
            user_string(p, a0, name, FS_NAME)) {
            /* A free descriptor first, so a refused open never leaves a new, empty file behind. */
            uint32_t i = 0;
            while (i < OPEN_FILES && p->files[i].mode) {
                i++;
            }
            int file = i < OPEN_FILES ? fs_open(name, (a1 & O_CREATE) && mode == O_WRITE) : -1;
            if (file >= 0 && may_open(file, mode)) {
                p->files[i] = (struct open_file){mode, file, 0};
                if (mode == O_WRITE) {
                    fs_truncate(file); /* writing replaces the contents */
                }
                result = 3 + i;
            }
        }
        break;
    }
    case SYS_CLOSE:
        if (open_file_of(p, a0, 0)) {
            result = close_file(open_file_of(p, a0, 0));
        }
        break;
    case SYS_FILES: {
        char file_name[FS_NAME];
        uint32_t size;
        if (fs_name(a0, file_name, &size) && user_range(p, a1, a2)) {
            uint32_t n = 0;
            while (n < FS_NAME && file_name[n]) {
                n++;
            }
            if (n < a2) {
                memcpy((void *)(uintptr_t)a1, file_name, n);
                *(char *)(uintptr_t)(a1 + n) = 0;
                result = size;
            }
        }
        break;
    }
    default:
        break; /* an unknown call fails */
    }
    f->x[10] = result;
    return 1;
}

static void kill(struct proc *p, uint32_t cause, uint32_t tval)
{
    kputs("kernel: pid ");
    kputdec(p->pid);
    kputc(' ');
    kputs(p->name);
    kputs(" killed: cause ");
    kputdec(cause);
    kputs(" at ");
    if (user_range(p, p->f.pc, 4)) { /* inside its own code: relative to where it was loaded */
        kputs(p->name);
        kputs("+0x");
        kputhex_short(p->f.pc - p->base);
    } else {
        kputhex(p->f.pc);
    }
    kputs(" tval ");
    kputhex(tval);
    kputc('\n');
    finish(p, 128u + cause);
}

/* The disk driver gives up for good on a device that needs a reset or a lost request; say so once. */
static void report_disk_failure(void)
{
    static int reported;
    const char *why = virtio_failure();
    if (why && !reported) {
        reported = 1;
        kputs("kernel: disk disabled: ");
        kputs(why);
        kputc('\n');
    }
}

/* Called by kentry.S with the frame it saved; returns the frame to resume. */
struct frame *kernel_trap(struct frame *f)
{
    uint32_t cause = csr_read(CSR_MCAUSE), tval = csr_read(CSR_MTVAL);
    struct proc *p = proc_of(f);
    current = p;
    if (cause & MCAUSE_INTERRUPT) {
        uint32_t code = cause & 0x1fu;
        if (code == IRQ_MEI) {
            external_interrupt();
        } else if (code == IRQ_MTI) {
            set_timer(mtime() + tick);
            /* O4: round robin. The running process yields the machine when another is ready. */
            struct proc *next = p ? next_ready(p) : 0;
            if (p && p->state == RUNNING && next && next != p) {
                p->state = READY;
                p->switches++;
            }
        }
    } else if (!p) {
        panic("trap in the idle loop");
    } else if (cause == CAUSE_ECALL_M || cause == CAUSE_ECALL_U) {
        if (syscall(p)) {
            f->pc += 4;
        }
        report_disk_failure();
    } else {
        kill(p, cause, tval);
    }
    return schedule(); /* the running process keeps the machine unless it blocked, finished or was preempted */
}

/* Called by kentry.S for a trap taken while the kernel itself ran (mscratch 0): a bug in the
 * kernel, never a process's fault. The machine stops. */
_Noreturn void kernel_fault(void)
{
    faulted = 1;
    kputs("kernel: fault in the kernel: cause ");
    kputdec(csr_read(CSR_MCAUSE));
    kputs(" at ");
    kputhex(csr_read(CSR_MEPC));
    kputs(" tval ");
    kputhex(csr_read(CSR_MTVAL));
    kputc('\n');
    halt(254);
}

_Noreturn void kernel_main(uint32_t hart, uintptr_t tree)
{
    discover(tree);
    kputs("kernel: ");
    kputs(model);
    kputs(", hart ");
    kputdec(hart);
    kputs("\nkernel: devices console clint plic");
    kputs(input ? " input" : "");
    kputs(display ? " display" : "");
    kputs(gpu ? " g1" : "");
    kputs(g3d ? " g2" : "");
    kputs(dma_window ? " dma" : "");
    kputs(disk ? " disk" : "");
    uint32_t count;
    check_programs();
    (void)programs(&count);
    kputs("\nkernel: ");
    kputdec(count);
    kputs(" programs\n");
    if (disk) {
        uint32_t sectors = virtio_init(disk);
        int mounted = sectors && fs_mount(sectors);
        kputs("kernel: disk ");
        kputdec(sectors);
        kputs(mounted ? " sectors, tfs\n" : " sectors, no file system\n");
        if (mounted && fs_corrupt()) {
            kputs("kernel: tfs: ");
            kputdec(fs_corrupt());
            kputs(" corrupt directory entries set aside\n");
        }
    }

    csr_write(CSR_MTVEC, (uint32_t)(uintptr_t)trap_vector);
    if (input) {
        mmio_write32(plic + RV32_PLIC_PRIORITY(input_source), 1);
        mmio_write32(plic + RV32_PLIC_ENABLE, 1u << input_source);
        mmio_write32(plic + RV32_PLIC_THRESHOLD, 0);
    }
    set_timer(mtime() + tick);
    csr_write(CSR_MIE, MIP_MEIP | MIP_MTIP);
    csr_write(CSR_MCOUNTEREN, 7u); /* O5: user mode may read cycle, time and instret */
    csr_write(CSR_SCOUNTEREN, 7u); /* and, since the hart has S-mode (issue #20), scounteren too */

    idle_frame.x[2] = (uint32_t)(uintptr_t)&idle_stack[64];
    idle_frame.pc = (uint32_t)(uintptr_t)kernel_idle;
    idle_frame.mstatus = MSTATUS_MPP_M | MSTATUS_MPIE;
    if (!spawn("sh", "", 0)) {
        panic("no shell");
    }
    kernel_resume(schedule());
}
