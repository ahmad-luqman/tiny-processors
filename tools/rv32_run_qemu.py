#!/usr/bin/env python3
"""Run an RV32 firmware ELF on QEMU's virt board and check the done/console protocol.

QEMU is the independent reference runner for M1: its 16550 UART and sifive_test
device sit at the machine contract's console and done-register addresses, so
the exact firmware image runs unmodified. Guest console bytes arrive on stdout;
QEMU's own diagnostics stay on stderr.

QEMU's stdout is a file, never a pipe (issue #41). The architectural tests store to the UART
without polling LSR, and once a pipe is full the bytes that do not fit never arrive, though QEMU
still exits 0: a reader that fell behind under load lost a 250 KB signature's tail that way. A
file takes every byte, however slowly it is read.
"""

import argparse
import bisect
import io
import itertools
import os
from collections import namedtuple
from pathlib import Path
import re
import select
import subprocess
import tempfile
import time


PASS_LINE = re.compile(r"\APASS ([0-9a-f]{8})\Z")
FAIL_LINE = re.compile(r"\AFAIL ([1-9][0-9]{0,2})\Z")
DEFAULT_CPU = "rv32i"
# If a QEMU release stops booting the bare model, try these in order.
CPU_FALLBACKS = ("rv32i,zicsr=true", "rv32,m=false,a=false,f=false,d=false,c=false")

Outcome = namedtuple("Outcome", "ok reason code checksum")
# A gated run: as run() returns, and how many of the input's lines were fed.
GatedRun = namedtuple("GatedRun", "status stdout stderr timed_out fed lines")


def decode(data):
    return bytes(data or b"").decode("utf-8", errors="backslashreplace")


def qemu_command(qemu, elf, cpu=DEFAULT_CPU, memory="16M", log=None, drive=None, icount=None):
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
    guest's UART receives (O2); without it the UART receives nothing. stdout goes through a file
    (issue #41), so a timed-out run still returns all the console it printed."""
    with tempfile.TemporaryFile() as console, open(stdin, "rb") if stdin is not None else open("/dev/null", "rb") as source:
        try:
            completed = subprocess.run(command, stdin=source, stdout=console, stderr=subprocess.PIPE, timeout=timeout)
            status, stderr, timed_out = completed.returncode, completed.stderr, False
        except subprocess.TimeoutExpired as expired:  # QEMU has been killed and reaped: the file is whole
            status, stderr, timed_out = None, expired.stderr, True
        console.seek(0)
        return status, decode(console.read()), decode(stderr), timed_out


def input_lines(data):
    """The lines a gated input feeds one at a time, split after each newline only (a lone \\r stays
    inside its line, as on the emulator and the testbench); a last line without one is still a line."""
    return io.BytesIO(data).readlines()


class PromptCounter:
    """Counts the prompt in a byte stream that arrives in pieces, overlapping matches included: the
    rolling tail the emulator's --console-prompt and the testbench's +console-prompt-hex keep."""

    def __init__(self, prompt):
        self.prompt, self.tail, self.seen = prompt, b"", 0

    def feed(self, chunk):
        for byte in chunk:
            self.tail = (self.tail + bytes([byte]))[-len(self.prompt):]
            self.seen += self.tail == self.prompt


PROMPT_MAX = 64  # bytes: the emulator's --console-prompt and the testbench's +console-prompt-hex allow 1 to 64


def run_gated(command, timeout, stdin, prompt):
    """Like run(), but the guest receives line k of `stdin` only once it has printed `prompt` k times
    (issue #36), so a shell sees each command when it is waiting for one, as the emulator's
    --console-prompt and the testbench's +console-prompt-hex arrange. The console file is polled and
    input written as the pipe allows, in one select loop, so the timeout holds even when the guest
    stops reading; QEMU's stdin stays open. Returns a GatedRun; a line counts as fed once its last byte
    is written."""
    lines = input_lines(Path(stdin).read_bytes())
    ends = list(itertools.accumulate(len(line) for line in lines))  # the byte offset past each line
    prompts = PromptCounter(prompt.encode())
    console = tempfile.TemporaryFile()  # not a pipe: see the module docstring (issue #41)
    process = subprocess.Popen(command, stdin=subprocess.PIPE, stdout=console, stderr=subprocess.PIPE)
    out, err, pending, released, written, closed = bytearray(), bytearray(), bytearray(), 0, 0, False
    inp, errors = process.stdin.fileno(), process.stderr.fileno()
    os.set_blocking(inp, False)
    fed = lambda: bisect.bisect_right(ends, written)  # noqa: E731

    def drain():  # the console bytes QEMU has written since the last call; pread leaves QEMU's offset alone
        while chunk := os.pread(console.fileno(), 65536, len(out)):
            out.extend(chunk)
            prompts.feed(chunk)

    try:
        deadline = time.monotonic() + timeout
        while (left := deadline - time.monotonic()) > 0:
            drain()
            status = process.poll()
            if status is not None:
                drain()
                while errors is not None and (chunk := os.read(errors, 65536)):
                    err.extend(chunk)
                return GatedRun(status, decode(out), decode(err), False, fed(), len(lines))
            while released < len(lines) and prompts.seen > released:
                pending += lines[released]
                released += 1
            wanted = [inp] if pending and not closed else []
            # A file is always readable, so the console is polled: stderr and stdin wake the loop early.
            readable, writable, _ = select.select([errors] if errors is not None else [], wanted, [], min(left, 0.05))
            if readable:
                chunk = os.read(errors, 65536)
                if chunk:
                    err.extend(chunk)
                else:
                    errors = None
            if writable:
                try:
                    count = os.write(inp, pending)
                    del pending[:count]
                    written += count
                except BlockingIOError:
                    pass
                except BrokenPipeError:  # QEMU has gone; classify_status says which line it missed
                    closed = True
        drain()
        return GatedRun(None, decode(out), decode(err), True, fed(), len(lines))
    finally:
        if process.poll() is None:  # a timeout, an interrupt or an error: never leave QEMU running
            process.kill()
            process.wait()
        console.close()


def classify_status(status, transcript, timed_out, fed=None, lines=None):
    """--status-only: the verdict is QEMU's exit status, 0 for the done register's pass word (with
    -no-reboot a guest reset also exits 0, which is why the caller compares the transcript too), and
    with a gated input every line must have been fed."""
    count = len(transcript.replace("\r", "").splitlines())
    waiting = f"; line {fed + 1} of {lines} was never fed" if lines is not None and fed < lines else ""
    if timed_out:
        return Outcome(False, f"timed out with {count} console line(s){waiting}", None, None)
    if status != 0:
        return Outcome(False, f"QEMU exit status {status}, not the pass word's 0{waiting}", status, None)
    if waiting:
        return Outcome(False, f"QEMU exited 0 before the input was all fed{waiting}", 0, None)
    return Outcome(True, f"QEMU exit status 0 (the pass word) after {count} console lines", 0, None)


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
    parser.add_argument("--memory", default="16M", help="QEMU RAM; keep equal to the contract RAM")
    parser.add_argument("--timeout", type=float, default=20.0)
    parser.add_argument("--transcript", type=Path, help="write the guest console output here")
    parser.add_argument("--qemu-log", type=Path, help="enable QEMU guest error logging to this file")
    parser.add_argument("--expect-hex", help="checksum the PASS line must carry")
    parser.add_argument("--last-line", action="store_true", help="judge the last console line; earlier lines are a report")
    parser.add_argument("--stdin", type=Path, help="bytes the guest's UART receives (O2)")
    parser.add_argument("--drive", type=Path, help="a raw disk image for virtio-blk (O3); the guest may write it")
    parser.add_argument("--prompt", help="feed --stdin a line at a time, line k once the guest has printed this k times (issue #36)")
    parser.add_argument("--status-only", action="store_true",
                        help="for a guest such as Linux that prints no PASS line: judge QEMU's exit status (0 is the done "
                             "register's pass word, though a guest reset exits 0 too, so the caller compares the transcript), "
                             "and with --prompt require every line to have been fed")
    parser.add_argument("--icount", type=int, choices=range(11), metavar="N",
                        help="instruction-counted time: each instruction advances virtual time 2**N ns, and wfi skips ahead")
    args = parser.parse_args()
    if args.prompt is not None and args.stdin is None:
        parser.error("--prompt gates --stdin, which is missing")
    if args.prompt is not None and not 1 <= len(args.prompt.encode()) <= PROMPT_MAX:
        parser.error(f"--prompt must be 1 to {PROMPT_MAX} bytes, as on the emulator and the testbench")
    command = qemu_command(args.qemu, args.elf, cpu=args.cpu, memory=args.memory, log=args.qemu_log, drive=args.drive,
                           icount=args.icount)
    fed = lines = None
    try:
        if args.prompt is not None:
            status, transcript, diagnostics, timed_out, fed, lines = run_gated(command, args.timeout, args.stdin, args.prompt)
        else:
            status, transcript, diagnostics, timed_out = run(command, args.timeout, args.stdin)
    except OSError as error:
        parser.exit(1, f"{args.qemu}: {error}\n")
    if args.transcript:
        args.transcript.parent.mkdir(parents=True, exist_ok=True)
        args.transcript.write_text(transcript)
    if args.status_only:
        outcome = classify_status(status, transcript, timed_out, fed, lines)
    else:
        outcome = classify(status, transcript, timed_out, args.expect_hex, args.last_line)
    print(" ".join(command))
    print(transcript, end="" if transcript.endswith("\n") else "\n")
    if not outcome.ok:
        parser.exit(1, f"{args.elf}: {outcome.reason}\n{diagnostics}")
    if args.status_only:
        print(outcome.reason)
    else:
        print(f"QEMU exit status {status}; console and done register agree: {outcome.reason} {outcome.checksum}")


if __name__ == "__main__":
    main()
