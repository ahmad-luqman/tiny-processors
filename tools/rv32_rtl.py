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
from collections import namedtuple
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_asm import PROGRAMS, words_to_bytes, words_to_hex  # noqa: E402
from tools.rv32_image import to_hex_words  # noqa: E402
from tools.rv32_run_emu import DEFAULT_EMULATOR, emulator_command, halt_line, last_halt_line, parse_halt_line  # noqa: E402

RTL_SOURCES = [ROOT / "rtl" / "rv32" / name
               for name in ("rv32_regfile.v", "rv32_alu.v", "rv32_decode.v", "rv32.v")]
TESTBENCH = ROOT / "tests" / "rv32_tb.sv"
DEFAULT_SIMULATOR = "build/rv32/rv32_tb.vvp"
DEFAULT_OUT = "build/rv32/rtl"
BENCH_SEED = 7
COUNTERS = ("cycles", "steps", "stalls", "transfers")
DECIMAL = COUNTERS + ("cause",)
HEX = ("done", "tval", "pc", "word")
# The keys each halt reason carries besides the counters and the outcome (docs/rv32-rtl.md).
REQUIRED = {"done": ("done",), "double-fault": ("cause", "tval"), "limit": ()}
# A run's guest transcript and the simulator's own output are kept apart:
# `console` is what the guest printed (the emulator's stdout, or the file the
# testbench writes with +console), `noise` is anything the simulator itself
# printed on stdout (Icarus $fatal, $readmemh, and plusarg messages, Verilator
# %Error/%Fatal), which is empty for a clean run and always empty for the emulator.
Run = namedtuple("Run", "status console noise stderr trace halt")


def compile_testbench(output, iverilog="iverilog"):
    """Compile the testbench and the core for Icarus into `output`."""
    subprocess.run([iverilog, "-g2012", "-Wall", "-s", "rv32_tb", "-o", str(output),
                    str(TESTBENCH), *map(str, RTL_SOURCES)], check=True)


def simulator_command(simulator, image, trace=None, console=None, wave=None, stall=None, seed=None, max_cycles=None):
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


def run_backend(command, trace, parse_halt, timeout, console=None):
    """Run one backend; the trace (and console) files are truncated first so a crash cannot pass.

    With `console`, the guest transcript is read from that file and the process's
    stdout is the simulator's own noise; without it the transcript is stdout and
    there is no noise channel (the emulator). A malformed halt line becomes
    `halt=None` with the reason appended to stderr.
    """
    Path(trace).write_text("")
    if console is not None:
        Path(console).write_text("")
    completed = subprocess.run(command, capture_output=True, timeout=timeout)
    stdout, stderr = decode(completed.stdout), decode(completed.stderr)
    try:
        halt = parse_halt(stderr)
    except ValueError as error:
        halt, stderr = None, f"{stderr}\n{error}\n"
    if console is None:
        transcript, noise = stdout, ""
    else:
        transcript, noise = decode(Path(console).read_bytes()), simulator_noise(stdout)
    return Run(completed.returncode, transcript, noise, stderr, Path(trace).read_text().splitlines(), halt)


def run_rtl(simulator, image_hex, trace, stall=None, seed=None, wave=None, max_cycles=None, timeout=120):
    """Run the testbench on a hex image with the documented plusargs; the console goes next to the trace."""
    console = Path(trace).with_name(Path(trace).name + ".console")
    command = simulator_command(simulator, image_hex, trace=trace, console=console, wave=wave,
                                stall=stall, seed=seed, max_cycles=max_cycles)
    return run_backend(command, trace, rtl_halt_line, timeout, console=console)


def run_emulator(emulator, image_bin, trace, limit=None, timeout=120):
    """Run the emulator on a flat image with a trace, the same way tools/rv32_run_emu.py does."""
    command = emulator_command(emulator, image_bin, trace=trace, limit=limit)
    return run_backend(command, trace, halt_line, timeout)


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
    """A matching trace is not enough: the backend must exit cleanly, print nothing
    of its own, and report `halt=done ... pass`; otherwise exit with everything it printed."""
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


def cycle_relation(rtl):
    """Relate the testbench's cycle count to the trace: 4 cycles per instruction without a data
    access, 5 with one, plus the stalls. Returns (text, holds); `holds` is None when a trap line
    makes the formula inapplicable (a trap costs the cycles up to the state that raised it)."""
    steps = len(rtl.trace)
    memory = sum("mem[" in line for line in rtl.trace)
    traps = sum(" trap " in line for line in rtl.trace)
    halt = rtl.halt
    expected = 4 * (steps - memory) + 5 * memory + halt["stalls"]
    text = (f"cycles {halt['cycles']} = 4 x {steps - memory} + 5 x {memory} + {halt['stalls']} stalls; "
            f"transfers {halt['transfers']} = {steps} fetches + {memory} data")
    if traps:
        return f"{text} (not exact: {traps} trap lines)", None
    return text, halt["cycles"] == expected and halt["transfers"] == steps + memory


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("check", "waves", "bench"), default="check")
    parser.add_argument("--program", choices=sorted(PROGRAMS), default="loop", help="assembled program to run")
    parser.add_argument("--image", type=Path, help="a flat .bin image to run instead of an assembled program")
    parser.add_argument("--expect-console", help="the guest console output both backends must produce")
    parser.add_argument("--emulator", default=DEFAULT_EMULATOR)
    parser.add_argument("--simulator", default=DEFAULT_SIMULATOR, help=".vvp file or Verilator binary")
    parser.add_argument("--out", default=DEFAULT_OUT, help="directory for the image, traces, and VCD")
    parser.add_argument("--stall", type=int, default=None, help="fixed stall cycles per request")
    parser.add_argument("--seed", type=int, default=None, help="random 0..3 stall cycles per request")
    args = parser.parse_args()
    if args.stall is not None and args.stall < 0:
        parser.error("--stall must not be negative")
    if args.stall is not None and args.seed is not None:
        parser.error("--stall and --seed are exclusive")
    for path in (args.emulator, args.simulator):
        if not Path(path).exists() and shutil.which(path) is None:
            parser.error(f"{path} does not exist; run make build-rv32-emu / build-rv32-rtl first")

    out = Path(args.out)
    if args.image is not None:
        if not args.image.exists():
            parser.error(f"{args.image} does not exist; run make check-rv32-image first")
        name = args.image.stem
        out.mkdir(parents=True, exist_ok=True)
        hex_path, bin_path = out / f"{name}.hex", args.image
        hex_path.write_text("".join(f"{word}\n" for word in to_hex_words(bin_path.read_bytes())))
    else:
        name = args.program
        hex_path, bin_path = write_image(PROGRAMS[name](), out, name)
    emulator = run_emulator(args.emulator, bin_path, out / f"{name}.emu.trace")
    check_passed(emulator, "emulator")
    if args.expect_console is not None and emulator.console.rstrip("\n") != args.expect_console:
        sys.exit(f"emulator console {emulator.console!r} is not {args.expect_console!r}")

    if args.mode == "bench":
        print("stall  cycles  stalls  transfers  steps")
        seed = BENCH_SEED if args.seed is None else args.seed
        for stall, seed in [(0, None), (1, None), (2, None), (3, None), (None, seed)]:
            rtl = run_rtl(args.simulator, hex_path, out / f"{name}.rtl.trace", stall=stall, seed=seed)
            check_passed(rtl)
            difference = diff_traces(rtl.trace, emulator.trace)
            if difference:
                sys.exit(f"stall={stall} seed={seed}: {difference}")
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
    rtl = run_rtl(args.simulator, hex_path, out / f"{name}.rtl.trace", stall=stall, seed=args.seed, wave=wave)
    print(emulator.stderr.strip().splitlines()[-1])
    print(rtl.stderr.strip().splitlines()[-1] if rtl.stderr.strip() else "rv32_tb: no halt line")
    check_passed(rtl)
    difference = diff_traces(rtl.trace, emulator.trace)
    if difference:
        sys.exit(f"trace mismatch: {difference}")
    if rtl.console != emulator.console:
        sys.exit(f"console mismatch: RTL {rtl.console!r}, emulator {emulator.console!r}")
    print(f"traces identical: {len(rtl.trace)} lines; {out / f'{name}.rtl.trace'}")
    relation, holds = cycle_relation(rtl)
    print(relation)
    if holds is False:
        sys.exit("the cycle count does not follow the state machine")
    if wave is not None:
        if not wave.exists() or wave.stat().st_size == 0:
            sys.exit(f"the simulator wrote no waveform to {wave}")
        print(f"waveform: {wave}")


if __name__ == "__main__":
    main()
