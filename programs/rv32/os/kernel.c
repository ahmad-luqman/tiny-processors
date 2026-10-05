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
 * Since issue #25 every process also runs under Sv32 paging: its own page
 * table maps its slots, the framebuffer and, for a program that drives them,
 * the accelerators' windows, each at its own physical address; nothing else is
 * mapped, so a stray access is a page fault before PMP is asked. PMP stays as
 * the backstop. The kernel stays in machine mode, where nothing is translated,
 * and switches satp with PMP. The mapping is the identity, so the engines' DMA
 * addresses, the DMA window and the transcripts' addresses mean what they did.
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
#define ARGS_MAX OS_ARGS_MAX
#define MSTATUS_MPP_M 0x1800u  /* the idle loop; a process's MPP is 0, user mode (O5) */
#define MSTATUS_FS 0x6000u       /* issue #33: the FS field (14:13), Dirty when all set */
#define MSTATUS_FS_CLEAN 0x4000u
#define CAUSE_ILLEGAL 2u
#define CAUSE_ECALL_M 11u
#define CAUSE_ECALL_U 8u
#define CAUSE_LOAD_PAGE 13u  /* issue #25: a load, or a store, the page table refuses */
#define CAUSE_STORE_PAGE 15u
#define RAMDISK_MAGIC 0x4b534452u /* "RDSK" */
#define OPEN_FILES OS_OPEN_FILES /* descriptors OS_FIRST_FILE on */
_Static_assert(FS_NAME == OS_FILE_NAME, "sys.h and fs.h agree on a file name's length");

/* Sv32 (issue #25): a page table entry's bits, and satp's mode. */
#define PAGE_SIZE 4096u
#define PTE_V 0x01u
#define PTE_R 0x02u
#define PTE_W 0x04u
#define PTE_X 0x08u
#define PTE_U 0x10u
#define PTE_A 0x40u /* every leaf has A and D: our hart never sets them (Svade), QEMU need not */
#define PTE_D 0x80u
#define SATP_SV32 0x80000000u
#define PAGE_TABLES 7u /* per process table entry: the root and a level-0 table per 4 MiB touched */

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
/* A power of two, so indexing the process table is a shift: RV32I has no multiply, and at 260
 * bytes every procs[i] in the scheduler's loops became a call (Track 3 measured 59% more
 * steps in the console session: 2.86 M against 1.80 M). A new per-process field belongs in struct address_space. */
_Static_assert(sizeof(struct proc) == 256, "struct proc is 256 bytes");

/* A RAM disk entry (tools/rv32_ramdisk.py). */
struct program {
    char name[24];
    uint32_t load, entry, file_size, memory_size, offset, flags, span; /* span in bytes, whole slots */
    uint32_t stack; /* the top of the span the stack takes, whole pages, the lowest a guard (Track 3) */
};
_Static_assert(sizeof(struct program) == 56, "tools/rv32_ramdisk.py's <24s8I entry");
#define PROGRAM_ACCELERATORS 1u /* it drives SIMD4, G1 and G2 itself */

extern void trap_vector(void);
extern void kernel_idle(void);
extern _Noreturn void kernel_resume(struct frame *f);
extern const uint8_t ramdisk[], ramdisk_end[];

static struct proc procs[MAX_PROCS];
static struct proc *current; /* 0 while the idle loop runs */
static struct proc *fpu_owner; /* issue #33: whose floating state the FPU holds, or 0 */
static struct frame idle_frame;
static uint32_t idle_stack[64];
static uint32_t next_pid = 1, exits, exit_sum;

static uint32_t console, done_register, clint, plic;
static uint32_t input, input_source, display, framebuffer, framebuffer_size, gpu, g3d, disk;
static uint32_t accelerators, accelerators_end; /* O5: the window PMP grants a PROGRAM_ACCELERATORS program */
/* The engines' register and memory windows. O5's PMP region spans them all; since issue #25 the
 * page table of a PROGRAM_ACCELERATORS program maps each one, and nothing between them. */
static const struct { const char *compatible; uint32_t index; } engines[] = {
    {"tiny-processors,simd4", 0}, {"tiny-processors,simd4", 1}, {"tiny-processors,simd4", 2},
    {"tiny-processors,g1", 0},    {"tiny-processors,g2", 0},
};
static struct { uint32_t base, size; } engine_windows[sizeof engines / sizeof engines[0]];
static uint32_t engine_window_count;
static uint32_t dma_window;                     /* issue #20: the engines' DMA bound, 0 where there is none (QEMU) */
static uint32_t protected_pid;                  /* O5: the process PMP is set up for */
static char model[48];
static uint32_t tick = KERNEL_TICK; /* O4: 100 µs where the tree gives a timebase (QEMU), else KERNEL_TICK */

static int faulted; /* kernel_fault ran: halt must not touch the disk again */

static uint32_t keys[KEY_BUFFER];
static uint32_t key_head, key_count, keys_dropped;

/* Issue #25: each process table entry's page tables, a root and its level-0 tables. kernel.ld
 * places them in a page-aligned section of their own, not zeroed at boot (map_process() clears an
 * entry's root and map() each level-0 table, when first taken), whose bounds PMP entries 6 and 7
 * let the hart's walks read. */
static uint32_t page_tables[MAX_PROCS][PAGE_TABLES][PAGE_SIZE / 4]
    __attribute__((section(".pagetables"), aligned(PAGE_SIZE)));
_Static_assert(sizeof page_tables[0][0] == PAGE_SIZE, "one table per page");
extern uint8_t __pagetables_start[], __pagetables_end[];

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

/* Reg entry `index` of the node `compatible` names: 1 with its base and size, 0 when the tree has
 * no such node or entry. Any other answer is a malformed tree, and the kernel stops: a device it
 * took for absent would be left out of PMP and the page tables, and fail later as something
 * else. */
static int find_reg(const fdt *t, const char *compatible, uint32_t index, uint32_t *base, uint32_t *size)
{
    fdt_status status = fdt_find(t, "compatible", compatible, index, base, size);
    if (status == FDT_NOT_FOUND) {
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
    return 1;
}

static uint32_t find(const fdt *t, const char *compatible, uint32_t index, int required)
{
    uint32_t base = 0, size;
    if (!find_reg(t, compatible, index, &base, &size)) {
        if (required) {
            kputs("kernel: device tree: no ");
            kputs(compatible);
            kputc('\n');
            panic("device tree");
        }
        return 0;
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
    for (uint32_t w = 0; w < sizeof engines / sizeof engines[0]; w++) {
        uint32_t base, size;
        if (find_reg(&t, engines[w].compatible, engines[w].index, &base, &size)) {
            engine_windows[engine_window_count].base = base;
            engine_windows[engine_window_count++].size = size;
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
    if (!find_reg(&t, "tiny-processors,dma-window", 0, &dma_window, &window_size)) {
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
static uint32_t guard_of(const struct proc *p);

/* Whether [address, address + len) lies in `p`'s slots and off its stack's guard page: the
 * kernel copies to and from user memory by physical address, in machine mode, so the page table
 * that keeps the program out of the guard would not stop a system call (Track 3). */
static int user_range(const struct proc *p, uint32_t address, uint32_t len)
{
    if (!(address >= p->base && len <= p->span && address - p->base <= p->span - len)) {
        return 0;
    }
    uint32_t guard = guard_of(p); /* inside the slots, so address + len cannot wrap */
    return !(len && address < guard + OS_GUARD_SIZE && address + len > guard);
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

/* What tools/rv32_ramdisk.py promises of an entry in a RAM disk of `size` bytes: inside the disk
 * and its slots, the file no larger than the memory it loads into, the entry point inside the file. */
static int entry_ok(const struct program *e, uint32_t size)
{
    uint32_t slots_end = OS_SLOT_BASE + OS_SLOTS * OS_SLOT_SIZE;
    return !(e->name[sizeof e->name - 1] || e->offset > size || e->file_size > size - e->offset ||
             e->file_size > e->memory_size || e->span == 0 || e->span % OS_SLOT_SIZE ||
             e->span > OS_SLOTS * OS_SLOT_SIZE || e->load < OS_SLOT_BASE || (e->load - OS_SLOT_BASE) % OS_SLOT_SIZE ||
             e->load > slots_end - e->span || e->stack % PAGE_SIZE || e->stack < 2 * PAGE_SIZE ||
             e->stack >= e->span || e->memory_size > e->span - e->stack ||
             e->entry < e->load || e->entry - e->load >= e->file_size);
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
        if (!entry_ok(&list[i], size)) {
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

/* Issue #33: a process's floating state while another holds the FPU; kfpu.S knows the layout. */
struct fstate {
    uint32_t f[32];
    uint32_t fcsr;
};
_Static_assert(offsetof(struct fstate, fcsr) == 128 && sizeof(struct fstate) == 132, "kfpu.S offsets");
extern void fpu_save(struct fstate *s);
extern void fpu_load(const struct fstate *s);

/* Issue #25: what each process table entry's page tables map. The next process there starts from
 * the same tables with the last one's leaves cleared, rather than from 16 KiB cleared again; a
 * table is cleared once, when it is first taken. The layout is kept here, not read from the
 * proc: spawn() has overwritten the proc's base, span and flags by the time the old leaves are
 * cleared. Issue #33 keeps each entry's floating state here too, being per process and, unlike
 * struct proc, free to grow. */
static struct address_space {
    uint32_t tables; /* the entry's tables in use, the root first; 0 before its first process */
    uint32_t base, span, guard; /* guard: the stack's lowest page, never mapped (Track 3) */
    uint32_t drives_engines; /* a PROGRAM_ACCELERATORS program's: the engines' windows mapped */
    struct fstate fp; /* issue #33: the floating state, when the FPU is not holding it */
} spaces[MAX_PROCS];

static void clear_table(uint32_t *table)
{
    for (uint32_t i = 0; i < PAGE_SIZE / 4; i++) {
        ((volatile uint32_t *)table)[i] = 0; /* else the compiler makes it a byte-wise memset */
    }
}

/* Map [start, end) at the same physical addresses with leaf rights `rights`, or with 0 clear the
 * leaves there. A level-0 table is taken the first time a 4 MiB region is mapped; the root must
 * have been taken already (map_process()), or it would be handed out as a level-0 table. */
static void map(uint32_t entry, uint32_t start, uint32_t end, uint32_t rights)
{
    uint32_t (*pt)[PAGE_SIZE / 4] = page_tables[entry];
    if (!spaces[entry].tables) {
        panic("map before the root");
    }
    for (uint32_t page = start & ~(PAGE_SIZE - 1u); page < end; page += PAGE_SIZE) {
        uint32_t *pointer = &pt[0][page >> 22];
        if (!(*pointer & PTE_V)) {
            if (!rights) {
                continue;
            }
            if (spaces[entry].tables == PAGE_TABLES) {
                panic("page tables full"); /* check_page_table_budget() rules this out at boot */
            }
            uint32_t *table = pt[spaces[entry].tables++];
            clear_table(table);
            *pointer = (uint32_t)(uintptr_t)table >> 12 << 10 | PTE_V;
        }
        uint32_t *leaf = (uint32_t *)(uintptr_t)(*pointer >> 10 << 12);
        leaf[page >> 12 & 1023u] = rights ? page >> 12 << 10 | rights | PTE_V | PTE_U | PTE_A | PTE_D : 0;
    }
}

/* The address space `spaces[entry]` describes, with `rights` on the slots (R, W and X to map it, 0
 * to clear it) and the same less X on the framebuffer and, for a program that drives them, the
 * accelerators' windows. Nothing else is mapped. */
static void lay_out(uint32_t entry, uint32_t rights)
{
    const struct address_space *space = &spaces[entry];
    uint32_t data = rights & ~PTE_X;
    map(entry, space->base, space->guard, rights);
    map(entry, space->guard + OS_GUARD_SIZE, space->base + space->span, rights);
    if (framebuffer) {
        map(entry, framebuffer, framebuffer + framebuffer_size, data);
    }
    if (space->drives_engines) {
        for (uint32_t w = 0; w < engine_window_count; w++) {
            map(entry, engine_windows[w].base, engine_windows[w].base + engine_windows[w].size, data);
        }
    }
}

/* The guard page below `p`'s stack (Track 3). */
static uint32_t guard_of(const struct proc *p)
{
    return spaces[p - procs].guard;
}

/* The page tables of the process about to start in `p`'s entry, whose stack is the top `stack`
 * bytes of its span. */
static void map_process(const struct proc *p, uint32_t stack)
{
    uint32_t entry = (uint32_t)(p - procs);
    struct address_space *space = &spaces[entry];
    if (space->tables) {
        lay_out(entry, 0); /* the previous process's leaves */
    } else {
        clear_table(page_tables[entry][0]);
        space->tables = 1;
    }
    space->base = p->base;
    space->span = p->span;
    space->guard = p->base + p->span - stack;
    space->drives_engines = !!(p->flags & PROGRAM_ACCELERATORS);
    lay_out(entry, PTE_R | PTE_W | PTE_X);
}

/* Add the 4 MiB regions [start, end) touches to `regions`: `*count` so far, at most `max`. */
static void add_regions(uint32_t *regions, uint32_t *count, uint32_t max, uint32_t start, uint32_t end)
{
    for (uint32_t region = start >> 22; region <= (end - 1u) >> 22; region++) {
        uint32_t k = 0;
        while (k < *count && regions[k] != region) {
            k++;
        }
        if (k == *count) {
            if (*count == max) {
                panic("page tables: more 4 MiB regions than an entry has tables");
            }
            regions[(*count)++] = region;
        }
    }
}

/* Issue #25: an entry has a root and PAGE_TABLES - 1 level-0 tables, one per 4 MiB region its
 * process touches. Checked once at boot against the most any process could touch (every slot, the
 * framebuffer and every engine window), so a tree that needs more stops the kernel here and not
 * at the first spawn of the program that would. */
static void check_page_table_budget(void)
{
    uint32_t regions[PAGE_TABLES - 1], count = 0;
    add_regions(regions, &count, PAGE_TABLES - 1, OS_SLOT_BASE, OS_SLOT_BASE + OS_SLOTS * OS_SLOT_SIZE);
    if (framebuffer) {
        add_regions(regions, &count, PAGE_TABLES - 1, framebuffer, framebuffer + framebuffer_size);
    }
    for (uint32_t w = 0; w < engine_window_count; w++) {
        add_regions(regions, &count, PAGE_TABLES - 1, engine_windows[w].base,
                    engine_windows[w].base + engine_windows[w].size);
    }
}

/* Load a program into its slots and make it ready; returns the process or 0. */
static int may_open(int file, uint32_t mode);

/* Issue #35: a program too large for the kernel's image lives on the disk, as a tfs file that is
 * a RAM disk of one program (tools/rv32_ramdisk.py writes both) named as the program is. Its entry
 * is read into `e` and checked as the boot check checks the RAM disk's, against the file's size;
 * returns the file, or -1 when there is no such program, it is malformed or it is being written. */
static int disk_program(const char *name, struct program *e)
{
    uint32_t header[4];
    int file = fs_open(name, 0);
    if (file < 0 || !may_open(file, O_READ)) {
        return -1;
    }
    uint32_t size = fs_size(file);
    if (fs_read(file, 0, (uint8_t *)header, sizeof header) != sizeof header || header[0] != RAMDISK_MAGIC ||
        header[1] != 1 || fs_read(file, sizeof header, (uint8_t *)e, sizeof *e) != sizeof *e ||
        !entry_ok(e, size) || !fdt_same(e->name, name)) {
        return -1;
    }
    return file;
}

static struct proc *spawn(const char *name, const char *args, uint32_t parent)
{
    static struct program on_disk;
    const struct program *program = program_named(name);
    int file = -1;
    if (!program) {
        file = disk_program(name, &on_disk);
        if (file < 0) {
            return 0;
        }
        program = &on_disk;
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
    uint8_t *base = (uint8_t *)(uintptr_t)program->load;
    if (file < 0) {
        memcpy(base, ramdisk + program->offset, program->file_size);
    } else if (fs_read(file, program->offset, base, program->file_size) != program->file_size) {
        return 0;
    }
    memset(p, 0, sizeof *p);
    p->base = program->load;
    p->span = program->span;
    p->flags = program->flags;
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
    p->f.mstatus = MSTATUS_MPIE; /* MPP 0: mret enters user mode (O5); FS Off (issue #33) */
    memset(&spaces[p - procs].fp, 0, sizeof spaces[p - procs].fp); /* its floating state: zeros, as at reset */
    p->brk = (program->load + program->memory_size + 15u) & ~15u;
    p->pid = next_pid++;
    p->parent = parent;
    p->state = READY;
    for (i = 0; program->name[i] && i + 1 < sizeof p->name; i++) {
        p->name[i] = program->name[i];
    }
    map_process(p, program->stack);
    return p;
}

/* Descriptor `fd` of `p` when it is open in `mode` (0: either), else 0. */
static struct open_file *open_file_of(struct proc *p, uint32_t fd, uint32_t mode)
{
    if (fd < OS_FIRST_FILE || fd >= OS_FIRST_FILE + OPEN_FILES || !p->files[fd - OS_FIRST_FILE].mode ||
        (mode && p->files[fd - OS_FIRST_FILE].mode != mode)) {
        return 0;
    }
    return &p->files[fd - OS_FIRST_FILE];
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
    if (fpu_owner == p) {
        fpu_owner = 0; /* issue #33: its floating state goes with it, unsaved */
    }
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
 * framebuffer (RW), 4-5 the accelerators (RW) for a program that drives them. A user access
 * nothing matches faults; the idle loop runs in machine mode and ignores it. Since issue #25 its
 * page table is the first check and PMP the backstop; entries 6-7 let the hart's page-table walks,
 * which are supervisor reads, read the tables (and only read them). Unlocked PMP cannot tell
 * supervisor from user mode, so that entry lets user mode read the tables too: only the page
 * tables, which never map them, keep a program out. */
static void protect(const struct proc *p)
{
    if (p->pid == protected_pid) {
        return;
    }
    protected_pid = p->pid;
    uint32_t tor_rwx = PMP_TOR | PMP_R | PMP_W | PMP_X, tor_rw = PMP_TOR | PMP_R | PMP_W;
    uint32_t tor_r = PMP_TOR | PMP_R;
    csr_write(CSR_PMPADDR0, p->base >> 2);
    csr_write(CSR_PMPADDR1, (p->base + p->span) >> 2);
    csr_write(CSR_PMPADDR2, framebuffer >> 2);
    csr_write(CSR_PMPADDR3, (framebuffer + framebuffer_size) >> 2);
    csr_write(CSR_PMPADDR4, accelerators >> 2);
    csr_write(CSR_PMPADDR5, accelerators_end >> 2);
    csr_write(CSR_PMPADDR6, (uint32_t)(uintptr_t)__pagetables_start >> 2);
    csr_write(CSR_PMPADDR7, (uint32_t)(uintptr_t)__pagetables_end >> 2);
    csr_write(CSR_PMPCFG0, tor_rwx << 8 | (framebuffer ? tor_rw << 24 : 0u));
    uint32_t engines_rw = (p->flags & PROGRAM_ACCELERATORS) && accelerators_end ? tor_rw : 0u;
    csr_write(CSR_PMPCFG1, engines_rw << 8 | tor_r << 24);
    /* Its address space, after PMP, and a fence for two reasons. The ASID is 0 bits wide, so every
     * process's translations look alike to a TLB, and map_process() rewrites an entry's leaves for
     * a new process: a hart whose satp write flushed nothing could keep the old process's. And the
     * privileged spec asks for one after PMP changes over the page tables, as entries 0-5 just did.
     * Our RTL empties its TLB on a satp write anyway, and the emulator has none. */
    csr_write(CSR_SATP, SATP_SV32 | (uint32_t)(uintptr_t)page_tables[p - procs][0] >> 12);
    __asm__ volatile("sfence.vma" ::: "memory");
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
/* Move an open file's position by `offset` from the start, the current position or the end;
 * the new position, which must lie between 0 and the file's size, or SYS_ERROR (Track 3). */
static uint32_t seek_file(struct open_file *o, int32_t offset, uint32_t whence)
{
    int64_t to = offset;
    switch (whence) {
    case SEEK_FROM_START:
        break;
    case SEEK_FROM_CURRENT:
        to += o->position;
        break;
    case SEEK_FROM_END:
        to += fs_size(o->file);
        break;
    default:
        return SYS_ERROR;
    }
    if (to < 0 || to > fs_size(o->file)) {
        return SYS_ERROR;
    }
    o->position = (uint32_t)to;
    return o->position;
}

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
        if (a0 <= guard_of(p) - p->brk) { /* the heap stops below the stack */
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
                result = OS_FIRST_FILE + i;
            }
        }
        break;
    }
    case SYS_SEEK: { /* Track 3: the C library's lseek */
        struct open_file *o = open_file_of(p, a0, 0);
        if (o) {
            result = seek_file(o, (int32_t)a1, a2);
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
    if ((cause == CAUSE_LOAD_PAGE || cause == CAUSE_STORE_PAGE) && tval - guard_of(p) < OS_GUARD_SIZE) {
        kputs(" (stack overflow)"); /* Track 3: a load or store on the guard page below the stack */
    }
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

/* Issue #33: whether `word` is an F instruction (FLW, FSW, the arithmetic opcodes) or an access to
 * fflags, frm or fcsr: what FS Off makes illegal (rv32.v's fs_illegal; tools/rv32_rtl.py's
 * floating_word repeats the test). */
static int floating_instruction(uint32_t word)
{
    uint32_t opcode = word & 0x7fu, funct3 = (word >> 12) & 7u, csr = word >> 20;
    return opcode == 0x07u || opcode == 0x27u || opcode == 0x43u || opcode == 0x47u || opcode == 0x4bu ||
           opcode == 0x4fu || opcode == 0x53u || (opcode == 0x73u && (funct3 & 3u) && csr >= 1u && csr <= 3u);
}

/* Issue #33: the lazy switch. Only the process whose state the FPU holds, fpu_owner, runs with FS on;
 * every other one has FS Off, so its first F instruction traps here as illegal. Its owner gives the
 * FPU up (its state saved only if FS says Dirty: Clean means its saved copy is current, since any
 * write of an f register or fcsr, flags accrued included, makes it Dirty on our hart and on QEMU)
 * and runs with FS Off from now on; this process's state is loaded, FS is Clean, and the instruction
 * runs again. A process that never uses the FPU never comes here, so switching to it costs nothing.
 * Returns 0 for an illegal instruction that is not this case: it is killed as before. */
static int claim_fpu(struct proc *p, uint32_t word)
{
    if ((p->f.mstatus & MSTATUS_FS) || !floating_instruction(word)) {
        return 0;
    }
    struct proc *owner = fpu_owner;
    if (owner) {
        if ((owner->f.mstatus & MSTATUS_FS) == MSTATUS_FS) {
            fpu_save(&spaces[owner - procs].fp);
        }
        owner->f.mstatus &= ~MSTATUS_FS;
    }
    fpu_load(&spaces[p - procs].fp);
    p->f.mstatus |= MSTATUS_FS_CLEAN;
    fpu_owner = p;
    for (uint32_t i = 0; i < MAX_PROCS; i++) { /* only the owner runs with FS on */
        if (alive(&procs[i]) && &procs[i] != p && (procs[i].f.mstatus & MSTATUS_FS)) {
            panic("fpu: a second process with FS on");
        }
    }
    return 1;
}

/* The instruction a trap names: mtval, which our hart and QEMU set to it for an illegal
 * instruction, else read where the process was (its code is mapped where it was linked). */
static uint32_t trapped_instruction(const struct proc *p, uint32_t tval)
{
    if (tval || !user_range(p, p->f.pc, 4)) {
        return tval;
    }
    return *(const uint32_t *)(uintptr_t)p->f.pc;
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
    } else if (cause == CAUSE_ILLEGAL && claim_fpu(p, trapped_instruction(p, tval))) {
        /* issue #33: the FPU is this process's now; the instruction runs again */
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
    check_page_table_budget();
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
