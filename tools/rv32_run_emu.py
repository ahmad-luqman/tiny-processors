#!/usr/bin/env python3
"""Run a flat RV32 firmware image on our emulator and check the done/console protocol.

Mirrors tools/rv32_run_qemu.py so the same `classify` decides the outcome from
the single console line and the exit status. The emulator additionally reports
how it halted on its last stderr line; that line must agree with the status,
because a guest `FAIL 2` and an emulator error both exit with status 2.
"""

import argparse
from pathlib import Path
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.rv32_run_qemu import classify  # noqa: E402

DEFAULT_EMULATOR = "build/rv32/rv32emu"
COUNTERS = ("steps", "retired", "traps", "loaded")


def halt_line(stderr):
    """Parse the emulator's final `rv32emu: halt=... ` line into a dict, or None if absent."""
    lines = [line for line in stderr.splitlines() if line.startswith("rv32emu: halt=")]
    if not lines:
        return None
    fields, outcome = {}, []
    for token in lines[-1].split()[1:]:
        key, _, value = token.partition("=")
        if key == "halt":
            fields[key] = value
        elif key in COUNTERS:
            fields[key] = int(value)
        elif key == "done" and not outcome:
            fields[key] = int(value, 16)
        else:
            outcome.append(token)
    fields["outcome"] = " ".join(outcome)
    return fields


def emulator_command(emulator, image, trace=None, state=None, limit=None):
    command = [str(emulator), "--image", str(image)]
    if trace is not None:
        command += ["--trace", str(trace)]
    if state is not None:
        command += ["--dump-state", str(state)]
    if limit is not None:
        command += ["--max-instructions", str(limit)]
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
