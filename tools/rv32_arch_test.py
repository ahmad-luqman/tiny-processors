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
from tools.rv32_run_qemu import qemu_command  # noqa: E402
# The pinned suite: riscv-arch-test tag 3.9.1.
ARCH_TEST_URL = "https://github.com/riscv-non-isa/riscv-arch-test.git"
ARCH_TEST_COMMIT = "eb66181dd27ff7847e2c3a010705b13490b0bf75"
DEFAULT_ARCH_TEST = ROOT / "third_party" / "riscv-arch-test"
MODEL = ROOT / "tests" / "arch"


class Suite(NamedTuple):
    """How a suite is assembled: exactly its own ISA, so a test cannot use an instruction outside
    it, and whether RVMODEL_ASSERT checks the expected values written into the sources."""
    march: str
    asserts: bool


SUITES = {
    "I": Suite("rv32i_zicsr", asserts=True),
    "M": Suite("rv32im_zicsr", asserts=True),
    "F": Suite("rv32if_zicsr", asserts=False),
}
# Suites in rv32i_m that are not selected, and why (docs/rv32-groundwork.md).
EXCLUDED = {
    "A": "no A extension",
    "B": "no bit-manipulation extensions",
    "C": "no compressed instructions",
    "CMO": "no cache-management operations",
    "D": "no D extension",
    "D_Zfa": "no D or Zfa",
    "F_Zfa": "no Zfa",
    "K": "no scalar cryptography",
    "P_unratified": "no packed SIMD",
    "Svadu": "no virtual memory",
    "Zacas": "no A extension",
    "Zcmop": "no compressed instructions",
    "Zfh": "no half precision",
    "Zicond": "no Zicond",
    "Zifencei": "fence.i is an illegal instruction in our contract (docs/rv32.md)",
    "Zimop": "no may-be-operations",
    "privilege": "needs S-mode, misa and the suite's full trap-handler harness; O1 added only mstatus, mie, mip and mscratch",
}
BACKENDS = ("emulator", "qemu", "icarus", "verilator")
DEFAULT_QEMU_CPU = "rv32,c=false,d=false"  # generic RV32 with C and D off: IMAF plus Zicsr
# With -m 4M, QEMU's virt board writes its device tree 2 MiB into RAM (the end of RAM rounded
# down to 2 MiB); a longer image would be overwritten there. The largest selected test is 1.7 MiB.
QEMU_FDT_OFFSET = 0x200000


class TestFailure(Exception):
    """One test's failure, with the backend and what differed."""


def link_flags(march, ld):
    """The compiler driver flags that assemble and link a source against the model: bare RV32
    with `march`, soft-float ABI, no relaxation, tests/arch/link.ld, and model_test.h on the path."""
    return ["--target=riscv32-unknown-elf", f"-march={march}", "-mabi=ilp32", "-mno-relax", "-nostdlib",
            "-static", f"--ld-path={ld}", f"-Wl,-T,{MODEL / 'link.ld'}", f"-I{MODEL}"]


def assemble(test, suite, work, arch_test, cc, ld):
    """Assemble and link one test; returns (elf, bin, hex) paths."""
    elf = work / f"{test.stem}.elf"
    flags = [*link_flags(SUITES[suite].march, ld), "-DXLEN=32", "-DFLEN=32", "-DTEST_CASE_1=True",
             f"-I{arch_test / 'riscv-test-suite' / 'env'}"]
    if SUITES[suite].asserts:
        flags.append("-DRVMODEL_ASSERT")
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


def signature_lines(console):
    lines = console.splitlines()
    if not lines or any(len(line) != 8 or line.strip("0123456789abcdef") for line in lines):
        raise TestFailure(f"the console is not a signature: {console[:200]!r}")
    return lines


def run_qemu(args, elf):
    """Run one test's ELF on QEMU's virt board and return its console, the reference signature."""
    try:
        qemu = subprocess.run(qemu_command(args.qemu, elf, cpu=args.qemu_cpu), capture_output=True, timeout=args.timeout)
    except subprocess.TimeoutExpired:
        raise TestFailure(f"QEMU did not finish within {args.timeout} s") from None
    if qemu.returncode != 0:
        raise TestFailure(f"QEMU exited {qemu.returncode}: {qemu.stderr.decode(errors='replace').strip()}")
    return qemu.stdout.decode(errors="replace")


def run_one(test, suite, args):
    """Run one test on every backend; returns (name, signature words, emulator steps, seconds)."""
    started = time.monotonic()
    name = f"{suite}/{test.stem}"
    work = args.out / suite / test.stem
    shutil.rmtree(work, ignore_errors=True)
    work.mkdir(parents=True)
    elf, bin_path, hex_path = assemble(test, suite, work, args.arch_test, args.cc, args.ld)
    rtl_backends = [backend for backend in ("icarus", "verilator") if backend in args.backends]
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
    for backend in rtl_backends:
        simulator = args.icarus if backend == "icarus" else args.verilator
        # Generous: a floating-point instruction costs up to ~40 cycles, an M instruction 37.
        max_cycles = min(60 * emulator.halt["steps"] + 10000, 2147483647)
        rtl = run_rtl(simulator, hex_path, work / f"{backend}.trace", max_cycles=max_cycles, timeout=args.timeout)
        check_passed(rtl, backend)
        if rtl.console != emulator.console:
            raise TestFailure(f"{backend} signature differs from the emulator's")
        difference = diff_traces(rtl.trace, emulator.trace)
        if difference:
            raise TestFailure(f"{backend} trace differs from the emulator's: {difference}")
    if not args.keep:
        for trace in work.glob("*.trace*"):
            trace.unlink()
    return name, len(signature), emulator.halt["steps"], time.monotonic() - started


def checkout_commit(arch_test):
    """The commit a riscv-arch-test checkout is at, or "" when it has none."""
    return subprocess.run(["git", "-C", str(arch_test), "rev-parse", "HEAD"], capture_output=True, text=True).stdout.strip()


def fetch(arch_test):
    """Check out the pinned commit, only the files the selected suites need (the whole suite,
    D and Zfh included, is over 500 MB): the model headers, the licences, and I, M and F."""
    def git(*command):
        subprocess.run(["git", "-C", str(arch_test), *command], check=True)
    if (arch_test / ".git").exists():
        if checkout_commit(arch_test) == ARCH_TEST_COMMIT:
            print(f"{arch_test} is at the pinned {ARCH_TEST_COMMIT}")
            return
    shutil.rmtree(arch_test, ignore_errors=True)
    arch_test.mkdir(parents=True)
    git("init", "-q")
    git("remote", "add", "origin", ARCH_TEST_URL)
    git("sparse-checkout", "set", "--no-cone", "/COPYING.*", "/README.md", "/riscv-test-suite/env/",
        *(f"/riscv-test-suite/rv32i_m/{suite}/" for suite in SUITES))
    git("fetch", "-q", "--depth", "1", "--filter=blob:none", "origin", ARCH_TEST_COMMIT)
    git("checkout", "-q", "FETCH_HEAD")
    print(f"fetched riscv-arch-test {ARCH_TEST_COMMIT} into {arch_test}")


def select_tests(arch_test, suites, patterns):
    tests = []
    for suite in suites:
        source = arch_test / "riscv-test-suite" / "rv32i_m" / suite / "src"
        found = sorted(source.glob("*.S"))
        if not found:
            sys.exit(f"no tests in {source}; run make fetch-rv32-arch-test")
        tests += [(test, suite) for test in found
                  if not patterns or any(fnmatch.fnmatch(test.stem, pattern) for pattern in patterns)]
    return tests


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--suite", action="append", choices=sorted(SUITES), help="suite to run (repeatable; default all)")
    parser.add_argument("--test", action="append", default=[], metavar="GLOB", help="only tests whose name matches (repeatable)")
    parser.add_argument("--backend", action="append", choices=BACKENDS, dest="backends",
                        help="backends to compare (repeatable; default emulator and qemu); the emulator always runs")
    parser.add_argument("--arch-test", type=Path, default=DEFAULT_ARCH_TEST, help="the fetched riscv-arch-test checkout")
    parser.add_argument("--emulator", default=str(ROOT / "build/rv32/rv32emu"))
    parser.add_argument("--icarus", default=str(ROOT / "build/rv32/rv32_tb.vvp"), help="compiled Icarus testbench")
    parser.add_argument("--verilator", default=str(ROOT / "build/verilator-rv32/rv32_sim"), help="Verilator testbench binary")
    parser.add_argument("--qemu", default="qemu-system-riscv32")
    parser.add_argument("--qemu-cpu", default=DEFAULT_QEMU_CPU)
    parser.add_argument("--cc", default=os.environ.get("RV32_CC", "clang"))
    parser.add_argument("--ld", default=os.environ.get("RV32_LD", "ld.lld"))
    parser.add_argument("--out", type=Path, default=ROOT / "build/rv32/arch")
    parser.add_argument("--jobs", type=int, default=os.cpu_count() or 1)
    parser.add_argument("--limit", type=int, default=50_000_000, help="emulator instruction limit per test")
    parser.add_argument("--timeout", type=float, default=7200.0, help="seconds each backend may run one test")
    parser.add_argument("--keep", action="store_true", help="keep the traces of passing tests")
    parser.add_argument("--fetch", action="store_true", help="check out the pinned suite into --arch-test and exit")
    args = parser.parse_args()
    if args.fetch:
        fetch(args.arch_test)
        return
    args.backends = sorted(set(args.backends or ["emulator", "qemu"]) | {"emulator"}, key=BACKENDS.index)
    if args.jobs < 1:
        parser.error("--jobs must be at least 1")
    if not (args.arch_test / ".git").exists():
        parser.error(f"{args.arch_test} is not a checkout; run make fetch-rv32-arch-test")
    commit = checkout_commit(args.arch_test)
    if commit != ARCH_TEST_COMMIT:
        parser.error(f"{args.arch_test} is at {commit or 'no commit'}, not the pinned {ARCH_TEST_COMMIT}")
    for backend, path in (("icarus", args.icarus), ("verilator", args.verilator)):
        if backend in args.backends and not Path(path).exists():
            parser.error(f"{path} does not exist; build the {backend} testbench first")
    tests = select_tests(args.arch_test, args.suite or sorted(SUITES), args.test)
    if not tests:
        parser.error("no test matches")
    print(f"riscv-arch-test {ARCH_TEST_COMMIT[:12]}: {len(tests)} test(s) on {', '.join(args.backends)}", flush=True)

    failures, passed, steps = [], 0, 0

    def attempt(item):
        test, suite = item
        try:
            return run_one(test, suite, args), None
        except (TestFailure, SystemExit) as error:  # check_passed and the shared runners exit
            return None, (f"{suite}/{test.stem}", str(error))

    started = time.monotonic()
    with ThreadPoolExecutor(max_workers=args.jobs) as pool:
        for result, failure in pool.map(attempt, tests):
            if failure:
                failures.append(failure)
                print(f"FAIL {failure[0]}: {failure[1]}", flush=True)
            else:
                name, words, count, seconds = result
                passed += 1
                steps += count
                print(f"PASS {name}: {words} signature words, {count} instructions, {seconds:.1f} s", flush=True)
    by_suite = Counter(suite for _, suite in tests)
    summary = ", ".join(f"{suite} {count}" for suite, count in sorted(by_suite.items()))
    print(f"{passed}/{len(tests)} passed ({summary}); {steps} instructions on the emulator; "
          f"{time.monotonic() - started:.0f} s on {', '.join(args.backends)}")
    if failures:
        sys.exit(f"{len(failures)} test(s) failed: {', '.join(name for name, _ in failures)}")


if __name__ == "__main__":
    main()
