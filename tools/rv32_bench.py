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
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_image import to_hex_words  # noqa: E402
from tools.rv32_rtl import decode, rtl_halt_line, simulator_command, simulator_noise  # noqa: E402
from tools.rv32_run_emu import emulator_command, halt_line  # noqa: E402

BENCH_LINE = re.compile(r"\Abench: (\w+)(?: iterations=(\d+))? cycles=(\d+) instret=(\d+)\Z")
# Lines whose numbers are device time; everything else must match across backends.
TIMING_PREFIXES = ("bench: ", "Total ticks", "Total time (secs)", "Iterations/Sec",
                   "Microseconds for one run", "Dhrystones per Second")
# CoreMark's own check values for the 2K performance run (seeds 0, 0, 0x66): core_main.c.
COREMARK_KNOWN = {"seedcrc": "0xe9f5", "[0]crclist": "0xe714", "[0]crcmatrix": "0x1fd7", "[0]crcstate": "0x8e3a"}
DHRYSTONE_VAX_MIPS = 1757  # Dhrystones per second of the VAX 11/780, the 1 MIPS reference


class BenchError(Exception):
    pass


def run(command, timeout):
    try:
        return subprocess.run(command, capture_output=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        raise BenchError(f"{command[0]} did not finish within {timeout} s") from None


def run_emulator(emulator, image, timeout):
    completed = run(emulator_command(emulator, image), timeout)
    stderr = decode(completed.stderr)
    halt = halt_line(stderr)
    if completed.returncode != 0 or halt is None or (halt["halt"], halt["outcome"]) != ("done", "pass"):
        raise BenchError(f"emulator did not pass: {stderr.strip()[-300:]}")
    return decode(completed.stdout), halt


def run_rtl(simulator, image, out, max_cycles, timeout):
    hex_path, console = out / f"{image.stem}.hex", out / f"{image.stem}.console"
    hex_path.write_text("".join(f"{word}\n" for word in to_hex_words(image.read_bytes())))
    console.write_text("")
    completed = run(simulator_command(simulator, hex_path, console=console, max_cycles=max_cycles), timeout)
    stderr = decode(completed.stderr)
    halt = rtl_halt_line(stderr)
    noise = simulator_noise(decode(completed.stdout))
    if completed.returncode != 0 or noise or halt is None or (halt["halt"], halt["outcome"]) != ("done", "pass"):
        raise BenchError(f"RTL did not pass: {stderr.strip()[-300:]} {noise[:200]}")
    return decode(console.read_bytes()), halt


def timing_lines(console):
    """The `bench:` lines as {name: (iterations, cycles, instret)}."""
    found = {}
    for line in console.splitlines():
        match = BENCH_LINE.match(line)
        if match:
            name, iterations, cycles, instret = match.groups()
            found[name] = (int(iterations) if iterations else None, int(cycles), int(instret))
        elif line.startswith("bench: "):
            raise BenchError(f"malformed timing line {line!r}")
    if len(found) != 1:
        raise BenchError(f"expected one timing line, found {sorted(found)}")
    return found


def check_coremark(console):
    """CoreMark says so itself, and its CRCs must be the known ones for this run."""
    if "Correct operation validated." not in console or "ERROR" in console or "Errors detected" in console:
        raise BenchError("CoreMark did not validate:\n" + console)
    values = {key.strip(): value.strip() for key, value in
              (line.split(":", 1) for line in console.splitlines() if ":" in line)}
    for key, expected in COREMARK_KNOWN.items():
        if values.get(key) != expected:
            raise BenchError(f"CoreMark {key} is {values.get(key)!r}, not {expected}")


def check_dhrystone(console):
    """Every printed value against the "should be" line after it: literal values, the run count
    plus ten, and the two pointers that must agree with each other."""
    lines = console.splitlines()
    runs = int(re.search(r"Trying (\d+) runs through Dhrystone", console).group(1))
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


def comparable(console):
    return [line for line in console.splitlines() if not line.startswith(TIMING_PREFIXES)]


def main():
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
    args = parser.parse_args()
    backends = args.backends or ["emulator", "verilator"]
    args.out.mkdir(parents=True, exist_ok=True)
    rows, failures = [], []
    for image in args.images:
        try:
            consoles, results = {}, {}
            for backend in backends:
                if backend == "emulator":
                    console, halt = run_emulator(args.emulator, image, args.timeout)
                else:
                    simulator = args.icarus if backend == "icarus" else args.verilator
                    console, halt = run_rtl(simulator, image, args.out, args.max_cycles, args.timeout)
                if "Correct operation validated" in console or "CoreMark" in console:
                    check_coremark(console)
                elif "Dhrystone" in console:
                    check_dhrystone(console)
                else:
                    raise BenchError(f"{backend}: neither CoreMark nor Dhrystone output:\n{console}")
                consoles[backend], results[backend] = console, (timing_lines(console), halt)
            reference = backends[0]
            for backend in backends[1:]:
                if comparable(consoles[backend]) != comparable(consoles[reference]):
                    raise BenchError(f"{backend} console differs from the {reference}'s outside the timing lines")
                ours, theirs = results[backend][0], results[reference][0]
                if {name: value[2] for name, value in ours.items()} != {name: value[2] for name, value in theirs.items()}:
                    raise BenchError(f"instret differs: {backend} {ours}, {reference} {theirs}")
            for backend in backends:
                (name, (iterations, cycles, instret)), = results[backend][0].items()
                if name == "dhrystone":
                    iterations = int(re.search(r"Trying (\d+) runs", consoles[backend]).group(1))
                rows.append((image.stem, backend, name, iterations, cycles, instret, results[backend][1]))
        except BenchError as error:
            failures.append(image.stem)
            print(f"FAIL {image.stem}: {error}", file=sys.stderr)
    print(f"{'image':<14} {'backend':<10} {'iterations':>10} {'cycles':>12} {'instret':>12} {'CPI':>6} "
          f"{'cycles/iter':>12} {'instr/iter':>11} {'per MHz':>9}")
    for image, backend, name, iterations, cycles, instret, halt in rows:
        per_mhz = iterations * 1e6 / cycles
        if name == "dhrystone":
            per_mhz /= DHRYSTONE_VAX_MIPS  # DMIPS/MHz
        unit = "DMIPS" if name == "dhrystone" else "CM"
        # The emulator's cycles are instructions (device time): no clock, so no per-MHz figure.
        rate = f"{'-':>6} {unit:<5}" if backend == "emulator" else f"{per_mhz:>6.3f} {unit:<5}"
        print(f"{image:<14} {backend:<10} {iterations:>10} {cycles:>12} {instret:>12} {cycles / instret:>6.3f} "
              f"{cycles / iterations:>12.1f} {instret / iterations:>11.1f} {rate}"
              + (f"  (run: {halt['cycles']} cycles, md_waits {halt.get('md_waits', 0)})" if backend != "emulator" else ""))
    if failures:
        sys.exit(f"{len(failures)} image(s) failed: {', '.join(failures)}")


if __name__ == "__main__":
    main()
