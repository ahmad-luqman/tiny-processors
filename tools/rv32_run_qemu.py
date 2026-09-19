#!/usr/bin/env python3
"""Run an RV32 firmware ELF on QEMU's virt board and check the done/console protocol.

QEMU is the independent reference runner for M1: its 16550 UART and sifive_test
device sit at the machine contract's console and done-register addresses, so
the exact firmware image runs unmodified. Guest console bytes arrive on stdout;
QEMU's own diagnostics stay on stderr.
"""

import argparse
from collections import namedtuple
from pathlib import Path
import re
import subprocess


PASS_LINE = re.compile(r"\APASS ([0-9a-f]{8})\Z")
FAIL_LINE = re.compile(r"\AFAIL ([1-9][0-9]{0,2})\Z")
DEFAULT_CPU = "rv32i"
# If a QEMU release stops booting the bare model, try these in order.
CPU_FALLBACKS = ("rv32i,zicsr=true", "rv32,m=false,a=false,f=false,d=false,c=false")

Outcome = namedtuple("Outcome", "ok reason code checksum")


def qemu_command(qemu, elf, cpu=DEFAULT_CPU, memory="4M", log=None):
    command = [qemu, "-M", "virt", "-cpu", cpu, "-bios", "none", "-kernel", str(elf), "-m", memory,
               "-nographic", "-monitor", "none", "-no-reboot"]
    if log is not None:
        command += ["-d", "guest_errors,unimp", "-D", str(log)]
    return command


def run(command, timeout):
    """Return (exit status or None, stdout, stderr, timed_out)."""
    try:
        completed = subprocess.run(command, stdin=subprocess.DEVNULL, capture_output=True,
                                   text=True, timeout=timeout)
    except subprocess.TimeoutExpired as expired:
        return None, expired.stdout or "", expired.stderr or "", True
    return completed.returncode, completed.stdout, completed.stderr, False


def classify(status, transcript, timed_out, expect_hex=None):
    """Decide whether a run satisfied the contract: one line, matching exit status."""
    lines = transcript.replace("\r", "").splitlines()
    if timed_out:
        return Outcome(False, f"timed out with {len(lines)} console line(s)", None, None)
    if len(lines) != 1:
        return Outcome(False, f"expected exactly one console line, got {len(lines)}", None, None)
    passed = PASS_LINE.match(lines[0])
    if passed:
        checksum = passed.group(1)
        if status != 0:
            return Outcome(False, f"guest reported PASS but QEMU exit status was {status}", status, checksum)
        if expect_hex is not None and checksum != expect_hex.lower():
            return Outcome(False, f"checksum {checksum} differs from expected {expect_hex.lower()}", 0, checksum)
        return Outcome(True, "pass", 0, checksum)
    failed = FAIL_LINE.match(lines[0])
    if failed:
        code = int(failed.group(1))
        if status != code:
            return Outcome(False, f"guest reported FAIL {code} but QEMU exit status was {status}", code, None)
        return Outcome(False, f"guest check {code} failed", code, None)
    return Outcome(False, f"unrecognized console line {lines[0]!r}", None, None)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("--qemu", default="qemu-system-riscv32")
    parser.add_argument("--cpu", default=DEFAULT_CPU)
    parser.add_argument("--memory", default="4M", help="QEMU RAM; keep equal to the contract RAM")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--transcript", type=Path, help="write the guest console output here")
    parser.add_argument("--qemu-log", type=Path, help="enable QEMU guest error logging to this file")
    parser.add_argument("--expect-hex", help="checksum the PASS line must carry")
    args = parser.parse_args()
    command = qemu_command(args.qemu, args.elf, args.cpu, args.memory, args.qemu_log)
    try:
        status, transcript, diagnostics, timed_out = run(command, args.timeout)
    except OSError as error:
        parser.exit(1, f"{args.qemu}: {error}\n")
    if args.transcript:
        args.transcript.parent.mkdir(parents=True, exist_ok=True)
        args.transcript.write_text(transcript)
    outcome = classify(status, transcript, timed_out, args.expect_hex)
    print(" ".join(command))
    print(transcript, end="" if transcript.endswith("\n") else "\n")
    if not outcome.ok:
        parser.exit(1, f"{args.elf}: {outcome.reason}\n{diagnostics}")
    print(f"QEMU exit status {status}; console and done register agree: {outcome.reason} {outcome.checksum}")


if __name__ == "__main__":
    main()
