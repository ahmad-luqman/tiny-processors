#!/usr/bin/env python3
"""Run the RV32 RTL testbench and the emulator on one image and diff their traces.

The testbench (tests/rv32_tb.sv) prints the retirement trace in the emulator's
format, so agreement is a plain line-for-line comparison. This module holds the
pieces the tests and the Make targets share: compiling the testbench, running
either backend with the documented arguments, parsing the testbench's halt
line, deciding whether a run passed, and reporting the first trace difference
with context.
"""

import argparse
import re
from collections import namedtuple
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_asm import PROGRAM_INPUTS, PROGRAMS, TIMER, words_to_bytes, words_to_hex  # noqa: E402
from tools.rv32_image import to_hex_words  # noqa: E402
from tools.rv32_run_emu import DEFAULT_EMULATOR, emulator_command, halt_line, last_halt_line, parse_halt_line  # noqa: E402

RTL_SOURCES = [ROOT / "rtl" / "rv32" / name
               for name in ("rv32_fregfile.v", "rv32_fdecode.v", "rv32_regfile.v", "rv32_alu.v", "rv32_decode.v", "rv32.v",
                            "rv32_bus.v", "rv32_ram.v", "rv32_console.v", "rv32_done.v", "rv32_timer.v", "rv32_input.v", "rv32_display.v", "rv32_soc.v")]
RTL_SOURCES.append(ROOT / "rtl/fp32/fp32.v")
TESTBENCH = ROOT / "tests" / "rv32_tb.sv"
DEFAULT_SIMULATOR = "build/rv32/rv32_tb.vvp"
DEFAULT_OUT = "build/rv32/rtl"
BENCH_SEED = 7
COUNTERS = ("cycles", "steps", "stalls", "transfers")
DECIMAL = COUNTERS + ("cause", "fp_waits")
HEX = ("done", "tval", "pc", "word")
# The keys each halt reason carries besides the counters and the outcome (docs/rv32-rtl.md).
REQUIRED = {"done": ("done",), "double-fault": ("cause", "tval"), "limit": ()}
# A run's guest transcript and the simulator's own output are kept apart:
# `console` is what the guest printed (the emulator's stdout, or the file the
# testbench writes with +console), `noise` is anything the simulator itself
# printed on stdout (Icarus $fatal, $readmemh, and plusarg messages, Verilator
# %Error/%Fatal), which is empty for a clean run and always empty for the emulator.
# `checkpoints` is the `frame N <hash>` lines the backend wrote, when a file was asked for.
Run = namedtuple("Run", "status console noise stderr trace halt checkpoints", defaults=([],))


def compile_testbench(output, iverilog="iverilog", params=None):
    """Compile the testbench and the machine for Icarus into `output`; `params` overrides
    testbench parameters (`{"CONSOLE_BUSY": 2}`), the way `-G` does for a Verilator build."""
    overrides = [f"-Prv32_tb.{name}={value}" for name, value in (params or {}).items()]
    subprocess.run([iverilog, "-g2012", "-Wall", "-I" + str(ROOT / "rtl/fp32"), *overrides, "-s", "rv32_tb", "-o", str(output),
                    str(TESTBENCH), *map(str, RTL_SOURCES)], check=True)


def simulator_command(simulator, image, trace=None, console=None, wave=None, stall=None, seed=None, max_cycles=None,
                      checkpoints=None, input_script=None, reset_at=None, allow_lost_events=False):
    """The command line for a compiled testbench: `vvp` for a .vvp file, else a Verilator binary."""
    simulator = Path(simulator)
    if simulator.suffix == ".vvp":
        command = ["vvp", str(simulator)]
    else:
        command = [str(simulator), "+verilator+quiet"]  # no simulation report on stdout
    command.append(f"+image={image}")
    if trace is not None:
        command.append(f"+trace={trace}")
    if console is not None:
        command.append(f"+console={console}")
    if wave is not None:
        command.append(f"+wave={wave}")
    if stall is not None:
        command.append(f"+stall={stall}")
    if seed is not None:
        command.append(f"+stall-seed={seed}")
    if max_cycles is not None:
        command.append(f"+max-cycles={max_cycles}")
    if checkpoints is not None:
        command.append(f"+checkpoints={checkpoints}")
    if input_script is not None:
        command.append(f"+input={input_script}")
    if reset_at is not None:
        command.append(f"+reset-at={reset_at}")
    if allow_lost_events:
        command.append("+allow-lost-events")
    return command


def rtl_halt_line(stderr):
    """Parse the testbench's final `rv32_tb: halt=...` line into a dict, or None if absent.

    The halt reason decides which keys must be present (`done`; `cause` and
    `tval` of the undeliverable trap; none for a limit); anything else raises ValueError
    naming the line, so testbench format drift is caught here.
    """
    line = last_halt_line(stderr, "rv32_tb:")
    if line is None:
        return None
    fields = parse_halt_line(line, DECIMAL, HEX)
    reason = fields["halt"]
    if reason not in REQUIRED:
        raise ValueError(f"unknown halt reason {reason!r} in {line!r}")
    expected = {"halt", "outcome", *COUNTERS, *REQUIRED[reason]}
    if "fp_waits" in fields:
        expected.add("fp_waits")
        if fields["fp_waits"] < 0: raise ValueError("negative FPU wait count")
    if set(fields) != expected:
        raise ValueError(f"halt line keys {sorted(fields)} do not match {sorted(expected)} in {line!r}")
    return fields


def decode(data):
    """Guest and simulator output as text: any byte is allowed, so decoding never raises."""
    return data.decode("utf-8", errors="backslashreplace")


# The only thing a simulator may say on stdout in a clean run: Icarus announces
# the VCD it opened. The guest cannot reach stdout once +console is given, so
# this allowlist cannot hide guest text.
INFORMATIONAL = ("VCD info: ",)


def simulator_noise(stdout):
    """Simulator stdout with the known informational lines removed; empty for a clean run."""
    return "".join(line for line in stdout.splitlines(keepends=True) if not line.startswith(INFORMATIONAL))


def run_backend(command, trace, parse_halt, timeout, console=None, checkpoints=None):
    """Run one backend; the trace (and console) files are truncated first so a crash cannot pass.

    With `console`, the guest transcript is read from that file and the process's
    stdout is the simulator's own noise; without it the transcript is stdout and
    there is no noise channel (the emulator). A malformed halt line becomes
    `halt=None` with the reason appended to stderr.
    """
    Path(trace).write_text("")
    if console is not None:
        Path(console).write_text("")
    if checkpoints is not None:
        Path(checkpoints).write_text("")
    try:
        completed = subprocess.run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        sys.exit(f"{command[0]} did not finish within {timeout} s; raise --timeout or bound the run "
                 f"(a partial trace is in {trace})")
    stdout, stderr = decode(completed.stdout), decode(completed.stderr)
    try:
        halt = parse_halt(stderr)
    except ValueError as error:
        halt, stderr = None, f"{stderr}\n{error}\n"
    if console is None:
        transcript, noise = stdout, ""
    else:
        transcript, noise = decode(Path(console).read_bytes()), simulator_noise(stdout)
    lines = Path(checkpoints).read_text().splitlines() if checkpoints is not None else []
    return Run(completed.returncode, transcript, noise, stderr, Path(trace).read_text().splitlines(), halt, lines)


def run_rtl(simulator, image_hex, trace, stall=None, seed=None, wave=None, max_cycles=None, timeout=120,
            checkpoints=None, input_script=None, reset_at=None, allow_lost_events=False):
    """Run the testbench on a hex image with the documented plusargs; the console goes next to the trace."""
    console = Path(trace).with_name(Path(trace).name + ".console")
    command = simulator_command(simulator, image_hex, trace=trace, console=console, wave=wave,
                                stall=stall, seed=seed, max_cycles=max_cycles, checkpoints=checkpoints,
                                input_script=input_script, reset_at=reset_at, allow_lost_events=allow_lost_events)
    return run_backend(command, trace, rtl_halt_line, timeout, console=console, checkpoints=checkpoints)


def run_emulator(emulator, image_bin, trace, limit=None, timeout=120, checkpoints=None, input_script=None,
                 frames=None, allow_lost_events=False):
    """Run the emulator on a flat image with a trace, the same way tools/rv32_run_emu.py does."""
    command = emulator_command(emulator, image_bin, trace=trace, limit=limit, checkpoints=checkpoints,
                               input_script=input_script, frames=frames, allow_lost_events=allow_lost_events)
    return run_backend(command, trace, halt_line, timeout, checkpoints=checkpoints)


def diff_traces(rtl, emulator, context=3):
    """Return None if the traces are identical, else the first difference with preceding lines."""
    common = min(len(rtl), len(emulator))
    for index in range(common):
        if rtl[index] != emulator[index]:
            before = rtl[max(0, index - context):index]
            return (f"line {index + 1}: RTL '{rtl[index]}', emulator '{emulator[index]}'; "
                    f"preceding {before}")
    if len(rtl) != len(emulator):
        longer, count = ("emulator", len(emulator)) if len(emulator) > len(rtl) else ("RTL", len(rtl))
        return f"traces agree for {common} line(s), then the {longer} continues to {count}"
    return None


def describe(run):
    """Everything a failed run printed, for an error message."""
    return (f"status {run.status}\n--- simulator output ---\n{run.noise}--- guest console ---\n{run.console}"
            f"--- stderr ---\n{run.stderr}")


def check_passed(run, name="simulator"):
    """A matching trace is not enough: the backend must exit cleanly, print nothing of its own, and
    report `halt=done ... pass`; otherwise exit with everything it printed. A scripted event the
    backend dropped or never delivered is a nonzero exit status on both backends (docs/rv32.md,
    "Input"), so it is caught here without reading their messages."""
    if run.status != 0 or run.noise:
        sys.exit(f"{name} failed:\n{describe(run)}")
    if run.halt is None:
        sys.exit(f"{name} printed no valid halt line:\n{describe(run)}")
    if (run.halt["halt"], run.halt["outcome"]) != ("done", "pass"):
        sys.exit(f"{name} run did not pass: {run.halt}\n{describe(run)}")


def write_image(words, out, name):
    """Write both image forms and return (hex_path, bin_path)."""
    out = Path(out)
    out.mkdir(parents=True, exist_ok=True)
    hex_path, bin_path = out / f"{name}.hex", out / f"{name}.bin"
    hex_path.write_text(words_to_hex(words))
    bin_path.write_bytes(words_to_bytes(words))
    return hex_path, bin_path


def has_value_changes(vcd):
    """True when a VCD has its header closed and at least one timestamp after it: a dump that
    resolved to an empty scope has definitions and nothing else."""
    _, separator, changes = vcd.partition("$enddefinitions")
    return bool(separator) and any(line.startswith("#") for line in changes.splitlines())


def trap_records(trace):
    """The trace's trap lines without their step numbers: PC, word, cause, and value, in order. Device
    time moves the step numbers between backends; it never moves a fault or changes its cause."""
    return [line.split(" ", 1)[1] for line in trace if " trap " in line]


def compare_backends(rtl, emulator, compare):
    """What must agree between two passing runs, as the first mismatch or None: the console
    transcript and the checkpoint lines always; the whole retirement trace in trace mode; in
    results mode (device time) the trap records, which step numbers never move."""
    if rtl.console != emulator.console:
        return f"console mismatch: RTL {rtl.console!r}, emulator {emulator.console!r}"
    if rtl.checkpoints != emulator.checkpoints:
        return f"checkpoint mismatch: RTL {rtl.checkpoints}, emulator {emulator.checkpoints}"
    if compare == "results":
        rtl_traps, emulator_traps = trap_records(rtl.trace), trap_records(emulator.trace)
        if rtl_traps != emulator_traps:
            return f"trap mismatch: RTL {rtl_traps}, emulator {emulator_traps}"
        return None
    difference = diff_traces(rtl.trace, emulator.trace)
    return f"trace mismatch: {difference}" if difference else None


def generated_paths(out, name, frames=None):
    """Every file a run may write or truncate under `out` (and the frames directory), so an
    input script that names one of them can be refused before anything is written."""
    paths = {out / f"{name}.{suffix}" for suffix in ("hex", "bin", "input", "vcd", "emu.trace", "emu.checkpoints",
                                                     "rtl.trace", "rtl.trace.console", "rtl.checkpoints")}
    if frames is not None:
        paths |= set(frames.glob("frame-*.ppm"))
    return paths


def refuse_aliased_input(script, out, name, frames=None, option="--input"):
    """Exit if `script` is (by path or by inode) one of the run's own files: the trace, console, and
    checkpoint files are truncated before a backend starts, which would destroy the script (or make
    an expected-checkpoints file compare the run against itself)."""
    for path in generated_paths(out, name, frames):
        same = path.resolve() == script.resolve() or (path.exists() and script.exists() and path.samefile(script))
        if same:
            sys.exit(f"{option} {script} names a file this run writes ({path}); keep it outside --out")


def read_expected_checkpoints(path):
    """The `frame N <hash>` lines of an expected-checkpoints file; blank lines and `#` comments are
    skipped. A file with no lines, or a malformed one, is refused: an empty expectation would compare
    nothing and pass."""
    if not path.is_file():
        sys.exit(f"--expect-checkpoints {path} is not a file")
    lines = [line.strip() for line in path.read_text().splitlines()]
    lines = [line for line in lines if line and not line.startswith("#")]
    if not lines:
        sys.exit(f"--expect-checkpoints {path} has no `frame N <hash>` lines")
    for number, line in enumerate(lines, 1):
        if not re.fullmatch(r"frame \d+ [0-9a-f]{8}", line):
            sys.exit(f"--expect-checkpoints {path}: line {number} is not `frame N <hash>`: {line!r}")
    return lines


def first_checkpoint_difference(observed, expected):
    """Where two checkpoint lists first differ, for a message that does not print 200 lines."""
    for index, (got, want) in enumerate(zip(observed, expected), 1):
        if got != want:
            return f"line {index}: got {got!r}, expected {want!r}"
    return f"{len(observed)} line(s) observed, {len(expected)} expected"


def cycle_relation(rtl):
    """Relate the testbench's cycle count to the trace: 4 cycles per instruction without a data
    access, 5 with one, plus the stalls. Returns (text, holds); `holds` is None when a trap line
    makes the formula inapplicable (a trap costs the cycles up to the state that raised it)."""
    steps = len(rtl.trace)
    memory = sum("mem[" in line for line in rtl.trace)
    traps = sum(" trap " in line for line in rtl.trace)
    halt = rtl.halt
    expected = 4 * (steps - memory) + 5 * memory + halt["stalls"] + halt.get("fp_waits", 0)
    text = (f"cycles {halt['cycles']} = 4 x {steps - memory} + 5 x {memory} + {halt['stalls']} stalls; "
            f"transfers {halt['transfers']} = {steps} fetches + {memory} data")
    if halt.get("fp_waits", 0):
        text += f"; plus {halt['fp_waits']} FPU issue/wait cycles"
    if traps:
        return f"{text} (not exact: {traps} trap lines)", None
    return text, halt["cycles"] == expected and halt["transfers"] == steps + memory


def check_fp_waits(rtl, expected):
    """A workload-specific latency pin, independent of the accounting identity."""
    if expected is not None and rtl.halt.get("fp_waits", 0) != expected:
        sys.exit(f"FPU wait cycles: got {rtl.halt.get('fp_waits', 0)}, expected {expected}")


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("check", "waves", "bench"), default="check")
    parser.add_argument("--program", choices=sorted(PROGRAMS), default="loop", help="assembled program to run")
    parser.add_argument("--image", type=Path, help="a flat .bin image to run instead of an assembled program")
    parser.add_argument("--expect-console", help="the guest console output both backends must produce")
    parser.add_argument("--expect-last-line", help="the last console line both backends must produce")
    parser.add_argument("--expect-checkpoint", action="append", default=[], metavar="LINE",
                        help="the `frame N <hash>` lines both backends must write, exactly and in order (repeatable)")
    parser.add_argument("--expect-checkpoints", type=Path, metavar="FILE",
                        help="a file of `frame N <hash>` lines to expect, one per line, after any --expect-checkpoint")
    parser.add_argument("--allow-lost-events", action="store_true",
                        help="tell both backends to accept a run that dropped a scripted event or never delivered one")
    parser.add_argument("--input", type=Path,
                        help="input script delivered to both backends (docs/rv32.md, Input); not a file under --out")
    parser.add_argument("--frames", type=Path, help="directory for the emulator's frame-NNNN.ppm pictures")
    parser.add_argument("--compare", choices=("trace", "results"), default="trace",
                        help="`trace`: identical retirement traces and the cycle formula; `results`: identical "
                             "console, outcome, and checkpoints, for a program that reads the timer (device time)")
    parser.add_argument("--backend", choices=("both", "emulator"), default="both",
                        help="`emulator` runs and checks the emulator only")
    parser.add_argument("--allow-traps", action="store_true",
                        help="accept a trace with trap lines, where the cycle formula is not exact")
    parser.add_argument("--emulator", default=DEFAULT_EMULATOR)
    parser.add_argument("--simulator", help=f".vvp file or Verilator binary (default {DEFAULT_SIMULATOR}; unused with --backend emulator)")
    parser.add_argument("--out", default=DEFAULT_OUT, help="directory for the image, traces, and VCD")
    parser.add_argument("--timeout", type=float, default=120.0, help="seconds each backend may run (default 120)")
    parser.add_argument("--max-cycles", type=int, help="RTL cycle budget (default: testbench budget of 10000000)")
    parser.add_argument("--stall", type=int, default=None, help="fixed stall cycles per request")
    parser.add_argument("--seed", type=int, default=None, help="random 0..3 stall cycles per request")
    parser.add_argument("--expect-fp-waits", type=int, help="pin total RTL FPU issue/wait cycles")
    args = parser.parse_args()
    if args.expect_fp_waits is not None:
        if args.expect_fp_waits < 0: parser.error("--expect-fp-waits must not be negative")
        if args.backend == "emulator": parser.error("--expect-fp-waits requires the RTL backend")
    if args.stall is not None and args.stall < 0:
        parser.error("--stall must not be negative")
    if args.stall is not None and args.seed is not None:
        parser.error("--stall and --seed are exclusive")
    if args.max_cycles is not None and not 1 <= args.max_cycles <= 2147483647:
        parser.error("--max-cycles must be in 1..2147483647")
    if args.timeout <= 0:
        parser.error("--timeout must be positive")
    if args.simulator is None and args.backend == "both":
        args.simulator = DEFAULT_SIMULATOR
    for path in (args.emulator, args.simulator):
        if path is not None and not Path(path).exists() and shutil.which(path) is None:
            parser.error(f"{path} does not exist; run make build-rv32-emu / build-rv32-rtl first")

    out = Path(args.out)
    name = args.image.stem if args.image is not None else args.program
    if args.input is not None:
        refuse_aliased_input(args.input, out, name, args.frames)
    if args.expect_checkpoints is not None:
        if not args.expect_checkpoints.exists():
            parser.error(f"{args.expect_checkpoints} does not exist")
        refuse_aliased_input(args.expect_checkpoints, out, name, args.frames, option="--expect-checkpoints")
        args.expect_checkpoint += read_expected_checkpoints(args.expect_checkpoints)  # never empty
    if args.image is not None:
        if not args.image.exists():
            parser.error(f"{args.image} does not exist; run make check-rv32-image first")
        out.mkdir(parents=True, exist_ok=True)
        hex_path, bin_path = out / f"{name}.hex", args.image
        hex_path.write_text("".join(f"{word}\n" for word in to_hex_words(bin_path.read_bytes())))
    else:
        hex_path, bin_path = write_image(PROGRAMS[name](), out, name)
        if args.input is None and name in PROGRAM_INPUTS:
            args.input = out / f"{name}.input"
            args.input.write_text(PROGRAM_INPUTS[name])
    if args.frames is not None:
        args.frames.mkdir(parents=True, exist_ok=True)
        for stale in args.frames.glob("frame-*.ppm"): # a previous run's frames must not survive this one
            stale.unlink()
    emulator = run_emulator(args.emulator, bin_path, out / f"{name}.emu.trace", timeout=args.timeout,
                            checkpoints=out / f"{name}.emu.checkpoints", input_script=args.input, frames=args.frames,
                            allow_lost_events=args.allow_lost_events)
    check_passed(emulator, "emulator")
    # Device time (docs/rv32.md): a timer-reading program has no single trace, so its traces are
    # never diffed; results mode compares what the guest printed, presented, and trapped on instead.
    reads_timer = any(f"mem[{TIMER:08x}]->" in line for line in emulator.trace)
    if args.compare == "trace" and reads_timer:
        sys.exit("this program reads the timer, so its traces differ by design; use --compare results")
    if args.compare == "results" and not reads_timer:
        sys.exit("--compare results is for a program that reads the timer; this one never did, use --compare trace")
    if args.expect_console is not None and emulator.console.rstrip("\n") != args.expect_console:
        sys.exit(f"emulator console {emulator.console!r} is not {args.expect_console!r}")
    last_line = emulator.console.rstrip("\n").rsplit("\n", 1)[-1]
    if args.expect_last_line is not None and last_line != args.expect_last_line:
        sys.exit(f"emulator's last console line {last_line!r} is not {args.expect_last_line!r}")
    if args.expect_checkpoint and emulator.checkpoints != args.expect_checkpoint:
        sys.exit(f"emulator checkpoints are not the expected ones: {first_checkpoint_difference(emulator.checkpoints, args.expect_checkpoint)}")
    if args.backend == "emulator":
        print(emulator.stderr.strip().splitlines()[-1])
        print(f"emulator: {len(emulator.trace)} trace lines, {len(emulator.checkpoints)} checkpoint(s), "
              f"console ends {last_line!r}")
        return

    if args.mode == "bench":
        print("stall  cycles  stalls  transfers  steps")
        seed = BENCH_SEED if args.seed is None else args.seed
        for stall, seed in [(0, None), (1, None), (2, None), (3, None), (None, seed)]:
            rtl = run_rtl(args.simulator, hex_path, out / f"{name}.rtl.trace", stall=stall, seed=seed,
                          timeout=args.timeout, max_cycles=args.max_cycles, checkpoints=out / f"{name}.rtl.checkpoints", input_script=args.input,
                          allow_lost_events=args.allow_lost_events)
            check_passed(rtl)
            check_fp_waits(rtl, args.expect_fp_waits)
            mismatch = compare_backends(rtl, emulator, args.compare)  # the same agreement as a check run
            if mismatch:
                sys.exit(f"stall={stall} seed={seed}: {mismatch}")
            if stall is not None and rtl.halt["stalls"] != stall * rtl.halt["transfers"]:
                sys.exit(f"stall={stall}: {rtl.halt['stalls']} stalls for {rtl.halt['transfers']} transfers")
            label = f"seed {seed}" if seed is not None else str(stall)
            print(f"{label:>6}  {rtl.halt['cycles']:>6}  {rtl.halt['stalls']:>6}  "
                  f"{rtl.halt['transfers']:>9}  {rtl.halt['steps']:>5}")
        return

    # Random stalls replace the fixed count; waves default to a stall so the handshake is visible.
    if args.seed is not None:
        stall = None
    elif args.stall is not None:
        stall = args.stall
    elif args.mode == "waves":
        stall = 2
    else:
        stall = 0
    wave = out / f"{name}.vcd" if args.mode == "waves" else None
    rtl = run_rtl(args.simulator, hex_path, out / f"{name}.rtl.trace", stall=stall, seed=args.seed, wave=wave,
                  timeout=args.timeout, max_cycles=args.max_cycles, checkpoints=out / f"{name}.rtl.checkpoints", input_script=args.input,
                  allow_lost_events=args.allow_lost_events)
    print(emulator.stderr.strip().splitlines()[-1])
    print(rtl.stderr.strip().splitlines()[-1] if rtl.stderr.strip() else "rv32_tb: no halt line")
    check_passed(rtl)
    check_fp_waits(rtl, args.expect_fp_waits)
    mismatch = compare_backends(rtl, emulator, args.compare)
    if mismatch:
        sys.exit(mismatch)
    if args.compare == "results":
        # What the guest printed and presented agreed, and so did every fault it took: the same PC,
        # word, cause, and value in the same order, only the step numbers differing.
        traps = len(trap_records(rtl.trace))
        print(f"results identical: {len(emulator.console.splitlines())} console line(s) ending {last_line!r}, "
              f"{len(rtl.checkpoints)} checkpoint(s) {rtl.checkpoints}, {traps} trap(s) alike; RTL "
              f"{len(rtl.trace)} instructions in {rtl.halt['cycles']} cycles, emulator {len(emulator.trace)} instructions")
    else:
        print(f"traces identical: {len(rtl.trace)} lines; {out / f'{name}.rtl.trace'}")
        relation, holds = cycle_relation(rtl)
        print(relation)
        if holds is None and not args.allow_traps:
            sys.exit("the trace has trap lines, so the cycle formula cannot be checked; pass --allow-traps if that is expected")
        if holds is False:
            sys.exit("the cycle count does not follow the state machine")
    if wave is not None:
        if not wave.exists() or not has_value_changes(wave.read_text()):
            sys.exit(f"the simulator wrote no waveform to {wave}")
        print(f"waveform: {wave}")


if __name__ == "__main__":
    main()
