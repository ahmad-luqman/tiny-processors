"""Run the RISC-V architectural tests (riscv-arch-test) on our machine and compare signatures.

Each selected test is assembled against tests/arch/model_test.h, which prints the test's
signature on the console and writes the done register. The signature must be identical on
every backend asked for: the emulator, QEMU's virt board (the reference: it has our console and
done register, and its own RISC-V implementation), and the RTL under Icarus or Verilator. The
emulator and each RTL run must also retire identical traces, line for line. The I and M suites
additionally check every integer result against the value the test generator wrote into the
source (RVMODEL_ASSERT), which does not depend on any of the backends.

The suite is fetched at a pinned commit by `make fetch-rv32-arch-test`; see
docs/rv32-groundwork.md for the selection, the model and the acceptance record.
"""
import argparse
from collections import Counter
from concurrent.futures import ThreadPoolExecutor
import fnmatch
import os
from pathlib import Path
import shutil
import subprocess
import sys
import time
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_image import RAM_BASE, flatten, parse_elf, write_hex  # noqa: E402
from tools.rv32_rtl import check_passed, diff_traces, run_emulator, run_rtl  # noqa: E402
from tools.rv32_run_qemu import qemu_command, run as run_captured  # noqa: E402
# The pinned suite: riscv-arch-test tag 3.9.1.
ARCH_TEST_URL = "https://github.com/riscv-non-isa/riscv-arch-test.git"
ARCH_TEST_COMMIT = "eb66181dd27ff7847e2c3a010705b13490b0bf75"
DEFAULT_ARCH_TEST = ROOT / "third_party" / "riscv-arch-test"
MODEL = ROOT / "tests" / "arch"


class Suite(NamedTuple):
    """How a suite is assembled: exactly its own ISA, so a test cannot use an instruction outside
    it, and whether RVMODEL_ASSERT checks the expected values written into the sources; and how
    many tests the pinned commit has, so a checkout missing one is refused, not run short."""
    march: str
    asserts: bool
    tests: int


SUITES = {
    "I": Suite("rv32i_zicsr", asserts=True, tests=39),
    "M": Suite("rv32im_zicsr", asserts=True, tests=8),
    "F": Suite("rv32if_zicsr", asserts=False, tests=142),
    "A": Suite("rv32ia_zicsr", asserts=False, tests=9),  # issue #34: the nine AMOs; LR/SC in tools/rv32_riscv_tests.py
    "Zifencei": Suite("rv32i_zicsr_zifencei", asserts=True, tests=1),  # issue #36: fence.i, after a store to code
}
# Suites in rv32i_m that are not selected, and why (docs/rv32-groundwork.md).
EXCLUDED = {
    "B": "no bit-manipulation extensions",
    "C": "no compressed instructions",
    "CMO": "no cache-management operations",
    "D": "no D extension",
    "D_Zfa": "no D or Zfa",
    "F_Zfa": "no Zfa",
    "K": "no scalar cryptography",
    "P_unratified": "no packed SIMD",
    "Svadu": "Sv32 sets no A or D bits in hardware: a clear one faults (Svade, issue #20)",
    "Zacas": "no Zacas (atomic compare-and-swap)",
    "Zcmop": "no compressed instructions",
    "Zfh": "no half precision",
    "Zicond": "no Zicond",
    "Zimop": "no may-be-operations",
    "privilege": "needs the suite's full trap-handler harness; S-mode and Sv32 (issue #20) are checked by mmucheck against QEMU and by tests/test_rv32_mmu.py instead",
}
BACKENDS = ("emulator", "qemu", "icarus", "verilator")
DEFAULT_QEMU_CPU = "rv32,c=false,d=false"  # generic RV32 with C and D off: IMAF plus Zicsr
# With -m 16M, QEMU's virt board writes its device tree 14 MiB into RAM (the end of RAM less the
# tree, rounded down to 2 MiB); a longer image would be overwritten there. The largest selected test is 1.7 MiB.
QEMU_FDT_OFFSET = 0xE00000


class TestFailure(Exception):
    """One test's failure, with the backend and what differed."""


def link_flags(march, ld):
    """The compiler driver flags that assemble and link a source against the model: bare RV32
    with `march`, soft-float ABI, no relaxation, tests/arch/link.ld, and model_test.h on the path."""
    return ["--target=riscv32-unknown-elf", f"-march={march}", "-mabi=ilp32", "-mno-relax", "-nostdlib",
            "-static", f"--ld-path={ld}", f"-Wl,-T,{MODEL / 'link.ld'}", f"-I{MODEL}"]


def compile_test(cc, flags, source, elf):
    """Assemble and link one test source into `elf`, then pack it (pack_image)."""
    result = subprocess.run([cc, *flags, "-o", str(elf), str(source)], capture_output=True, text=True)
    if result.returncode != 0:
        raise TestFailure(f"assembly failed:\n{result.stderr}")
    return (elf, *pack_image(elf))


def assemble(test, suite, work, arch_test, cc, ld):
    """Assemble and link one test; returns (elf, bin, hex) paths."""
    flags = [*link_flags(SUITES[suite].march, ld), "-DXLEN=32", "-DFLEN=32", "-DTEST_CASE_1=True",
             f"-I{arch_test / 'riscv-test-suite' / 'env'}"]
    if SUITES[suite].asserts:
        flags.append("-DRVMODEL_ASSERT")
    return compile_test(cc, flags, test, work / f"{test.stem}.elf")


def pack_image(elf):
    """Check a linked test (its entry at the reset PC, its image below QEMU's device tree) and
    write the flat .bin and the .hex beside it; returns (bin, hex) paths."""
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
    return bin_path, hex_path


def signature_lines(console):
    lines = console.splitlines()
    if not lines or any(len(line) != 8 or line.strip("0123456789abcdef") for line in lines):
        raise TestFailure(f"the console is not a signature: {console[:200]!r}")
    return lines


def run_qemu(args, elf):
    """Run one test's ELF on QEMU's virt board and return its console, the reference signature."""
    status, console, diagnostics, timed_out = run_captured(qemu_command(args.qemu, elf, cpu=args.qemu_cpu), args.timeout)
    if timed_out:
        raise TestFailure(f"QEMU did not finish within {args.timeout} s")
    if status != 0:
        raise TestFailure(f"QEMU exited {status}: {diagnostics.strip()}")
    return console


def run_one(test, suite, args):
    """Run one test on every backend; returns (name, signature words, emulator steps, seconds)."""
    started = time.monotonic()
    name = f"{suite}/{test.stem}"
    work = args.out / suite / test.stem
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    elf, bin_path, hex_path = assemble(test, suite, work, args.arch_test, args.cc, args.ld)
    emulator_trace = work / "emu.trace"
    emulator = run_emulator(args.emulator, bin_path, emulator_trace, limit=args.limit, timeout=args.timeout)
    check_passed(emulator, "emulator")
    signature = signature_lines(emulator.console)
    if "qemu" in args.backends:
        reference = run_qemu(args, elf)
        if reference != emulator.console:
            difference = next((i for i, (a, b) in enumerate(zip(reference.splitlines(), signature)) if a != b), None)
            raise TestFailure(f"signature differs from QEMU's at word {difference} "
                              f"({len(reference.splitlines())} vs {len(signature)} words)")
    compare_rtl(args, hex_path, work, emulator)
    return name, len(signature), emulator.halt["steps"], time.monotonic() - started


def compare_rtl(args, hex_path, work, emulator):
    """Run each simulator asked for; its console and whole trace must equal the emulator's. Then
    drop the traces, unless --keep."""
    for backend in ("icarus", "verilator"):
        if backend not in args.backends:
            continue
        simulator = args.icarus if backend == "icarus" else args.verilator
        # Generous: a floating-point instruction costs up to ~40 cycles, an M instruction 37.
        max_cycles = min(60 * emulator.halt["steps"] + 10000, 2147483647)
        rtl = run_rtl(simulator, hex_path, work / f"{backend}.trace", max_cycles=max_cycles, timeout=args.timeout)
        check_passed(rtl, backend)
        if rtl.console != emulator.console:
            raise TestFailure(f"{backend} console differs from the emulator's")
        difference = diff_traces(rtl.trace, emulator.trace)
        if difference:
            raise TestFailure(f"{backend} trace differs from the emulator's: {difference}")
    if not args.keep:
        for trace in work.glob("*.trace*"):
            trace.unlink()


def checkout_commit(path):
    """The commit a checkout is at, or "" when it has none."""
    return subprocess.run(["git", "-C", str(path), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def sparse_checkout(path, url, commit, patterns):
    """Replace `path` with a shallow checkout of `commit` from `url` holding only `patterns`."""
    def git(*command):
        subprocess.run(["git", "-C", str(path), *command], check=True)
    if path.exists():
        shutil.rmtree(path)
    path.mkdir(parents=True)
    git("init", "-q")
    git("remote", "add", "origin", url)
    git("sparse-checkout", "set", "--no-cone", *patterns)
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", commit)
    git("checkout", "-q", "FETCH_HEAD")


def fetch(arch_test):
    """Check out the pinned commit, only the files the selected suites need (the whole suite,
    D and Zfh included, is over 500 MB): the model headers, the licences, and I, M, F, A and
    Zifencei. A checkout at the pinned commit that lacks a selected suite's tests (one made before
    a suite was added: A in issue #34, Zifencei in issue #36) is fetched again."""
    if (arch_test / ".git").exists() and checkout_commit(arch_test) == ARCH_TEST_COMMIT and all(
            len(suite_sources(arch_test, suite)) == SUITES[suite].tests for suite in SUITES):
        print(f"{arch_test} is at the pinned {ARCH_TEST_COMMIT}")
        return
    sparse_checkout(arch_test, ARCH_TEST_URL, ARCH_TEST_COMMIT,
                    ["/COPYING.*", "/README.md", "/riscv-test-suite/env/",
                     *(f"/riscv-test-suite/rv32i_m/{suite}/" for suite in SUITES)])
    print(f"fetched riscv-arch-test {ARCH_TEST_COMMIT} into {arch_test}")


def suite_sources(arch_test, suite):
    return sorted((arch_test / "riscv-test-suite" / "rv32i_m" / suite / "src").glob("*.S"))


def matching(tests, patterns, name):
    """The tests whose `name` matches a pattern (all without one); a pattern that matches none of
    them is an error rather than a smaller run."""
    for pattern in patterns:
        if not any(fnmatch.fnmatch(name(test), pattern) for test in tests):
            sys.exit(f"--test {pattern} matches no test")
    return [test for test in tests if not patterns or any(fnmatch.fnmatch(name(test), pattern) for pattern in patterns)]


def select_tests(arch_test, suites, patterns):
    """The selected suites' tests, each suite whole as the pinned commit has it, then narrowed by
    the --test patterns."""
    tests = []
    for suite in suites:
        found = suite_sources(arch_test, suite)
        if len(found) != SUITES[suite].tests:
            sys.exit(f"suite {suite} has {len(found)} tests, not the pinned commit's {SUITES[suite].tests}; "
                     "run make fetch-rv32-arch-test")
        tests += [(test, suite) for test in found]
    return matching(tests, patterns, lambda item: item[0].stem)


def add_runner_arguments(parser, out, limit, timeout):
    """The options this runner and tools/rv32_riscv_tests.py share: the selection, the backends
    and their binaries, the toolchain, and each run's limits."""
    parser.add_argument("--test", action="append", default=[], metavar="GLOB", help="only tests whose name matches (repeatable)")
    parser.add_argument("--backend", action="append", choices=BACKENDS, dest="backends",
                        help="backends to compare (repeatable; default emulator and qemu); the emulator always runs")
    parser.add_argument("--emulator", default=str(ROOT / "build/rv32/rv32emu"))
    parser.add_argument("--icarus", default=str(ROOT / "build/rv32/rv32_tb.vvp"), help="compiled Icarus testbench")
    parser.add_argument("--verilator", default=str(ROOT / "build/verilator-rv32/rv32_sim"), help="Verilator testbench binary")
    parser.add_argument("--qemu", default="qemu-system-riscv32")
    parser.add_argument("--qemu-cpu", default=DEFAULT_QEMU_CPU)
    parser.add_argument("--cc", default=os.environ.get("RV32_CC", "clang"))
    parser.add_argument("--ld", default=os.environ.get("RV32_LD", "ld.lld"))
    parser.add_argument("--out", type=Path, default=out)
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--limit", type=int, default=limit, help="emulator instruction limit per test")
    parser.add_argument("--timeout", type=float, default=timeout, help="seconds each backend may run one test")
    parser.add_argument("--keep", action="store_true", help="keep the traces of passing tests")


def check_runner_arguments(parser, args):
    """Normalise the backends (the emulator always runs) and refuse a run that could not start."""
    args.backends = sorted(set(args.backends or ["emulator", "qemu"]) | {"emulator"}, key=BACKENDS.index)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    for backend, path in (("icarus", args.icarus), ("verilator", args.verilator)):
        if backend in args.backends and not Path(path).exists():
            parser.error(f"{path} does not exist; build the {backend} testbench first")


def run_all(items, run, name, jobs):
    """Run `run(item)` for each item on `jobs` threads, printing PASS with its text, or FAIL with
    the reason, as each finishes. `run` returns (text, emulator steps). Returns (passed, steps,
    failed names)."""
    def attempt(item):
        try:
            return run(item), None
        except (TestFailure, SystemExit) as error:  # check_passed and the shared runners exit
            return None, str(error)

    passed, steps, failed = 0, 0, []
    with ThreadPoolExecutor(max_workers=jobs) as pool:
        for item, (result, failure) in zip(items, pool.map(attempt, items)):
            if failure is not None:
                failed.append(name(item))
                print(f"FAIL {name(item)}: {failure}", flush=True)
            else:
                text, count = result
                passed += 1
                steps += count
                print(f"PASS {name(item)}: {text}", flush=True)
    return passed, steps, failed


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", action="append", choices=sorted(SUITES), help="suite to run (repeatable; default all)")
    parser.add_argument("--arch-test", type=Path, default=DEFAULT_ARCH_TEST, help="the fetched riscv-arch-test checkout")
    parser.add_argument("--fetch", action="store_true", help="check out the pinned suite into --arch-test and exit")
    add_runner_arguments(parser, ROOT / "build/rv32/arch", limit=50_000_000, timeout=7200.0)
    args = parser.parse_args()
    if args.fetch:
        fetch(args.arch_test)
        return
    check_runner_arguments(parser, args)
    if not (args.arch_test / ".git").exists():
        parser.error(f"{args.arch_test} is not a checkout; run make fetch-rv32-arch-test")
    commit = checkout_commit(args.arch_test)
    if commit != ARCH_TEST_COMMIT:
        parser.error(f"{args.arch_test} is at {commit or 'no commit'}, not the pinned {ARCH_TEST_COMMIT}")
    tests = select_tests(args.arch_test, args.suite or sorted(SUITES), args.test)
    print(f"riscv-arch-test {ARCH_TEST_COMMIT[:12]}: {len(tests)} test(s) on {', '.join(args.backends)}", flush=True)

    def run(item):
        _, words, count, seconds = run_one(*item, args)
        return f"{words} signature words, {count} instructions, {seconds:.1f} s", count

    started = time.monotonic()
    passed, steps, failed = run_all(tests, run, lambda item: f"{item[1]}/{item[0].stem}", args.jobs)
    by_suite = Counter(suite for _, suite in tests)
    summary = ", ".join(f"{suite} {count}" for suite, count in sorted(by_suite.items()))
    print(f"{passed}/{len(tests)} passed ({summary}); {steps} instructions on the emulator; "
          f"{time.monotonic() - started:.0f} s on {', '.join(args.backends)}")
    if failed:
        sys.exit(f"{len(failed)} test(s) failed: {', '.join(failed)}")


if __name__ == "__main__":
    main()
