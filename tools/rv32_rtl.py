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
import re
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_asm import program_loop, words_to_bytes, words_to_hex  # noqa: E402
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
REQUIRED = {"done": ("done",), "fault": ("cause", "tval"), "unsupported": ("pc", "word"), "limit": ()}
# Simulator diagnostics land on stdout (Icarus $fatal, $readmemh, and plusarg
# messages; Verilator %Error/%Fatal); any such line fails a run whatever the status.
DIAGNOSTIC = re.compile(r"^(FATAL:|ERROR:|WARNING:|VCD Error|%(Error|Fatal|Warning)|\[\d+( \w+)?\] %)")

Run = namedtuple("Run", "status stdout stderr trace halt")


def compile_testbench(output, iverilog="iverilog"):
    """Compile the testbench and the core for Icarus into `output`."""
    subprocess.run([iverilog, "-g2012", "-Wall", "-s", "rv32_tb", "-o", str(output),
                    str(TESTBENCH), *map(str, RTL_SOURCES)], check=True)


def simulator_command(simulator, image, trace=None, wave=None, stall=None, seed=None, max_cycles=None):
    """The command line for a compiled testbench: `vvp` for a .vvp file, else a Verilator binary."""
    simulator = Path(simulator)
    if simulator.suffix == ".vvp":
        command = ["vvp", str(simulator)]
    else:
        command = [str(simulator), "+verilator+quiet"]  # no simulation report on stdout
    command.append(f"+image={image}")
    if trace is not None:
        command.append(f"+trace={trace}")
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
    `tval`; `pc` and `word`; none for a limit); anything else raises ValueError
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


def diagnostics(stdout):
    """Simulator diagnostic lines that a run printed on stdout."""
    return [line for line in stdout.splitlines() if DIAGNOSTIC.match(line)]


def run_backend(command, trace, parse_halt, timeout):
    """Run one backend; the trace file is truncated first so a crash cannot pass.

    Output is captured as bytes and decoded leniently: a guest may print any
    byte, and a run must be reported rather than raise from inside subprocess.
    A malformed halt line becomes `halt=None` with the reason appended to stderr.
    """
    Path(trace).write_text("")
    completed = subprocess.run(command, capture_output=True, timeout=timeout)
    stdout = completed.stdout.decode("utf-8", errors="backslashreplace")
    stderr = completed.stderr.decode("utf-8", errors="backslashreplace")
    try:
        halt = parse_halt(stderr)
    except ValueError as error:
        halt, stderr = None, f"{stderr}\n{error}\n"
    return Run(completed.returncode, stdout, stderr, Path(trace).read_text().splitlines(), halt)


def run_rtl(simulator, image_hex, trace, stall=None, seed=None, wave=None, max_cycles=None, timeout=120):
    """Run the testbench on a hex image with the documented plusargs."""
    command = simulator_command(simulator, image_hex, trace=trace, wave=wave,
                                stall=stall, seed=seed, max_cycles=max_cycles)
    return run_backend(command, trace, rtl_halt_line, timeout)


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
    return f"status {run.status}\n--- stdout ---\n{run.stdout}--- stderr ---\n{run.stderr}"


def check_passed(run, name="simulator"):
    """A matching trace is not enough: the backend must exit cleanly, print no
    diagnostic, and report `halt=done ... pass`; otherwise exit with everything it printed."""
    noise = diagnostics(run.stdout)
    if run.status != 0 or noise:
        sys.exit(f"{name} failed ({'; '.join(noise) or 'no diagnostic'}):\n{describe(run)}")
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


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--mode", choices=("check", "waves", "bench"), default="check")
    parser.add_argument("--emulator", default=DEFAULT_EMULATOR)
    parser.add_argument("--simulator", default=DEFAULT_SIMULATOR, help=".vvp file or Verilator binary")
    parser.add_argument("--out", default=DEFAULT_OUT, help="directory for the loop image, traces, and VCD")
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
    hex_path, bin_path = write_image(program_loop(), out, "loop")
    emulator = run_emulator(args.emulator, bin_path, out / "loop.emu.trace")
    check_passed(emulator, "emulator")

    if args.mode == "bench":
        print("stall  cycles  stalls  transfers  steps")
        seed = BENCH_SEED if args.seed is None else args.seed
        for stall, seed in [(0, None), (1, None), (2, None), (3, None), (None, seed)]:
            rtl = run_rtl(args.simulator, hex_path, out / "loop.rtl.trace", stall=stall, seed=seed)
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
    wave = out / "loop.vcd" if args.mode == "waves" else None
    rtl = run_rtl(args.simulator, hex_path, out / "loop.rtl.trace", stall=stall, seed=args.seed, wave=wave)
    print(emulator.stderr.strip().splitlines()[-1])
    print(rtl.stderr.strip().splitlines()[-1] if rtl.stderr.strip() else "rv32_tb: no halt line")
    check_passed(rtl)
    difference = diff_traces(rtl.trace, emulator.trace)
    if difference:
        sys.exit(f"trace mismatch: {difference}")
    print(f"traces identical: {len(rtl.trace)} lines; {out / 'loop.rtl.trace'}")
    if wave is not None:
        if not wave.exists() or wave.stat().st_size == 0:
            sys.exit(f"the simulator wrote no waveform to {wave}")
        print(f"waveform: {wave}")


if __name__ == "__main__":
    main()
