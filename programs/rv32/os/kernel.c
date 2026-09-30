/* The kernel (Track 2, O2; docs/rv32-os.md).
 *
 * It boots from the device tree it is given in a1, loads programs from the RAM
 * disk bundled into its image into fixed slots of 128 KiB (a program may span
 * several; sys.h), and serves their
 * system calls (sys.h). Every context, each process and the idle loop, has a
 * frame; kentry.S saves the running one on every trap and resumes whichever
 * kernel_trap returns. The kernel runs with interrupts off and never traps
 * itself: a fault in the kernel is a double fault and stops the machine.
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
    uint32_t load, entry, file_size, memory_size, offset, flags, span;
};
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
static uint32_t protected_pid;                  /* O5: the process PMP is set up for */
static char model[48];
static uint32_t tick = KERNEL_TICK; /* O4: 100 µs where the tree gives a timebase (QEMU), else KERNEL_TICK */

static uint32_t keys[KEY_BUFFER];
static uint32_t key_head, key_count, keys_dropped;

void *memcpy(void *dst, const void *src, size_t n)
{
    uint8_t *d = dst;
    const uint8_t *s = src;
    while (n--) {
        *d++ = *s++;
    }
    return dst;
}

void *memset(void *dst, int value, size_t n)
{
    uint8_t *d = dst;
    while (n--) {
        *d++ = (uint8_t)value;
    }
    return dst;
}

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

static void kputdec(uint32_t value)
{
    char text[11];
    int i = 10;
    text[i] = 0;
    do {
        text[--i] = (char)('0' + value % 10u);
        value /= 10u;
    } while (value);
    kputs(text + i);
}

static void kputhex(uint32_t value)
{
    for (int shift = 28; shift >= 0; shift -= 4) {
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
    uint32_t hi, lo;
    do {
        hi = mmio_read32(clint + RV32_CLINT_MTIME + 4);
        lo = mmio_read32(clint + RV32_CLINT_MTIME);
    } while (hi != mmio_read32(clint + RV32_CLINT_MTIME + 4));
    return (uint64_t)hi << 32 | lo;
}

static void set_timer(uint64_t when)
{
    mmio_write32(clint + RV32_CLINT_MTIMECMP, 0xffffffffu);
    mmio_write32(clint + RV32_CLINT_MTIMECMP + 4, (uint32_t)(when >> 32));
    mmio_write32(clint + RV32_CLINT_MTIMECMP, (uint32_t)when);
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

static uint32_t close_file(struct open_file *o)
{
    uint32_t mode = o->mode;
    o->mode = 0;
    return mode == O_WRITE && !fs_flush() ? SYS_ERROR : 0; /* a written file's size reaches the disk */
}

static void finish(struct proc *p, uint32_t code)
{
    for (uint32_t i = 0; i < OPEN_FILES; i++) {
        (void)close_file(&p->files[i]);
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

/* The next ready process after `after` in table order, round robin, or 0. */
static struct proc *next_ready(const struct proc *after)
{
    uint32_t start = after ? (uint32_t)(after - procs) + 1 : 0;
    for (uint32_t k = 0; k < MAX_PROCS; k++) {
        struct proc *p = &procs[(start + k) % MAX_PROCS];
        if (p->state == READY) {
            return p;
        }
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
    protect(current);
    return &current->f;
}

/* ---- System calls ---- */

static uint32_t engines_busy(void)
{
    return (gpu && (mmio_read32(gpu + GPU_STATUS) & GPU_BUSY)) || (g3d && (mmio_read32(g3d + G3D_STATUS) & G3D_BUSY));
}

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
    case SYS_WRITE:
        if (a0 >= 3 && a0 < 3 + OPEN_FILES && p->files[a0 - 3].mode == O_WRITE && user_range(p, a1, a2)) {
            struct open_file *o = &p->files[a0 - 3];
            result = fs_write(o->file, o->position, (const uint8_t *)(uintptr_t)a1, a2);
            o->position += result;
        } else if ((a0 == 1 || a0 == 2) && user_range(p, a1, a2)) {
            for (uint32_t i = 0; i < a2; i++) {
                kputc(*(const char *)(uintptr_t)(a1 + i));
            }
            result = a2;
        }
        break;
    case SYS_READ:
        if (a0 >= 3 && a0 < 3 + OPEN_FILES && p->files[a0 - 3].mode == O_READ && user_range(p, a1, a2)) {
            struct open_file *o = &p->files[a0 - 3];
            result = fs_read(o->file, o->position, (uint8_t *)(uintptr_t)a1, a2);
            o->position += result;
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
         * for a program that drives the engines, wait until they finish. */
        if (display && !((p->flags & PROGRAM_ACCELERATORS) && engines_busy())) {
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
            uint32_t d = 10, v = q->pid;
            digits[d] = 0;
            do {
                digits[--d] = (char)('0' + v % 10u);
                v /= 10u;
            } while (v);
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
            int file = fs_open(name, (a1 & O_CREATE) && mode == O_WRITE);
            for (uint32_t i = 0; file >= 0 && i < OPEN_FILES; i++) {
                if (!p->files[i].mode) {
                    p->files[i] = (struct open_file){mode, file, 0};
                    if (mode == O_WRITE) {
                        fs_truncate(file); /* writing replaces the contents */
                    }
                    result = 3 + i;
                    break;
                }
            }
        }
        break;
    }
    case SYS_CLOSE:
        if (a0 >= 3 && a0 < 3 + OPEN_FILES && p->files[a0 - 3].mode) {
            result = close_file(&p->files[a0 - 3]);
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
    kputhex(p->f.pc);
    kputs(" tval ");
    kputhex(tval);
    kputc('\n');
    finish(p, 128u + cause);
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
    } else {
        kill(p, cause, tval);
    }
    return schedule(); /* O2 has no preemption: a running process keeps the machine */
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
    kputs(disk ? " disk" : "");
    uint32_t count;
    (void)programs(&count);
    kputs("\nkernel: ");
    kputdec(count);
    kputs(" programs\n");
    if (disk) {
        uint32_t sectors = virtio_init(disk);
        int mounted = sectors && fs_mount();
        kputs("kernel: disk ");
        kputdec(sectors);
        kputs(mounted ? " sectors, tfs\n" : " sectors, no file system\n");
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

    idle_frame.x[2] = (uint32_t)(uintptr_t)&idle_stack[64];
    idle_frame.pc = (uint32_t)(uintptr_t)kernel_idle;
    idle_frame.mstatus = MSTATUS_MPP_M | MSTATUS_MPIE;
    if (!spawn("sh", "", 0)) {
        panic("no shell");
    }
    kernel_resume(schedule());
}
