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


def qemu_command(qemu, elf, cpu=DEFAULT_CPU, memory="8M", log=None, drive=None, icount=None):
    command = [qemu, "-M", "virt", "-cpu", cpu, "-bios", "none", "-kernel", str(elf), "-m", memory,
               "-nographic", "-monitor", "none", "-no-reboot"]
    # Virtual time advances 2**icount ns per instruction instead of following the host, and an idle
    # wfi skips to the next deadline: whether the timer interrupts a program (the OS's "preempted")
    # no longer depends on how fast the host is. Console input still arrives on host time.
    if icount is not None:
        command += ["-icount", f"shift={icount},sleep=off"]
    if drive is not None:  # O3: a virtio-blk disk in virt's first virtio slot, modern (version 2) transport
        command += ["-global", "virtio-mmio.force-legacy=false", "-drive", f"file={drive},if=none,format=raw,id=disk0",
                    "-device", "virtio-blk-device,drive=disk0,bus=virtio-mmio-bus.0"]
    if log is not None:
        command += ["-d", "guest_errors,unimp", "-D", str(log)]
    return command


def run(command, timeout, stdin=None):
    """Return (exit status or None, stdout, stderr, timed_out). `stdin` is a file whose bytes the
    guest's UART receives (O2); without it the UART receives nothing."""
    try:
        with open(stdin, "rb") if stdin is not None else open("/dev/null", "rb") as source:
            completed = subprocess.run(command, stdin=source, capture_output=True, timeout=timeout)
        completed.stdout = completed.stdout.decode("utf-8", errors="backslashreplace")
        completed.stderr = completed.stderr.decode("utf-8", errors="backslashreplace")
    except subprocess.TimeoutExpired as expired:
        decode = lambda data: (data or b"").decode("utf-8", errors="backslashreplace")  # noqa: E731
        return None, decode(expired.stdout), decode(expired.stderr), True
    return completed.returncode, completed.stdout, completed.stderr, False


def classify(status, transcript, timed_out, expect_hex=None, last_line=False):
    """Decide whether a run satisfied the contract: one line, matching exit status. With
    `last_line`, a program may print a report first and only its last line is the verdict, as
    for the diagnostic and the platform check."""
    lines = transcript.replace("\r", "").splitlines()
    if timed_out:
        return Outcome(False, f"timed out with {len(lines)} console line(s)", None, None)
    if last_line and lines:
        lines = lines[-1:]
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
    parser.add_argument("--memory", default="8M", help="QEMU RAM; keep equal to the contract RAM")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--transcript", type=Path, help="write the guest console output here")
    parser.add_argument("--qemu-log", type=Path, help="enable QEMU guest error logging to this file")
    parser.add_argument("--expect-hex", help="checksum the PASS line must carry")
    parser.add_argument("--last-line", action="store_true", help="judge the last console line; earlier lines are a report")
    parser.add_argument("--stdin", type=Path, help="bytes the guest's UART receives (O2)")
    parser.add_argument("--drive", type=Path, help="a raw disk image for virtio-blk (O3); the guest may write it")
    parser.add_argument("--icount", type=int, choices=range(11), metavar="N",
                        help="instruction-counted time: each instruction advances virtual time 2**N ns, and wfi skips ahead")
    args = parser.parse_args()
    command = qemu_command(args.qemu, args.elf, cpu=args.cpu, memory=args.memory, log=args.qemu_log, drive=args.drive,
                           icount=args.icount)
    try:
        status, transcript, diagnostics, timed_out = run(command, args.timeout, args.stdin)
    except OSError as error:
        parser.exit(1, f"{args.qemu}: {error}\n")
    if args.transcript:
        args.transcript.parent.mkdir(parents=True, exist_ok=True)
        args.transcript.write_text(transcript)
    outcome = classify(status, transcript, timed_out, args.expect_hex, args.last_line)
    print(" ".join(command))
    print(transcript, end="" if transcript.endswith("\n") else "\n")
    if not outcome.ok:
        parser.exit(1, f"{args.elf}: {outcome.reason}\n{diagnostics}")
    print(f"QEMU exit status {status}; console and done register agree: {outcome.reason} {outcome.checksum}")


if __name__ == "__main__":
    main()
