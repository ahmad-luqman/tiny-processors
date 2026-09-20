#!/usr/bin/env python3
"""Run the RV32 RTL testbench and the emulator on one image and diff their traces.

The testbench (tests/rv32_tb.sv) prints the retirement trace in the emulator's
format, so agreement is a plain line-for-line comparison. This module holds the
pieces the tests and the Make targets share: compiling the testbench, running
either simulator with the documented plusargs, parsing the testbench's halt
line, and reporting the first trace difference with context.
"""

import argparse
from collections import namedtuple
from pathlib import Path
import shutil
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_asm import program_loop, words_to_bytes, words_to_hex  # noqa: E402
from tools.rv32_run_emu import emulator_command, halt_line  # noqa: E402

RTL_SOURCES = [ROOT / "rtl" / "rv32" / name
               for name in ("rv32_regfile.v", "rv32_alu.v", "rv32_decode.v", "rv32.v")]
TESTBENCH = ROOT / "tests" / "rv32_tb.sv"
DEFAULT_EMULATOR = "build/rv32/rv32emu"
DEFAULT_SIMULATOR = "build/rv32/rv32_tb.vvp"
DEFAULT_OUT = "build/rv32/rtl"
DECIMAL = ("cycles", "steps", "stalls", "transfers", "cause")
HEX = ("done", "tval", "pc", "word")

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
    """Parse the testbench's final `rv32_tb: halt=...` line into a dict, or None if absent."""
    lines = [line for line in stderr.splitlines() if line.startswith("rv32_tb: halt=")]
    if not lines:
        return None
    fields, outcome = {}, []
    for token in lines[-1].split()[1:]:
        key, _, value = token.partition("=")
        if key == "halt":
            fields[key] = value
        elif key in DECIMAL:
            fields[key] = int(value)
        elif key in HEX:
            fields[key] = int(value, 16)
        else:
            outcome.append(token)
    fields["outcome"] = " ".join(outcome)
    return fields


def run_rtl(simulator, image_hex, trace, stall=None, seed=None, wave=None, max_cycles=None, timeout=120):
    """Run the testbench on a hex image; the trace file is truncated first so a crash cannot pass."""
    Path(trace).write_text("")
    command = simulator_command(simulator, image_hex, trace, wave, stall, seed, max_cycles)
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    return Run(completed.returncode, completed.stdout, completed.stderr,
               Path(trace).read_text().splitlines(), rtl_halt_line(completed.stderr))


def run_emulator(emulator, image_bin, trace, limit=None, timeout=120):
    """Run the emulator on a flat image with a trace, the same way tools/rv32_run_emu.py does."""
    Path(trace).write_text("")
    command = emulator_command(emulator, image_bin, trace=trace, limit=limit)
    completed = subprocess.run(command, capture_output=True, text=True, timeout=timeout)
    return Run(completed.returncode, completed.stdout, completed.stderr,
               Path(trace).read_text().splitlines(), halt_line(completed.stderr))


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


def check_passed(rtl):
    """A matching trace is not enough: the simulator must exit cleanly and report done/pass."""
    if rtl.status != 0:
        sys.exit(f"simulator exited with status {rtl.status}:\n{rtl.stderr}")
    if rtl.halt is None:
        sys.exit(f"simulator printed no halt line:\n{rtl.stderr}")
    if (rtl.halt["halt"], rtl.halt["outcome"]) != ("done", "pass"):
        sys.exit(f"run did not pass: {rtl.halt}")


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
    for path in (args.emulator, args.simulator):
        if not Path(path).exists() and shutil.which(path) is None:
            parser.error(f"{path} does not exist; run make build-rv32-emu / build-rv32-rtl first")

    out = Path(args.out)
    hex_path, bin_path = write_image(program_loop(), out, "loop")
    emulator = run_emulator(args.emulator, bin_path, out / "loop.emu.trace")
    if emulator.halt is None or emulator.halt["halt"] != "done":
        sys.exit(f"emulator did not finish the loop:\n{emulator.stderr}")

    if args.mode == "bench":
        print("stall  cycles  stalls  transfers  steps")
        settings = [(stall, None) for stall in (0, 1, 2, 3)] + [(None, args.seed if args.seed is not None else 7)]
        for stall, seed in settings:
            rtl = run_rtl(args.simulator, hex_path, out / "loop.rtl.trace", stall=stall, seed=seed)
            difference = diff_traces(rtl.trace, emulator.trace)
            if difference:
                sys.exit(f"stall={stall} seed={seed}: {difference}")
            check_passed(rtl)
            label = f"seed {seed}" if seed is not None else str(stall)
            print(f"{label:>6}  {rtl.halt['cycles']:>6}  {rtl.halt['stalls']:>6}  "
                  f"{rtl.halt['transfers']:>9}  {rtl.halt['steps']:>5}")
        return

    stall = args.stall if args.stall is not None else (2 if args.mode == "waves" else 0)
    wave = out / "loop.vcd" if args.mode == "waves" else None
    rtl = run_rtl(args.simulator, hex_path, out / "loop.rtl.trace", stall=stall if args.seed is None else None,
                  seed=args.seed, wave=wave)
    difference = diff_traces(rtl.trace, emulator.trace)
    print(emulator.stderr.strip().splitlines()[-1])
    print(rtl.stderr.strip().splitlines()[-1] if rtl.stderr.strip() else "rv32_tb: no halt line")
    if difference:
        sys.exit(f"trace mismatch: {difference}")
    check_passed(rtl)
    print(f"traces identical: {len(rtl.trace)} lines; {out / 'loop.rtl.trace'}")
    if wave is not None:
        print(f"waveform: {wave}")


if __name__ == "__main__":
    main()
