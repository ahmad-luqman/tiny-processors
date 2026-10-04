/* rv32emu_core: the RV32 machine as a library (docs/rv32.md, docs/rv32-emulator.md).
 *
 * One machine struct, the memory map with its devices, the instruction step,
 * the input script, and the halt report. Two programs link it: rv32emu, the
 * headless emulator, and rv32win, the native window (M6). The core never
 * touches a window or a clock: a present is a change of `frames` that
 * emu_run_until reports, and host keys enter through emu_queue_event at the
 * same point scripted events do.
 *
 * Build: make build-rv32-emu (links SoftFloat, SIMD4 and the G1 device)
 */
#ifndef RV32EMU_CORE_H
#define RV32EMU_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include "rv32_simd4.h"
#include "rv32_gpu.h"
#include "rv32_g3d.h"

/* Machine contract constants; keep in step with programs/rv32/board.h;
 * the graphics contract is imported from programs/rv32/gpu.h. */
#define RAM_BASE 0x80000000u
#define RAM_SIZE 0x00800000u
#define CONSOLE_BASE 0x10000000u
#define CONSOLE_TX 0x0u      /* write: transmit; read: RBR, the next received byte (O2) */
#define CONSOLE_STATUS 0x5u
#define CONSOLE_TX_READY 0x20u
#define CONSOLE_RX_READY 0x01u /* LSR.DR: a received byte is waiting (O2) */
#define DONE_ADDR 0x00100000u
#define DONE_PASS 0x5555u
#define DONE_FAIL 0x3333u
#define DONE_RESET 0x7777u
#define CLINT_BASE 0x02000000u
#define CLINT_SIZE 0x10000u
#define CLINT_MSIP 0x0u
#define CLINT_MTIMECMP 0x4000u
#define CLINT_MTIME 0xbff8u
#define BOOT_HART 0u      /* a0 at reset: the hart id */
#define PLIC_BASE 0x0c000000u
#define PLIC_SIZE 0x600000u
#define PLIC_PENDING 0x1000u
#define PLIC_ENABLE 0x2000u
#define PLIC_THRESHOLD 0x200000u
#define PLIC_CLAIM 0x200004u
#define PLIC_SOURCES 32u         /* sources 1..31; 0 means "none" */
#define PLIC_SOURCE_INPUT 12u    /* the input queue: pending while COUNT is nonzero */
#define PLIC_SOURCE_VIRTIO 1u    /* virtio-blk (O3): pending while InterruptStatus is nonzero */
#define PLIC_WIRED ((1u << PLIC_SOURCE_INPUT) | (1u << PLIC_SOURCE_VIRTIO))
#define VIRTIO_BASE 0x10001000u  /* virt's first virtio-mmio slot (O3) */
#define VIRTIO_SIZE 0x200u
#define VIRTIO_DISK_SIZE 0x20000u /* 128 KiB, the RTL's DISK_WORDS: a disk file must be this size */
#define INPUT_BASE 0x11001000u
#define INPUT_EVENT 0x0u
#define INPUT_COUNT 0x4u
#define INPUT_KEYS 0x8u
#define INPUT_QUEUE 16u
#define EVENT_VALID 0x80000000u
#define EVENT_PRESS 0x100u
#define DISPLAY_BASE 0x11002000u
#define DISPLAY_PRESENT 0x0u
#define DISPLAY_FRAMES 0x4u
#define DISPLAY_WIDTH 0x8u
#define DISPLAY_HEIGHT 0xCu
/* The DMA window (issue #20): the RAM G1 and G2 may reach, [START, END), in its own page so the
 * kernel can keep it from the programs it grants the engines. */
#define DMA_WINDOW_BASE 0x1100a000u
#define DMA_WINDOW_START 0x0u
#define DMA_WINDOW_END 0x4u
#define FB_BASE 0x12000000u
#define FB_COLUMNS 320u
#define FB_ROWS 240u
#define FB_SIZE (FB_COLUMNS * FB_ROWS)

/* virtio-blk (O3, docs/rv32.md "virtio-blk"): the registers of one virtio-mmio version 2 block
 * device and its disk, held in memory and written through to the file it came from. */
typedef struct {
    uint8_t status;
    bool features_sel, queue_sel_zero, queue_ready, interrupt;
    uint32_t queue_num, desc_lo, desc_hi, driver_lo, driver_hi, device_lo, device_hi;
    uint16_t last_avail, used_idx;
    uint8_t disk[VIRTIO_DISK_SIZE];
    FILE *file;       /* --disk: written through on every OUT request, or NULL */
    bool write_error;
} virtio_blk;

/* Why the run ended. HALT_STOPPED is the host's doing (the window was closed, or a
 * debugger killed the run or hung up); a plain headless run never produces it. */
enum halt { RUNNING, HALT_DONE, HALT_DOUBLE_FAULT, HALT_LIMIT, HALT_STOPPED };

/* One line of the input script: the event and the frame it arrives at. */
typedef struct {
    uint32_t frame;
    uint32_t event;
} scripted_event;

/* Process exit status when the run did not end with a pass/fail done word. */
#define EXIT_EMULATOR_ERROR 2

typedef struct {
    simd_device simd;
    gpu_device gpu;
    g3d_device g3d;
    uint32_t dma_start, dma_end; /* the DMA window; all of RAM at reset */
    uint32_t x[32], f[32];
    uint8_t fcsr;
    uint32_t pc;
    uint32_t mtvec, mepc, mcause, mtval;
    uint32_t mstatus;  /* the writable bits (SIE, MIE, SPIE, MPIE, SPP, MPRV, SUM, MXR, TVM, TW, TSR, and FS since
                        * issue #33); SD is derived from FS, MPP is mpp (O5) */
    uint32_t mie, mscratch;
    uint8_t priv, mpp;   /* O5: the privilege mode (3 machine, 1 supervisor since issue #20, 0 user) and mstatus.MPP */
    /* S-mode and Sv32 (issue #20): delegation, the supervisor's trap CSRs, and address translation. */
    uint32_t medeleg, mideleg, mip_soft; /* mip_soft: SSIP, STIP and SEIP, which software raises */
    uint32_t stvec, sscratch, sepc, scause, stval, satp, scounteren;
    uint32_t mcounteren; /* O5: CY, TM, IR: the counters user mode may read */
    uint8_t pmpcfg[8];   /* O5: eight PMP entries */
    uint32_t pmpaddr[8];
    uint8_t plic_priority[PLIC_SOURCES]; /* the PLIC (O1): priorities of the wired sources */
    uint32_t plic_enable, plic_claimed;  /* context 0's enables; sources claimed and not completed */
    uint32_t plic_pending;               /* the gateways' latched requests */
    uint8_t plic_threshold;
    uint64_t interrupts; /* interrupts taken */
    virtio_blk *virtio;  /* the block device (O3), allocated by emu_alloc */
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
    uint64_t mtime_offset; /* mtime = steps + mtime_offset; a write sets the offset */
    uint64_t mtimecmp;     /* CLINT: mip.MTIP is mtime >= mtimecmp, mip.MSIP is msip (O1) */
    uint32_t msip;
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
    size_t dropped;         /* events the full queue refused; a passing run is rejected unless allowed */
    FILE *record;           /* every event offered to the queue as a script line, or NULL */
    uint8_t *console_in;    /* console input (O2): bytes the guest reads from RBR, all there from reset */
    size_t console_in_len, console_in_next;
    bool console_stdin;     /* --console-input -: bytes arrive from stdin as the host has them */
    /* Effects of the current step, for the trace line. */
    int wr_reg, wr_freg;
    bool wr_fcsr;
    uint32_t wr_value;
    bool mem_read, mem_write;
    uint32_t mem_addr, mem_value;
    int mem_width;
} machine;

/* Why emu_run_until returned. */
typedef enum { EMU_STOP_HALTED, EMU_STOP_PRESENTED, EMU_STOP_BUDGET } emu_stop;

/* The name every diagnostic line starts with ("rv32emu" unless a program sets it). */
extern const char *emu_prog;

/* Lifetime: zero the machine and set the default instruction limit; allocate RAM and the
 * framebuffer (false and a message when the host refuses); load a flat image at `base`
 * (false and a message on any problem; `*loaded` is the byte count); free everything. */
void emu_init(machine *m);
bool emu_alloc(machine *m);
bool emu_load_image(machine *m, const char *path, uint32_t base, size_t *loaded);
void emu_free(machine *m);

/* Input: parse the script (exits with a message on error), deliver every scripted event whose
 * frame has been reached, or offer one event to the queue now (recorded, then queued or dropped). */
void emu_read_input_script(machine *m, const char *path);
/* Console input (O2): every byte of `path` is received before the first instruction; "-" instead
 * reads stdin as it arrives, for interactive use. Exits with a message on error. */
void emu_read_console_input(machine *m, const char *path);
/* The disk (O3): a file of exactly VIRTIO_DISK_SIZE bytes, read now and written through on every
 * OUT request. Without one the disk is that many zero bytes that last only for the run. Exits with
 * a message on error. */
void emu_open_disk(machine *m, const char *path);
void emu_deliver_events(machine *m);
void emu_queue_event(machine *m, uint32_t frame, uint32_t event);
int emu_key_code(const char *text);   /* a board.h key name (any case) or 0..31; -1 otherwise */
const char *emu_key_name(int code);   /* the board.h name of a code, or NULL for an unnamed one */

/* Execution: run until the machine halts, a present raised `frames`, or `budget` more
 * instructions have executed. The instruction limit is checked before the budget. */
emu_stop emu_run_until(machine *m, uint64_t budget);

/* Display helpers shared by the PPM writer and the window: the checkpoint hash over the
 * framebuffer, and the fixed RGB332 mapping of one pixel. */
uint32_t emu_frame_hash(const uint8_t *pixels);
void emu_rgb332(uint8_t pixel, uint8_t rgb[3]);

/* Reporting. emu_finish_outputs flushes the console and closes whichever of the trace, checkpoints,
 * and record streams are open (a path is only for the message), returning false when any output is
 * incomplete.
 * emu_report_halt prints the undelivered-events line and the halt line to stderr and returns
 * the status the halt alone implies. emu_exit_status folds in incomplete outputs and lost
 * scripted events, printing why, and returns the process status. */
const char *emu_halt_name(enum halt halt);
void emu_dump_state(const machine *m, FILE *out);
bool emu_finish_outputs(machine *m, const char *trace_path, const char *checkpoints_path, const char *record_path);
int emu_report_halt(const machine *m, size_t loaded);
int emu_exit_status(const machine *m, int status, bool outputs_ok, bool allow_lost_events);

/* Command-line helpers: strict unsigned parsing (exits with a message), refusing two names
 * for one file, and closing an output while reporting a write error. */
uint64_t emu_parse_u64(const char *text, uint64_t max, const char *what);
uint32_t emu_parse_u32(const char *text, const char *what);
void emu_require_distinct(const char *path, const char *what, const char *other_path, const char *other);
void emu_require_outside(const char *path, const char *what, const char *directory, const char *other);
FILE *emu_open_output(const char *path, const char *what);
void emu_require_distinct_streams(FILE *a, const char *a_path, const char *a_what, FILE *b, const char *b_path, const char *b_what);
bool emu_close_output(FILE *stream, const char *path);

/* Debugger access (docs/rv32-gdb.md): copy n bytes of RAM or the framebuffer without any device
 * side effect; false when any byte lies outside RAM and outside the framebuffer (device windows
 * included). CSRs by number with the CSR instructions' WARL masks: a read is false for a number
 * that does not exist, a write also for a read-only one (the Zicntr counters). */
bool emu_debug_read(const machine *m, uint32_t addr, uint8_t *out, size_t n);
bool emu_debug_write(machine *m, uint32_t addr, const uint8_t *in, size_t n);
bool emu_csr_read(const machine *m, uint32_t number, uint32_t *value);
bool emu_csr_write(machine *m, uint32_t number, uint32_t value);

#endif
