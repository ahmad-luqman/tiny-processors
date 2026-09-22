/* rv32emu_core: the RV32 machine as a library (docs/rv32.md, docs/rv32-emulator.md).
 *
 * One machine struct, the memory map with its devices, the instruction step,
 * the input script, and the halt report. Two programs link it: rv32emu, the
 * headless emulator, and rv32win, the native window (M6). The core never
 * touches a window or a clock: a present is a change of `frames` that
 * emu_run_until reports, and host keys enter through emu_queue_event at the
 * same point scripted events do.
 *
 * Build: make build-rv32-emu (links the pinned host SoftFloat objects)
 */
#ifndef RV32EMU_CORE_H
#define RV32EMU_CORE_H

#include <stdbool.h>
#include <stddef.h>
#include <stdint.h>
#include <stdio.h>
#include "rv32_simd4.h"
#include "rv32_gpu.h"

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

/* Why the run ended. HALT_STOPPED is the host's doing (the window was closed);
 * the headless emulator never produces it. */
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
    uint32_t x[32], f[32];
    uint8_t fcsr;
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
    size_t dropped;         /* events the full queue refused; a passing run is rejected unless allowed */
    FILE *record;           /* every event offered to the queue as a script line, or NULL */
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

#endif
