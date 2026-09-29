"""Run the CoreMark and Dhrystone images on our backends, check them, and report cycle counts.

Each image prints its own validation (CoreMark's CRCs, Dhrystone's final values next to what
they "should be") and one timing line per measured region, `bench: <name> ... cycles=C
instret=I`, read from the Zicntr counters. This runner checks the validation on every backend,
requires every backend's console to be identical once the timing lines are set aside, and
requires `instret` to agree exactly: retirement does not depend on timing, so it is the same on
the emulator and the RTL. `cycles` is device time (docs/rv32.md): clock cycles on the RTL and
executed instructions on the emulator, so only the RTL's figure is a performance number.

docs/rv32-groundwork.md has the method and the recorded baseline.
"""
import argparse
from pathlib import Path
import re
import sys
from typing import NamedTuple

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_image import write_hex  # noqa: E402
from tools.rv32_rtl import check_passed, run_emulator, run_rtl  # noqa: E402

BENCH_LINE = re.compile(r"\Abench: (\w+)(?: iterations=(\d+))? cycles=(\d+) instret=(\d+)\Z")
# Lines whose numbers are device time; everything else must match across backends.
TIMING_PREFIXES = ("bench: ", "Total ticks", "Total time (secs)", "Iterations/Sec",
                   "Microseconds for one run", "Dhrystones per Second")
# CoreMark's own check values for the 2K performance run (seeds 0, 0, 0x66): core_main.c.
COREMARK_KNOWN = {"seedcrc": "0xe9f5", "[0]crclist": "0xe714", "[0]crcmatrix": "0x1fd7", "[0]crcstate": "0x8e3a"}
DHRYSTONE_VAX_MIPS = 1757  # Dhrystones per second of the VAX 11/780, the 1 MIPS reference


class BenchError(Exception):
    pass


class Timing(NamedTuple):
    """One `bench:` line; `iterations` is None when the line does not carry it (Dhrystone)."""
    name: str
    iterations: int | None
    cycles: int
    instret: int


class Row(NamedTuple):
    """One line of the report: an image measured on one backend."""
    image: str
    backend: str
    name: str
    iterations: int
    cycles: int
    instret: int
    halt: dict


def run_backend(backend, image, args):
    """Run one image on one backend with no trace and return its console and halt line. A run that
    did not pass exits through check_passed (SystemExit), as does a timeout."""
    if backend == "emulator":
        run = run_emulator(args.emulator, image, None, timeout=args.timeout)
    else:
        hex_path = args.out / f"{image.stem}.hex"
        write_hex(hex_path, image.read_bytes())
        simulator = args.icarus if backend == "icarus" else args.verilator
        run = run_rtl(simulator, hex_path, None, max_cycles=args.max_cycles, timeout=args.timeout)
    check_passed(run, backend)
    return run.console, run.halt


def timing_lines(console):
    """The console's one `bench:` line as a Timing; none, two, or a malformed one is an error."""
    found = []
    for line in console.splitlines():
        match = BENCH_LINE.match(line)
        if match:
            name, iterations, cycles, instret = match.groups()
            found.append(Timing(name, int(iterations) if iterations else None, int(cycles), int(instret)))
        elif line.startswith("bench: "):
            raise BenchError(f"malformed timing line {line!r}")
    if len(found) != 1:
        raise BenchError(f"expected one timing line, found {[timing.name for timing in found]}")
    return found[0]


def check_coremark(console):
    """CoreMark says so itself, and its CRCs must be the known ones for this run. Returns the
    iteration count CoreMark reports."""
    if "Correct operation validated." not in console or "ERROR" in console or "Errors detected" in console:
        raise BenchError("CoreMark did not validate:\n" + console)
    values = {key.strip(): value.strip() for key, value in
              (line.split(":", 1) for line in console.splitlines() if ":" in line)}
    for key, expected in COREMARK_KNOWN.items():
        if values.get(key) != expected:
            raise BenchError(f"CoreMark {key} is {values.get(key)!r}, not {expected}")
    if not values.get("Iterations", "").isdigit():
        raise BenchError(f"CoreMark reported no iteration count: {values.get('Iterations')!r}")
    return int(values["Iterations"])


def check_dhrystone(console):
    """Every printed value against the "should be" line after it: literal values, the run count
    plus ten, and the two pointers that must agree with each other. Returns the run count."""
    lines = console.splitlines()
    trying = re.search(r"Trying (\d+) runs through Dhrystone", console)
    if trying is None:
        raise BenchError("Dhrystone printed no \"Trying N runs\" line")
    runs = int(trying.group(1))
    checked, pointers = 0, []
    for value_line, expected_line in zip(lines, lines[1:]):
        if not expected_line.strip().startswith("should be:"):
            continue
        label, _, value = value_line.partition(":")
        value, expected = value.strip(), expected_line.split("should be:", 1)[1].strip()
        if expected == "Number_Of_Runs + 10":
            expected = str(runs + 10)
        elif expected.startswith("(implementation-dependent)"):
            pointers.append(value)
            continue
        if value != expected:
            raise BenchError(f"Dhrystone {label.strip()} is {value!r}, should be {expected!r}")
        checked += 1
    if checked != 20 or len(pointers) != 2 or pointers[0] != pointers[1]:
        raise BenchError(f"Dhrystone printed {checked} checkable values and pointers {pointers}")
    return runs


# The benchmark a timing line names decides how its console is validated; each check returns the
# number of iterations the measured region ran.
CHECKS = {"coremark": check_coremark, "dhrystone": check_dhrystone}


def measure(backend, console, halt, image):
    """Validate one backend's console and return its Row."""
    timing = timing_lines(console)
    if timing.name not in CHECKS:
        raise BenchError(f"{backend}: timing line for unknown benchmark {timing.name!r}")
    iterations = CHECKS[timing.name](console)
    if timing.iterations is not None and timing.iterations != iterations:
        raise BenchError(f"{backend}: timing line says {timing.iterations} iterations, {timing.name} ran {iterations}")
    if not (iterations > 0 and timing.cycles > 0 and timing.instret > 0):
        raise BenchError(f"{backend}: nothing measured: {iterations} iterations, {timing}")
    return Row(image.stem, backend, timing.name, iterations, timing.cycles, timing.instret, halt)


def comparable(console):
    return [line for line in console.splitlines() if not line.startswith(TIMING_PREFIXES)]


def bench_image(image, backends, args):
    """Run and check one image on every backend; returns its rows or raises BenchError/SystemExit."""
    consoles, rows = {}, {}
    for backend in backends:
        console, halt = run_backend(backend, image, args)
        consoles[backend], rows[backend] = console, measure(backend, console, halt, image)
    reference = backends[0]
    for backend in backends[1:]:
        if comparable(consoles[backend]) != comparable(consoles[reference]):
            raise BenchError(f"{backend} console differs from the {reference}'s outside the timing lines")
        if rows[backend].instret != rows[reference].instret:
            raise BenchError(f"instret differs: {backend} {rows[backend].instret}, {reference} {rows[reference].instret}")
    return [rows[backend] for backend in backends]


def main(argv=None):
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("images", nargs="+", type=Path, help="flat .bin images from make firmware-rv32-bench")
    parser.add_argument("--backend", action="append", choices=("emulator", "icarus", "verilator"), dest="backends",
                        help="backends to run (repeatable; default emulator and verilator)")
    parser.add_argument("--emulator", default=str(ROOT / "build/rv32/rv32emu"))
    parser.add_argument("--icarus", default=str(ROOT / "build/rv32/rv32_tb.vvp"))
    parser.add_argument("--verilator", default=str(ROOT / "build/verilator-rv32/rv32_sim"))
    parser.add_argument("--out", type=Path, default=ROOT / "build/rv32bench/run")
    parser.add_argument("--max-cycles", type=int, default=1_000_000_000)
    parser.add_argument("--timeout", type=float, default=7200.0)
    args = parser.parse_args(argv)
    backends = args.backends or ["emulator", "verilator"]
    args.out.mkdir(parents=True, exist_ok=True)
    rows, failures = [], []
    for image in args.images:
        try:
            rows += bench_image(image, backends, args)
        except (BenchError, SystemExit) as error:  # check_passed and the shared runners exit
            failures.append(image.stem)
            print(f"FAIL {image.stem}: {error}", file=sys.stderr)
    print(f"{'image':<14} {'backend':<10} {'iterations':>10} {'cycles':>12} {'instret':>12} {'CPI':>6} "
          f"{'cycles/iter':>12} {'instr/iter':>11} {'per MHz':>9}")
    for row in rows:
        per_mhz = row.iterations * 1e6 / row.cycles
        if row.name == "dhrystone":
            per_mhz /= DHRYSTONE_VAX_MIPS  # DMIPS/MHz
        unit = "DMIPS" if row.name == "dhrystone" else "CM"
        # The emulator's cycles are instructions (device time): no clock, so no per-MHz figure.
        rate = f"{'-':>6} {unit:<5}" if row.backend == "emulator" else f"{per_mhz:>6.3f} {unit:<5}"
        print(f"{row.image:<14} {row.backend:<10} {row.iterations:>10} {row.cycles:>12} {row.instret:>12} "
              f"{row.cycles / row.instret:>6.3f} {row.cycles / row.iterations:>12.1f} "
              f"{row.instret / row.iterations:>11.1f} {rate}"
              + (f"  (run: {row.halt['cycles']} cycles, md_waits {row.halt.get('md_waits', 0)})"
                 if row.backend != "emulator" else ""))
    if failures:
        sys.exit(f"{len(failures)} image(s) failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
