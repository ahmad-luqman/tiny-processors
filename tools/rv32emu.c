/* rv32emu: headless emulator for the RV32 machine (docs/rv32.md, docs/rv32-emulator.md).
 *
 * Loads a flat image (the .bin that tools/rv32_image.py flattens from the ELF)
 * into RAM, then executes RV32I plus the CSR subset of the trap contract until
 * the guest writes the done register, a trap cannot be delivered, or the
 * instruction bound is reached. Guest console bytes go to stdout and nothing
 * else does; diagnostics go to stderr; the retirement trace goes to a file.
 * The machine itself is rv32emu_core.c, shared with the window (rv32win.c).
 *
 * Build: make build-rv32-emu (links the core, SIMD4/graphics devices and pinned SoftFloat objects).
 */
#define _POSIX_C_SOURCE 200809L
#include "rv32emu_core.h"
#include "rv32_gdb.h"

#include <stdlib.h>
#include <string.h>

static void usage(void)
{
    fputs("usage: rv32emu --image FILE [--base ADDR] [--pc ADDR] [--trace FILE] [--dump-state FILE]\n"
          "               [--max-instructions N] [--checkpoints FILE] [--frames DIR] [--input FILE]\n"
          "               [--record FILE] [--allow-lost-events] [--gdb PORT] [--console-input FILE|-]\n"
          "Loads FILE at ADDR (default 0x80000000), starts at --pc (default 0x80000000), and runs\n"
          "until the done register is written. Console bytes go to stdout, the trace and state\n"
          "to their files, and a final 'rv32emu: halt=...' line to stderr. Each present appends\n"
          "'frame N <hash>' to the checkpoints file and writes DIR/frame-NNNN.ppm. The input\n"
          "script's `frame N down|up KEY` events arrive when frame N is presented; an event the\n"
          "full queue dropped or the guest never reached turns a pass into an error unless\n"
          "--allow-lost-events says it was expected. --record writes every event offered to the\n"
          "queue as a script that replays the run. --gdb serves one GDB remote-protocol client on\n"
          "127.0.0.1:PORT (0 picks a free port; the chosen one is printed to stderr) with the\n"
          "machine stopped at the reset pc (docs/rv32-gdb.md). --console-input gives the console's\n"
          "receive side every byte of FILE from reset, or stdin as it arrives with -.\n",
          stderr);
    exit(EXIT_EMULATOR_ERROR);
}

int main(int argc, char **argv)
{
    const char *image_path = NULL, *trace_path = NULL, *state_path = NULL, *checkpoints_path = NULL;
    const char *input_path = NULL, *record_path = NULL, *frames_dir = NULL, *console_input = NULL;
    uint32_t base = RAM_BASE, start = 0;
    bool start_given = false, allow_lost_events = false;
    int gdb_port = -1; /* --gdb: serve a debugger on this port (0 picks one) */
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
        } else if (!strcmp(arg, "--console-input")) {
            console_input = value;
        } else if (!strcmp(arg, "--gdb")) {
            gdb_port = (int)emu_parse_u64(value, 65535, "gdb port");
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
    /* Every output is checked against the inputs and every other output, and the script is
     * parsed, before any output is created: a refused run truncates nothing. The state file is
     * written after the run and is checked here too. */
    const char *inputs[][2] = {{image_path, "image"}, {input_path, "input script"},
                               {console_input && strcmp(console_input, "-") ? console_input : NULL, "console input"}};
    const char *outputs[][2] = {{record_path, "record file"}, {trace_path, "trace file"}, {checkpoints_path, "checkpoints file"},
                                {state_path, "state file"}, {frames_dir, "frames directory"}};
    for (size_t i = 0; i < sizeof outputs / sizeof outputs[0]; i++) {
        for (size_t j = 0; j < sizeof inputs / sizeof inputs[0]; j++) {
            emu_require_distinct(outputs[i][0], outputs[i][1], inputs[j][0], inputs[j][1]);
        }
        for (size_t j = 0; j < i; j++) {
            emu_require_distinct(outputs[i][0], outputs[i][1], outputs[j][0], outputs[j][1]);
        }
    }
    /* Nothing the run reads or writes may sit in the frames directory, where frame-NNNN.ppm files appear. */
    for (size_t i = 0; i + 1 < sizeof outputs / sizeof outputs[0]; i++) {
        emu_require_outside(outputs[i][0], outputs[i][1], frames_dir, "frames directory");
    }
    for (size_t j = 0; j < sizeof inputs / sizeof inputs[0]; j++) {
        emu_require_outside(inputs[j][0], inputs[j][1], frames_dir, "frames directory");
    }
    if (input_path) {
        emu_read_input_script(&m, input_path); /* exits on a bad script */
    }
    if (console_input) {
        emu_read_console_input(&m, console_input); /* exits on an unreadable file */
    }
    if (record_path) { /* opened before frame 0's events are delivered, so they are recorded too */
        m.record = emu_open_output(record_path, "record file");
        if (!m.record) {
            return EXIT_EMULATOR_ERROR;
        }
    }
    if (trace_path) {
        m.trace = emu_open_output(trace_path, "trace file");
        if (!m.trace) {
            return EXIT_EMULATOR_ERROR;
        }
    }
    if (checkpoints_path) {
        m.checkpoints = emu_open_output(checkpoints_path, "checkpoints file");
        if (!m.checkpoints) {
            return EXIT_EMULATOR_ERROR;
        }
    }
    /* Names have been compared every way a name can be; the open files settle it. */
    emu_require_distinct_streams(m.record, record_path, "record file", m.trace, trace_path, "trace file");
    emu_require_distinct_streams(m.record, record_path, "record file", m.checkpoints, checkpoints_path, "checkpoints file");
    emu_require_distinct_streams(m.trace, trace_path, "trace file", m.checkpoints, checkpoints_path, "checkpoints file");
    m.frames_dir = frames_dir;
    if (input_path) {
        emu_deliver_events(&m); /* frame 0's events are queued before the first instruction */
    }

    /* Under --gdb the client drives the machine until the guest halts, the client kills the run
     * (HALT_STOPPED), or it detaches, in which case the run continues as if it had never been
     * attached. Everything after the halt is the same as for a plain run. */
    if (gdb_port >= 0) {
        int fd = gdb_accept((uint16_t)gdb_port);
        if (fd < 0) {
            return EXIT_EMULATOR_ERROR;
        }
        gdb_serve(fd, &m); /* owns fd from here */
    }
    while (emu_run_until(&m, UINT64_MAX) != EMU_STOP_HALTED) {
        /* the headless emulator has nothing to do at a present */
    }
    bool outputs_ok = emu_finish_outputs(&m, trace_path, checkpoints_path, record_path);
    if (state_path) {
        FILE *out = emu_open_output(state_path, "state file");
        if (!out) {
            emu_free(&m);
            gdb_report_exit(EXIT_EMULATOR_ERROR);
            return EXIT_EMULATOR_ERROR;
        }
        emu_dump_state(&m, out);
        if (!emu_close_output(out, state_path)) {
            outputs_ok = false;
        }
    }
    int status = emu_exit_status(&m, emu_report_halt(&m, loaded), outputs_ok, allow_lost_events);
    emu_free(&m);
    gdb_report_exit(status); /* after a halt under gdb, the client learns the same status the process returns */
    return status;
}
