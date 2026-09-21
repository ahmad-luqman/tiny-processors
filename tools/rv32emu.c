/* rv32emu: headless emulator for the RV32 machine (docs/rv32.md, docs/rv32-emulator.md).
 *
 * Loads a flat image (the .bin that tools/rv32_image.py flattens from the ELF)
 * into RAM, then executes RV32I plus the CSR subset of the trap contract until
 * the guest writes the done register, a trap cannot be delivered, or the
 * instruction bound is reached. Guest console bytes go to stdout and nothing
 * else does; diagnostics go to stderr; the retirement trace goes to a file.
 * The machine itself is rv32emu_core.c, shared with the window (rv32win.c).
 *
 * Build: cc -std=c11 -O2 -Wall -Wextra -Werror -o rv32emu rv32emu.c rv32emu_core.c
 */
#define _POSIX_C_SOURCE 200809L
#include "rv32emu_core.h"

#include <stdlib.h>
#include <string.h>

static void usage(void)
{
    fputs("usage: rv32emu --image FILE [--base ADDR] [--pc ADDR] [--trace FILE] [--dump-state FILE]\n"
          "               [--max-instructions N] [--checkpoints FILE] [--frames DIR] [--input FILE]\n"
          "               [--record FILE] [--allow-lost-events]\n"
          "Loads FILE at ADDR (default 0x80000000), starts at --pc (default 0x80000000), and runs\n"
          "until the done register is written. Console bytes go to stdout, the trace and state\n"
          "to their files, and a final 'rv32emu: halt=...' line to stderr. Each present appends\n"
          "'frame N <hash>' to the checkpoints file and writes DIR/frame-NNNN.ppm. The input\n"
          "script's `frame N down|up KEY` events arrive when frame N is presented; an event the\n"
          "full queue dropped or the guest never reached turns a pass into an error unless\n"
          "--allow-lost-events says it was expected. --record writes every event offered to the\n"
          "queue as a script that replays the run.\n",
          stderr);
    exit(EXIT_EMULATOR_ERROR);
}

int main(int argc, char **argv)
{
    const char *image_path = NULL, *trace_path = NULL, *state_path = NULL, *checkpoints_path = NULL;
    const char *input_path = NULL, *record_path = NULL, *frames_dir = NULL;
    uint32_t base = RAM_BASE, start = 0;
    bool start_given = false, allow_lost_events = false;
    machine m;
    emu_init(&m);
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
            base = emu_parse_u32(value, "base address");
        } else if (!strcmp(arg, "--pc")) {
            start = emu_parse_u32(value, "start pc");
            start_given = true;
        } else if (!strcmp(arg, "--trace")) {
            trace_path = value;
        } else if (!strcmp(arg, "--dump-state")) {
            state_path = value;
        } else if (!strcmp(arg, "--max-instructions")) {
            m.limit = emu_parse_u64(value, UINT64_MAX, "instruction limit");
        } else if (!strcmp(arg, "--checkpoints")) {
            checkpoints_path = value;
        } else if (!strcmp(arg, "--frames")) {
            frames_dir = value;
        } else if (!strcmp(arg, "--input")) {
            input_path = value;
        } else if (!strcmp(arg, "--record")) {
            record_path = value;
        } else {
            usage();
        }
    }
    if (!image_path) {
        usage();
    }
    size_t loaded = 0;
    if (!emu_alloc(&m) || !emu_load_image(&m, image_path, base, &loaded)) {
        return EXIT_EMULATOR_ERROR;
    }
    m.pc = start_given ? start : RAM_BASE; /* reset PC from the contract */
    /* Every output is checked against every input and every other output before anything is
     * written, so no option can name a file another one reads or writes. */
    if (record_path) {
        emu_require_distinct(record_path, "record file", image_path, "image");
        emu_require_distinct(record_path, "record file", input_path, "input script");
        m.record = fopen(record_path, "w");
        if (!m.record) {
            fprintf(stderr, "rv32emu: cannot write %s\n", record_path);
            return EXIT_EMULATOR_ERROR;
        }
    }
    if (input_path) {
        emu_read_input_script(&m, input_path);
        emu_deliver_events(&m); /* frame 0's events are queued before the first instruction */
    }
    if (trace_path) {
        emu_require_distinct(trace_path, "trace file", image_path, "image");
        emu_require_distinct(trace_path, "trace file", input_path, "input script");
        emu_require_distinct(trace_path, "trace file", record_path, "record file");
        m.trace = fopen(trace_path, "w");
        if (!m.trace) {
            fprintf(stderr, "rv32emu: cannot write %s\n", trace_path);
            return EXIT_EMULATOR_ERROR;
        }
    }
    if (checkpoints_path) {
        emu_require_distinct(checkpoints_path, "checkpoints file", image_path, "image");
        emu_require_distinct(checkpoints_path, "checkpoints file", trace_path, "trace file");
        emu_require_distinct(checkpoints_path, "checkpoints file", input_path, "input script");
        emu_require_distinct(checkpoints_path, "checkpoints file", record_path, "record file");
        m.checkpoints = fopen(checkpoints_path, "w");
        if (!m.checkpoints) {
            fprintf(stderr, "rv32emu: cannot write %s\n", checkpoints_path);
            return EXIT_EMULATOR_ERROR;
        }
    }
    if (frames_dir) {
        emu_require_distinct(frames_dir, "frames directory", image_path, "image");
        emu_require_distinct(frames_dir, "frames directory", input_path, "input script");
        m.frames_dir = frames_dir;
    }

    while (emu_run_until(&m, UINT64_MAX) != EMU_STOP_HALTED) {
        /* the headless emulator has nothing to do at a present */
    }
    bool outputs_ok = emu_finish_outputs(&m, trace_path, checkpoints_path, record_path);
    if (state_path) {
        emu_require_distinct(state_path, "state file", image_path, "image");
        emu_require_distinct(state_path, "state file", trace_path, "trace file");
        emu_require_distinct(state_path, "state file", checkpoints_path, "checkpoints file");
        emu_require_distinct(state_path, "state file", input_path, "input script");
        emu_require_distinct(state_path, "state file", record_path, "record file");
        FILE *out = fopen(state_path, "w");
        if (!out) {
            fprintf(stderr, "rv32emu: cannot write %s\n", state_path);
            return EXIT_EMULATOR_ERROR;
        }
        emu_dump_state(&m, out);
        if (!emu_close_output(out, state_path)) {
            outputs_ok = false;
        }
    }
    int status = emu_report_halt(&m, loaded);
    emu_free(&m);
    return emu_exit_status(&m, status, outputs_ok, allow_lost_events);
}
