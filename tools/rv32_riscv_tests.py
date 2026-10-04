"""Run riscv-tests' rv32ua ISA tests (issue #34) on our machine.

riscv-arch-test's A suite (tools/rv32_arch_test.py) has the nine AMOs but no LR.W or SC.W test;
riscv-tests' rv32ua has both, lrsc.S among them. Each selected test is assembled against
tests/riscv-tests/riscv_test.h, our bare M-mode environment in place of riscv-tests' own, and
must print PASS and write the pass word on every backend asked for: the emulator, QEMU's virt
board, and the RTL under Icarus or Verilator. The emulator and each RTL run must also retire
identical traces, line for line.

riscv-tests is fetched at a pinned commit by `make fetch-rv32-riscv-tests`: only its licence, the
rv32ua sources (which include rv64ua's) and the scalar test macros. See docs/rv32-a.md.
"""
import argparse
from concurrent.futures import ThreadPoolExecutor
import fnmatch
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_arch_test import BACKENDS, DEFAULT_QEMU_CPU, QEMU_FDT_OFFSET, TestFailure, link_flags, run_qemu  # noqa: E402
from tools.rv32_image import RAM_BASE, flatten, parse_elf, write_hex  # noqa: E402
from tools.rv32_rtl import check_passed, diff_traces, run_emulator, run_rtl  # noqa: E402

# riscv-tests has no release tags; this is master on 2026-09-25.
RISCV_TESTS_URL = "https://github.com/riscv-software-src/riscv-tests.git"
RISCV_TESTS_COMMIT = "bcffa2b3188b040c611f90dc0b6e422f54775a09"
DEFAULT_RISCV_TESTS = ROOT / "third_party" / "riscv-tests"
ENV = ROOT / "tests" / "riscv-tests"
SPARSE = ("/LICENSE", "/isa/rv32ua/", "/isa/rv64ua/", "/isa/macros/scalar/")
MARCH = "rv32ia_zicsr"
# rv32ua tests that are not selected, and why.
EXCLUDED = {
    "amocas_w": "Zacas, not part of A",
    "amocas_d": "Zacas, not part of A",
}
PASS_CONSOLE = "PASS\n"


def checkout_commit(path):
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def fetch(path):
    """Check out the pinned commit, only the files the tests need."""
    def git(*command):
        subprocess.run(["git", "-C", str(path), *command], check=True)
    if (path / ".git").exists() and checkout_commit(path) == RISCV_TESTS_COMMIT and all(
            (path / entry.strip("/")).exists() for entry in SPARSE):
        print(f"{path} is at the pinned {RISCV_TESTS_COMMIT}")
        return
    shutil.rmtree(path, ignore_errors=True)
    path.mkdir(parents=True)
    git("init", "-q")
    git("remote", "add", "origin", RISCV_TESTS_URL)
    git("sparse-checkout", "set", "--no-cone", *SPARSE)
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", RISCV_TESTS_COMMIT)
    git("checkout", "-q", "FETCH_HEAD")
    print(f"fetched riscv-tests {RISCV_TESTS_COMMIT} into {path}")


def select_tests(path, patterns):
    source = path / "isa" / "rv32ua"
    found = sorted(source.glob("*.S"))
    if not found:
        sys.exit(f"no tests in {source}; run make fetch-rv32-riscv-tests")
    return [test for test in found if test.stem not in EXCLUDED
            and (not patterns or any(fnmatch.fnmatch(test.stem, pattern) for pattern in patterns))]


def assemble(test, work, riscv_tests, cc, ld):
    """Assemble and link one test against our environment; returns (elf, bin, hex) paths."""
    elf = work / f"{test.stem}.elf"
    flags = [*link_flags(MARCH, ld), f"-I{ENV}", f"-I{riscv_tests / 'isa' / 'macros' / 'scalar'}"]
    result = subprocess.run([cc, *flags, "-o", str(elf), str(test)], capture_output=True, text=True)
    if result.returncode != 0:
        raise TestFailure(f"assembly failed:\n{result.stderr}")
    parsed = parse_elf(elf.read_bytes())
    entry = parsed.symbols.get("rvtest_entry_point")
    if entry != RAM_BASE:
        raise TestFailure(f"rvtest_entry_point is {entry}, not the reset PC {RAM_BASE:#x}")
    image = flatten(parsed, RAM_BASE)
    if len(image) > QEMU_FDT_OFFSET:
        raise TestFailure(f"the image is {len(image)} bytes; QEMU puts its device tree at {QEMU_FDT_OFFSET:#x} into RAM")
    bin_path, hex_path = elf.with_suffix(".bin"), elf.with_suffix(".hex")
    bin_path.write_bytes(image)
    write_hex(hex_path, image)
    return elf, bin_path, hex_path


def run_one(test, args):
    """Run one test on every backend; returns (name, emulator steps, seconds)."""
    started = time.monotonic()
    work = args.out / test.stem
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    elf, bin_path, hex_path = assemble(test, work, args.riscv_tests, args.cc, args.ld)
    emulator = run_emulator(args.emulator, bin_path, work / "emu.trace", limit=args.limit, timeout=args.timeout)
    check_passed(emulator, "emulator")
    if emulator.console != PASS_CONSOLE:
        raise TestFailure(f"the emulator printed {emulator.console!r}")
    if "qemu" in args.backends:
        console = run_qemu(args, elf)  # QEMU exits nonzero on a fail word, which raises
        if console != PASS_CONSOLE:
            raise TestFailure(f"QEMU printed {console!r}")
    for backend in ("icarus", "verilator"):
        if backend not in args.backends:
            continue
        simulator = args.icarus if backend == "icarus" else args.verilator
        rtl = run_rtl(simulator, hex_path, work / f"{backend}.trace", max_cycles=60 * emulator.halt["steps"] + 10000,
                      timeout=args.timeout)
        check_passed(rtl, backend)
        if rtl.console != emulator.console:
            raise TestFailure(f"{backend} printed {rtl.console!r}")
        difference = diff_traces(rtl.trace, emulator.trace)
        if difference:
            raise TestFailure(f"{backend} trace differs from the emulator's: {difference}")
    if not args.keep:
        for trace in work.glob("*.trace*"):
            trace.unlink()
    return test.stem, emulator.halt["steps"], time.monotonic() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--test", action="append", default=[], metavar="GLOB", help="only tests whose name matches (repeatable)")
    parser.add_argument("--backend", action="append", choices=BACKENDS, dest="backends",
                        help="backends to compare (repeatable; default emulator and qemu); the emulator always runs")
    parser.add_argument("--riscv-tests", type=Path, default=DEFAULT_RISCV_TESTS, help="the fetched riscv-tests checkout")
    parser.add_argument("--emulator", default=str(ROOT / "build/rv32/rv32emu"))
    parser.add_argument("--icarus", default=str(ROOT / "build/rv32/rv32_tb.vvp"), help="compiled Icarus testbench")
    parser.add_argument("--verilator", default=str(ROOT / "build/verilator-rv32/rv32_sim"), help="Verilator testbench binary")
    parser.add_argument("--qemu", default="qemu-system-riscv32")
    parser.add_argument("--qemu-cpu", default=DEFAULT_QEMU_CPU)
    parser.add_argument("--cc", default=os.environ.get("RV32_CC", "clang"))
    parser.add_argument("--ld", default=os.environ.get("RV32_LD", "ld.lld"))
    parser.add_argument("--out", type=Path, default=ROOT / "build/rv32/riscv-tests")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--limit", type=int, default=1_000_000, help="emulator instruction limit per test")
    parser.add_argument("--timeout", type=float, default=600.0, help="seconds each backend may run one test")
    parser.add_argument("--keep", action="store_true", help="keep the traces of passing tests")
    parser.add_argument("--fetch", action="store_true", help="check out the pinned tests into --riscv-tests and exit")
    args = parser.parse_args()
    if args.fetch:
        fetch(args.riscv_tests)
        return
    args.backends = sorted(set(args.backends or ["emulator", "qemu"]) | {"emulator"}, key=BACKENDS.index)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    commit = checkout_commit(args.riscv_tests) if (args.riscv_tests / ".git").exists() else ""
    if commit != RISCV_TESTS_COMMIT:
        parser.error(f"{args.riscv_tests} is at {commit or 'no commit'}, not the pinned {RISCV_TESTS_COMMIT}; "
                     "run make fetch-rv32-riscv-tests")
    for backend, path in (("icarus", args.icarus), ("verilator", args.verilator)):
        if backend in args.backends and not Path(path).exists():
            parser.error(f"{path} does not exist; build the {backend} testbench first")
    tests = select_tests(args.riscv_tests, args.test)
    if not tests:
        parser.error("no test matches")
    print(f"riscv-tests {RISCV_TESTS_COMMIT[:12]} rv32ua: {len(tests)} test(s) on {', '.join(args.backends)}", flush=True)

    def attempt(test):
        try:
            return run_one(test, args), None
        except (TestFailure, SystemExit) as error:  # check_passed and the shared runners exit
            return None, (test.stem, str(error))

    failures, passed, steps = [], 0, 0
    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for result, failure in pool.map(attempt, tests):
            if failure:
                failures.append(failure)
                print(f"FAIL rv32ua/{failure[0]}: {failure[1]}", flush=True)
            else:
                name, count, seconds = result
                passed += 1
                steps += count
                print(f"PASS rv32ua/{name}: {count} instructions, {seconds:.1f} s", flush=True)
    print(f"{passed}/{len(tests)} passed; {steps} instructions on the emulator; "
          f"{time.monotonic() - started:.0f} s on {', '.join(args.backends)}")
    if failures:
        sys.exit(f"{len(failures)} test(s) failed: {', '.join(name for name, _ in failures)}")


if __name__ == "__main__":
    main()
