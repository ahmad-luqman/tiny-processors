#!/usr/bin/env python3
"""Compare the emulator's retired PC sequence with QEMU's for the same ELF.

QEMU with `-accel tcg,one-insn-per-tb=on -d exec,nochain` logs one `Trace`
line per executed instruction. Its sequence contains the virt mask ROM at
0x1000 before our image, and after the guest writes the done register it may
log the guard spin at the next address once or twice before the exit takes
effect. Everything else must match our trace instruction for instruction:
same PCs in the same order. Register values are compared by the self-check
itself, not here.
"""

import argparse
from pathlib import Path
import re
import subprocess
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.rv32_run_qemu import qemu_command  # noqa: E402

TRACE_LINE = re.compile(r"\ATrace \d+: 0x[0-9a-f]+ \[[0-9a-f]+/([0-9a-f]{16})/")
RAM_BASE = 0x80000000


def qemu_pcs(log_text, ram_base=RAM_BASE):
    """PCs of every instruction QEMU executed inside RAM, in order."""
    pcs = []
    for line in log_text.splitlines():
        match = TRACE_LINE.match(line)
        if match:
            pc = int(match.group(1), 16)
            if pc >= ram_base:
                pcs.append(pc)
    return pcs


def trace_pcs(trace_text):
    """PCs from our retirement trace, in order, including trapped instructions."""
    return [int(line.split()[1], 16) for line in trace_text.splitlines() if line]


def compare(ours, theirs):
    """Return None if the sequences agree, else a description of the first difference.

    QEMU keeps executing the guard spin (the instruction after the done store)
    until its exit request takes effect, tens of times in practice; trailing
    repeats of exactly that address are tolerated.
    """
    common = min(len(ours), len(theirs))
    for index in range(common):
        if ours[index] != theirs[index]:
            return (f"instruction {index + 1}: emulator pc {ours[index]:#010x}, QEMU pc {theirs[index]:#010x}; "
                    f"preceding pcs {[f'{pc:#010x}' for pc in ours[max(0, index - 3):index]]}")
    if len(theirs) < len(ours):
        return f"QEMU stopped after {len(theirs)} instructions, the emulator retired {len(ours)}"
    spin = ours[-1] + 4
    extra = theirs[len(ours):]
    if any(pc != spin for pc in extra):
        return f"QEMU executed {len(extra)} extra instruction(s) after our last one: {[f'{pc:#010x}' for pc in extra[:6]]}"
    return None


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("trace", type=Path, help="retirement trace written by the emulator for the same image")
    parser.add_argument("--qemu", default="qemu-system-riscv32")
    parser.add_argument("--cpu", default="rv32i")
    parser.add_argument("--log", type=Path, required=True, help="where to write QEMU's execution log")
    parser.add_argument("--timeout", type=float, default=60.0)
    args = parser.parse_args()
    command = qemu_command(args.qemu, args.elf, args.cpu) + ["-accel", "tcg,one-insn-per-tb=on",
                                                              "-d", "exec,nochain", "-D", str(args.log)]
    args.log.parent.mkdir(parents=True, exist_ok=True)
    args.log.unlink(missing_ok=True)  # never compare against a log from an earlier run
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True, text=True,
                                   timeout=args.timeout)
    except (OSError, subprocess.TimeoutExpired) as error:
        parser.exit(1, f"{args.qemu}: {error}\n")
    print(" ".join(command))
    if completed.returncode != 0:
        parser.exit(1, f"{args.elf}: QEMU exited with status {completed.returncode}, expected 0 (the pass word)\n"
                       f"{completed.stderr}")
    if not args.log.exists():
        parser.exit(1, f"{args.elf}: QEMU wrote no execution log at {args.log}\n{completed.stderr}")
    ours = trace_pcs(args.trace.read_text())
    theirs = qemu_pcs(args.log.read_text())
    if not theirs:
        parser.exit(1, f"{args.elf}: QEMU's log has no instructions inside RAM\n{completed.stderr}")
    problem = compare(ours, theirs)
    if problem:
        parser.exit(1, f"{args.elf}: PC sequences differ: {problem}\n")
    print(f"{len(ours)} instructions: emulator and QEMU executed the same PC sequence "
          f"({len(theirs) - len(ours)} trailing guard-spin repeat(s) in QEMU's log ignored)")


if __name__ == "__main__":
    main()
