"""Run riscv-tests' rv32ua ISA tests (issue #34) on our machine.

riscv-arch-test's A suite (tools/rv32_arch_test.py) has the nine AMOs but no LR.W or SC.W test;
riscv-tests' rv32ua has both, lrsc.S among them. Each selected test is assembled against
tests/riscv-tests/riscv_test.h, our bare M-mode environment in place of riscv-tests' own, and
must print PASS and write the pass word on every backend asked for: the emulator, QEMU's virt
board, and the RTL under Icarus or Verilator. The emulator and each RTL run must also retire
identical traces, line for line. The shared machinery is tools/rv32_arch_test.py's.

riscv-tests is fetched at a pinned commit by `make fetch-rv32-riscv-tests`: only its licence, the
rv32ua sources (which include rv64ua's) and the scalar test macros. See docs/rv32-a.md.
"""
import argparse
from pathlib import Path
import re
import shutil
import sys
import time

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_arch_test import (TestFailure, add_runner_arguments, check_runner_arguments, checkout_commit,  # noqa: E402
                                  compare_rtl, compile_test, link_flags, matching, run_all, run_qemu, sparse_checkout)
from tools.rv32_rtl import check_passed, run_emulator  # noqa: E402

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


def suite_names(path):
    """The rv32ua tests the suite's own Makefrag lists (rv32ua_sc_tests)."""
    makefrag = (path / "isa" / "rv32ua" / "Makefrag").read_text()
    match = re.search(r"^rv32ua_sc_tests\s*=((?:.*\\\n)*.*)$", makefrag, re.MULTILINE)
    if not match:
        sys.exit(f"no rv32ua_sc_tests in {path / 'isa/rv32ua/Makefrag'}")
    return set(match.group(1).replace("\\", " ").split())


def fetch(path):
    """Check out the pinned commit, only the files the tests need, unless a checkout there already
    holds every test its Makefrag lists."""
    if (path / ".git").exists() and checkout_commit(path) == RISCV_TESTS_COMMIT and (path / "isa/rv32ua/Makefrag").exists() \
            and all((path / "isa" / "rv32ua" / f"{name}.S").exists() for name in suite_names(path)):
        print(f"{path} is at the pinned {RISCV_TESTS_COMMIT}")
        return
    sparse_checkout(path, RISCV_TESTS_URL, RISCV_TESTS_COMMIT, SPARSE)
    print(f"fetched riscv-tests {RISCV_TESTS_COMMIT} into {path}")


def select_tests(path, patterns):
    """Every test the Makefrag lists less EXCLUDED, each present (and every exclusion naming a real
    test), then narrowed by --test: a checkout missing one is refused, never run short."""
    listed = suite_names(path)
    stale = sorted(set(EXCLUDED) - listed)
    if stale:
        sys.exit(f"EXCLUDED names tests rv32ua no longer has: {', '.join(stale)}")
    source = path / "isa" / "rv32ua"
    missing = sorted(name for name in listed if not (source / f"{name}.S").exists())
    if missing:
        sys.exit(f"rv32ua is missing {', '.join(missing)}; run make fetch-rv32-riscv-tests")
    tests = [source / f"{name}.S" for name in sorted(listed - set(EXCLUDED))]
    return matching(tests, patterns, lambda test: test.stem)


def assemble(test, work, riscv_tests, cc, ld):
    """Assemble and link one test against our environment; returns (elf, bin, hex) paths."""
    flags = [*link_flags(MARCH, ld), f"-I{ENV}", f"-I{riscv_tests / 'isa' / 'macros' / 'scalar'}"]
    return compile_test(cc, flags, test, work / f"{test.stem}.elf")


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
        console = run_qemu(args, elf)  # a fail word makes QEMU exit nonzero, which raises
        if console != PASS_CONSOLE:
            raise TestFailure(f"QEMU printed {console!r}")
    compare_rtl(args, hex_path, work, emulator)
    return test.stem, emulator.halt["steps"], time.monotonic() - started


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--riscv-tests", type=Path, default=DEFAULT_RISCV_TESTS, help="the fetched riscv-tests checkout")
    parser.add_argument("--fetch", action="store_true", help="check out the pinned tests into --riscv-tests and exit")
    add_runner_arguments(parser, ROOT / "build/rv32/riscv-tests", limit=1_000_000, timeout=600.0)
    args = parser.parse_args()
    if args.fetch:
        fetch(args.riscv_tests)
        return
    check_runner_arguments(parser, args)
    commit = checkout_commit(args.riscv_tests) if (args.riscv_tests / ".git").exists() else ""
    if commit != RISCV_TESTS_COMMIT:
        parser.error(f"{args.riscv_tests} is at {commit or 'no commit'}, not the pinned {RISCV_TESTS_COMMIT}; "
                     "run make fetch-rv32-riscv-tests")
    tests = select_tests(args.riscv_tests, args.test)
    print(f"riscv-tests {RISCV_TESTS_COMMIT[:12]} rv32ua: {len(tests)} test(s) on {', '.join(args.backends)}", flush=True)

    def run(test):
        _, count, seconds = run_one(test, args)
        return f"{count} instructions, {seconds:.1f} s", count

    started = time.monotonic()
    passed, steps, failed = run_all(tests, run, lambda test: f"rv32ua/{test.stem}", args.jobs)
    print(f"{passed}/{len(tests)} passed; {steps} instructions on the emulator; "
          f"{time.monotonic() - started:.0f} s on {', '.join(args.backends)}")
    if failed:
        sys.exit(f"{len(failed)} test(s) failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
