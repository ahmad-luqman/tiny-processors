"""A SHELL for make that times each recipe line and keeps parallel output readable.

GNU Make 3.81 (the macOS default) has no --output-sync, so `make -j` interleaves the logs of
concurrent recipes. With RV32_TIMING=1 the Makefile sets SHELL to this script with the target name
as the first argument. Each recipe line then runs under /bin/sh with its output captured. When the
line finishes, its output is printed in one block under a lock. A JSON line with the target, the
command, the start and end times and the exit status is appended to $RV32_TIMING_LOG, when that
variable is set.

    python3 tools/rv32_recipe_shell.py --report build/rv32-timing.jsonl

run from the repository root, prints the per-target table from such a log. Each target's time
runs from its first line's start to its last line's end, and the longest targets come first.
"""
import fcntl
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path

LOCK = Path(tempfile.gettempdir()) / f"rv32-recipe-shell-{os.getuid()}.lock"


def emit(text):
    """Write `text` to stdout in one piece, so no other recipe's block lands inside it."""
    with open(LOCK, "a") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        sys.stdout.write(text)
        sys.stdout.flush()


def untimed_environment():
    """The recipe's environment without RV32_TIMING. A make that a recipe starts itself (a test
    reading `make print-rv32-os-layout`, Verilator's generated makefiles) then runs plainly, and its
    output is not wrapped in banners."""
    env = dict(os.environ)
    env.pop("RV32_TIMING", None)
    if "MAKEFLAGS" in env:
        env["MAKEFLAGS"] = re.sub(r"(^|\s)RV32_TIMING=\S*", "", env["MAKEFLAGS"]).strip()
    return env


def run(target, flag, command):
    shown = command if len(command) <= 240 else command[:237] + "..."
    emit(f">> {target}: {shown}\n")
    start = time.time()
    with tempfile.TemporaryFile() as output:
        status = subprocess.run(["/bin/sh", flag, command], stdout=output, stderr=subprocess.STDOUT,
                                env=untimed_environment()).returncode
        end = time.time()
        output.seek(0)
        captured = output.read().decode(errors="replace")
    if captured and not captured.endswith("\n"):
        captured += "\n"
    verdict = "ok" if status == 0 else f"FAILED (exit {status})"
    emit(f"{captured}<< {target}: {end - start:.1f} s {verdict}\n")
    log = os.environ.get("RV32_TIMING_LOG")
    if log:
        record = json.dumps({"target": target, "cwd": os.getcwd(), "command": command, "start": start, "end": end,
                             "status": status})
        with open(log, "a") as stream:  # one short append per line: atomic for concurrent writers
            stream.write(record + "\n")
    return status


def report(log):
    """Per-target wall time, longest first, and the run's span. A target is keyed by its directory as
    well as its name: Verilator's generated makefiles inherit this SHELL and reuse names such as
    verilated.o in every build directory."""
    targets, busy, here = {}, 0.0, os.getcwd()
    for line in Path(log).read_text().splitlines():
        record = json.loads(line)
        cwd = record.get("cwd", here)
        name = record["target"] if cwd == here else f"{os.path.relpath(cwd, here)}/{record['target']}"
        first, last, lines, failed = targets.get(name, (record["start"], record["end"], 0, False))
        targets[name] = (min(first, record["start"]), max(last, record["end"]), lines + 1,
                         failed or record["status"] != 0)
        if cwd == here:  # a line in another directory runs inside one of ours (a Verilator build)
            busy += record["end"] - record["start"]
    if not targets:
        sys.exit(f"{log} has no records")
    begin = min(first for first, _, _, _ in targets.values())
    finish = max(last for _, last, _, _ in targets.values())
    print("| target | seconds | lines |")
    print("|---|---:|---:|")
    for name, (first, last, lines, failed) in sorted(targets.items(), key=lambda item: item[1][0] - item[1][1]):
        print(f"| `{name}`{' (failed)' if failed else ''} | {last - first:.1f} | {lines} |")
    print(f"\n{len(targets)} targets; {busy:.0f} s of recipe time in {finish - begin:.0f} s of wall time")


def main(argv):
    if len(argv) == 2 and argv[0] == "--report":
        report(argv[1])
        return 0
    # make passes `<target> -c <line>`. A $(shell ...) call has no target, so $@ expands to nothing
    # and only `-c <line>` arrives: run it untouched, since its output is a value, not a log.
    if len(argv) == 2:
        os.execv("/bin/sh", ["/bin/sh", *argv])
    if len(argv) != 3:
        sys.exit(f"usage: rv32_recipe_shell.py TARGET -c COMMAND | --report LOG (got {argv!r})")
    return run(*argv)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
