#!/usr/bin/env python3
"""Run a flat RV32 firmware image on our emulator and check the done/console protocol.

Mirrors tools/rv32_run_qemu.py so the same `classify` decides the outcome from
the single console line and the exit status. The emulator additionally reports
how it halted on its last stderr line; that line must agree with the status,
because a guest `FAIL 2` and an emulator error both exit with status 2.
"""

import argparse
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_run_qemu import classify  # noqa: E402

DEFAULT_EMULATOR = "build/rv32/rv32emu"
EMULATOR_SOURCE = ROOT / "tools" / "rv32emu.c"
EMULATOR_CFLAGS = ("-std=c11", "-O2", "-Wall", "-Wextra", "-Werror")  # the Makefile's RV32EMU_CFLAGS
COUNTERS = ("steps", "retired", "traps", "loaded")


def build_emulator(output):
    """Compile tools/rv32emu.c into `output` with the Makefile's flags; HOST_CC picks the compiler."""
    compiler = os.environ.get("HOST_CC", "cc")
    subprocess.run([compiler, *EMULATOR_CFLAGS, "-o", str(output), str(EMULATOR_SOURCE)], check=True)


def last_halt_line(stderr, prefix):
    """The last `<prefix> halt=...` line of `stderr`, or None."""
    lines = [line for line in stderr.splitlines() if line.startswith(f"{prefix} halt=")]
    return lines[-1] if lines else None


def parse_halt_line(line, decimal, hexadecimal):
    """Split one halt line into a dict: `halt`, the typed keys, and the untyped rest as `outcome`.

    A typed key whose value does not parse raises ValueError naming the line, so
    a truncated line or an `x` from the RTL is reported rather than swallowed.
    """
    fields, outcome = {}, []
    for token in line.split()[1:]:
        key, _, value = token.partition("=")
        try:
            if key == "halt":
                fields[key] = value
            elif key in decimal and not outcome:
                fields[key] = int(value)
            elif key in hexadecimal and not outcome:
                fields[key] = int(value, 16)
            else:
                outcome.append(token)
        except ValueError:
            raise ValueError(f"malformed halt line field {token!r} in {line!r}") from None
    if "halt" not in fields or not fields["halt"]:
        raise ValueError(f"halt line without a reason: {line!r}")
    fields["outcome"] = " ".join(outcome)
    return fields


def halt_line(stderr):
    """Parse the emulator's final `rv32emu: halt=... ` line into a dict, or None if absent."""
    line = last_halt_line(stderr, "rv32emu:")
    return None if line is None else parse_halt_line(line, COUNTERS, ("done",))


def emulator_command(emulator, image, trace=None, state=None, limit=None, checkpoints=None, input_script=None,
                     frames=None):
    command = [str(emulator), "--image", str(image)]
    if trace is not None:
        command += ["--trace", str(trace)]
    if state is not None:
        command += ["--dump-state", str(state)]
    if limit is not None:
        command += ["--max-instructions", str(limit)]
    if checkpoints is not None:
        command += ["--checkpoints", str(checkpoints)]
    if input_script is not None:
        command += ["--input", str(input_script)]
    if frames is not None:
        command += ["--frames", str(frames)]
    return command


def count(text):
    value = int(text, 0)
    if value < 0:
        raise argparse.ArgumentTypeError(f"{text} is negative")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("image", type=Path, help="flat image loaded at 0x80000000 (build/rv32/*.bin)")
    parser.add_argument("--emulator", default=DEFAULT_EMULATOR)
    parser.add_argument("--trace", type=Path, help="write the retirement trace here")
    parser.add_argument("--state", type=Path, help="write the final architectural state here")
    parser.add_argument("--transcript", type=Path, help="write the guest console output here")
    parser.add_argument("--max-instructions", type=count)
    parser.add_argument("--timeout", type=float, default=60.0, help="seconds before the run is abandoned")
    parser.add_argument("--expect-hex", help="checksum the PASS line must carry")
    args = parser.parse_args()
    command = emulator_command(args.emulator, args.image, args.trace, args.state, args.max_instructions)
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=args.timeout)
    except OSError as error:
        parser.exit(1, f"{args.emulator}: {error}\n")
    except subprocess.TimeoutExpired:
        parser.exit(1, f"{args.image}: the emulator did not finish within {args.timeout} s\n")
    if args.transcript:
        args.transcript.parent.mkdir(parents=True, exist_ok=True)
        args.transcript.write_text(completed.stdout)
    print(" ".join(command))
    print(completed.stdout, end="" if completed.stdout.endswith("\n") else "\n")
    halt = halt_line(completed.stderr)
    if halt is None or halt["halt"] != "done":
        parser.exit(1, f"{args.image}: the emulator did not reach the done register\n{completed.stderr}")
    outcome = classify(completed.returncode, completed.stdout, False, args.expect_hex)
    if not outcome.ok:
        parser.exit(1, f"{args.image}: {outcome.reason}\n{completed.stderr}")
    print(f"emulator exit status {completed.returncode}; console and done register agree: "
          f"{outcome.reason} {outcome.checksum}; {halt['retired']} instructions retired, {halt['traps']} traps")


if __name__ == "__main__":
    main()
