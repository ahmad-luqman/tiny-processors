"""Generate Python reference fixtures and run SIMD4 simulation/checks/reports."""

import argparse
import json
from pathlib import Path
import random
import re
import subprocess

from programs.simd4.matrix_mac import expected_counts, program as matrix_program, reference as matrix_reference
from programs.simd4.vector_add import program as vector_program
from tools.simd4_model import CLRA, LAST_OPCODE, MAC, MACU, MUL, RDA, execute, image, word


RESULT = re.compile(r"RESULT lanes=(\d+) wait=(\d+) cycles=(\d+) stalls=(\d+) transfers=(\d+) instructions=(\d+) fault=(\d+)")


def write_hex(path, values, width):
    path.write_text("".join(f"{value:0{width}x}\n" for value in values))


def vector_memory(length):
    rng = random.Random(0x51D4)
    memory = [rng.randrange(65536) for _ in range(256)]
    a = [65535, 32767, 32768, 0, 1, 65535, 123, 456]
    b = [1, 1, 32768, 65535, 65535, 65535, 456, 123]
    memory[:min(length, 8)] = a[:min(length, 8)]
    memory[64:64 + min(length, 8)] = b[:min(length, 8)]
    return memory


def matrix_memory(n, extreme=False):
    """Row-major A at 0x00 and B at 0x40; the extreme variant fills them with +-32767/-32768/-1."""
    rng = random.Random(0x3A7 + n)
    memory = [rng.randrange(65536) for _ in range(256)]
    if extreme:
        corners = [0x7fff, 0x8000, 0xffff, 0x0001]
        memory[:n * n] = [corners[(i + i // n) % 4] for i in range(n * n)]
        memory[64:64 + n * n] = [corners[(3 * i) % 4] for i in range(n * n)]
    else:
        # Small signed values keep ordinary products readable in the waveform.
        memory[:n * n] = [(rng.randrange(-9, 10)) % 65536 for _ in range(n * n)]
        memory[64:64 + n * n] = [(rng.randrange(-9, 10)) % 65536 for _ in range(n * n)]
    return memory


def extreme_products():
    """Every hand-computed product from the model tests, back to back through one launch.

    Each group loads a pair, accumulates it once or three times with MAC then MACU,
    reads both halves back, and stores them so the transfers pin the same values.
    """
    words = [word(2, rd=3), word(1, rd=2, imm=0x00c0)]  # r3 = lane, r2 = store base
    for a, b, times in ((0xffff, 0xffff, 1), (0x8000, 0x8000, 1), (0x8000, 0x7fff, 1),
                        (0xffff, 0x0001, 1), (0x7fff, 0x7fff, 3), (0xffff, 0xffff, 3), (0x0003, 0x0004, 1)):
        for op in (MAC, MACU):
            words += [word(1, rd=0, imm=a), word(1, rd=1, imm=b), word(CLRA)]
            words += [word(op, ra=0, rb=1)] * times
            words += [word(RDA, rd=0, imm=0), word(RDA, rd=1, imm=16), word(MUL, rd=3, ra=0, rb=1)]
            words += [word(6, rd=0, ra=2, imm=0), word(6, rd=1, ra=2, imm=1), word(4, rd=2, ra=2, imm=2)]
    words += [word(1, rd=0, imm=0x7fff), word(1, rd=1, imm=0x7fff), word(CLRA)] + [word(MAC, ra=0, rb=1)] * 3
    words += [word(RDA, rd=0, imm=1), word(RDA, rd=1, imm=31), word(RDA, rd=3, imm=33), word(MAC, ra=0, rb=0),
              word(RDA, rd=0, imm=4), word(6, rd=0, ra=2, imm=0), word(6, rd=1, ra=2, imm=1),
              word(6, rd=3, ra=2, imm=2), word(0)]
    return image(words)


EXTREMES = extreme_products()


class Runner:
    def __init__(self, simulator):
        self.simulator = simulator
        self.root = Path("build/simd4") / simulator
        self.root.mkdir(parents=True, exist_ok=True)
        self.results = []

    def run(self, name, code, memory, lanes=4, wait=0, entry=0, abort_after=None, relaunch=False, wave=False):
        reference = execute(code, memory, lanes, entry)
        prefix = self.root / name
        write_hex(Path(f"{prefix}.program.hex"), code, 8)
        write_hex(Path(f"{prefix}.memory.hex"), memory, 4)
        write_hex(Path(f"{prefix}.expected-memory.hex"), reference.memory, 4)
        write_hex(Path(f"{prefix}.retire.hex"), reference.retirements, (lanes * 96 + 64) // 4)
        write_hex(Path(f"{prefix}.transfers.hex"), reference.transfers, 7)
        write_hex(Path(f"{prefix}.final.hex"), [reference.final_state], (lanes * 96 + 24) // 4)
        write_hex(Path(f"{prefix}.meta.hex"), [len(reference.retirements), len(reference.transfers),
                  reference.base_cycles, int(reference.fault), entry], 8)
        command = (["vvp", f"build/simd4-{lanes}.vvp"] if self.simulator == "icarus"
                   else [f"build/verilator-simd4-{lanes}/simd4_sim"])
        command += [f"+case={prefix}", f"+wait={wait}"]
        if abort_after is not None:
            command.append(f"+abort-after={abort_after}")
        if relaunch:
            command.append("+relaunch")
        if wave:
            command += [f"+wave={prefix}.vcd", "+trace"]
        process = subprocess.run(command, capture_output=True, text=True, timeout=20)
        log = process.stdout + process.stderr
        Path(f"{prefix}.log").write_text(log)
        if process.returncode or "PASS: SIMD4" not in log:
            raise RuntimeError(f"{name} failed ({self.simulator}):\n{log}")
        matches = RESULT.findall(log)
        if len(matches) != (2 if relaunch else 1):
            raise RuntimeError(f"{name}: missing completion report")
        transfer_count = len(reference.transfers)
        expected_stalls = (0, transfer_count, 3 * transfer_count,
                           sum(index % 4 for index in range(transfer_count)))[wait]
        expected = (lanes, wait, reference.base_cycles + expected_stalls, expected_stalls,
                    transfer_count, len(reference.retirements), int(reference.fault))
        for match in matches:
            if tuple(map(int, match)) != expected:
                raise RuntimeError(f"{name}: measured counters {match} != independently expected {expected}")
        result = dict(zip(("lanes", "wait", "cycles", "stalls", "transfers", "instructions", "fault"), expected))
        result.update(name=name, launches=len(matches), abort_after=abort_after)
        self.results.append(result)
        return result

    def vector(self, lanes, length, wait=0, name=None, **kwargs):
        code, memory = vector_program(lanes, length), vector_memory(length)
        # A direct array calculation checks the interpreter too.
        expected = memory.copy()
        expected[128:128 + length] = [(memory[i] + memory[64 + i]) % 65536 for i in range(length)]
        if execute(code, memory, lanes).memory != expected:
            raise RuntimeError("Vector interpreter disagrees with direct Python array addition")
        return self.run(name or f"vector-{length}-lanes-{lanes}-wait-{wait}", code, memory,
                        lanes, wait, **kwargs)

    def matrix(self, lanes, n, wait=0, shift=0, extreme=False, name=None, **kwargs):
        code, memory = matrix_program(lanes, n, shift), matrix_memory(n, extreme)
        # A direct Python matrix product checks the interpreter and the kernel shape too.
        expected = memory.copy()
        expected[128:128 + n * n] = matrix_reference(memory[:n * n], memory[64:64 + n * n], n, shift)
        run = execute(code, memory, lanes)
        if run.memory != expected:
            raise RuntimeError("Matrix interpreter disagrees with direct Python matrix product")
        if (len(run.retirements), len(run.transfers)) != expected_counts(lanes, n):
            raise RuntimeError("Matrix kernel retired a different instruction/transfer count than predicted")
        return self.run(name or f"matrix-{n}-lanes-{lanes}-wait-{wait}-shift-{shift}", code, memory,
                        lanes, wait, **kwargs)

    def benchmark(self):
        rows = []
        for wait in (0, 1, 2):
            for lanes in (1, 2, 4):
                row = self.vector(lanes, 32, wait)
                rows.append(row)
                print(f"32 elements | lanes={lanes} wait={wait} | cycles={row['cycles']} "
                      f"stalls={row['stalls']} transfers={row['transfers']}")
        for wait in (0, 1, 2):
            for lanes in (1, 2, 4):
                row = self.matrix(lanes, 4, wait)
                row["kernel"] = "matrix"
                rows.append(row)
                print(f"4x4 matrix  | lanes={lanes} wait={wait} | cycles={row['cycles']} "
                      f"stalls={row['stalls']} transfers={row['transfers']} instructions={row['instructions']}")
        (self.root / "benchmark.json").write_text(json.dumps(rows, indent=2) + "\n")

    def suite(self):
        self.benchmark()
        for lanes in (1, 2, 4):
            self.vector(lanes, lanes, 3, name=f"one-group-{lanes}", relaunch=True)
            self.vector(lanes, 64, 3, name=f"full-vector-{lanes}")
            memory = vector_memory(32)
            alias = image([word(1, imm=65535), word(1, rd=1, imm=1),
                           word(3, rd=0, ra=0, rb=1), word(4, rd=2, ra=0, imm=32768),
                           word(3, rd=3, ra=2, rb=2), word(2, rd=1),
                           word(6, rd=2, ra=1, imm=254), word(5, rd=1, ra=1, imm=254),
                           word(3, rd=3, ra=1, rb=2), 0])
            self.run(f"aliases-{lanes}", alias, memory, lanes, 3)
            collision = image([word(2, rd=2), word(1, imm=511), word(6, rd=2, ra=0, imm=2),
                               word(5, rd=3, ra=0, imm=2), 0x00ffffff])
            self.run(f"collisions-{lanes}", collision, memory, lanes, 1)
            wrap = image([0])
            wrap[254:256] = [word(1, imm=32768), word(4, rd=0, ra=0, imm=32768)]
            self.run(f"pc-wrap-{lanes}", wrap, memory, lanes, entry=254)
            loop = image([word(2), word(7, imm=3), word(4, rd=0, ra=0, imm=65535), word(8, imm=2), 0])
            self.run(f"uniform-loop-{lanes}", loop, memory, lanes)
            for abort in (0, 1, 2 * lanes, 2 * lanes + 1):
                self.vector(lanes, 8, 3, name=f"reset-{lanes}-after-{abort}", abort_after=abort, relaunch=True)
            rng = random.Random(0xA11 + lanes)
            for index in range(8):
                words = [word(2)] + [word(rng.randint(1, 6), rng.randrange(4), rng.randrange(4),
                                         rng.randrange(4), rng.randrange(65536)) for _ in range(32)] + [0]
                self.run(f"mixed-{lanes}-{index}", image(words), memory, lanes, index % 4)
            rng = random.Random(0xACC + lanes)
            for index in range(8):
                # Loads, stores and every arithmetic opcode, including the accumulator ones.
                ops = [1, 2, 3, 4, 5, 6, MUL, MAC, MAC, MACU, CLRA, RDA, RDA]
                words = [word(2)] + [word(rng.choice(ops), rng.randrange(4), rng.randrange(4),
                                         rng.randrange(4), rng.randrange(65536)) for _ in range(40)] + [0]
                self.run(f"mixed-mac-{lanes}-{index}", image(words), memory, lanes, index % 4)
            self.run(f"extremes-{lanes}", EXTREMES, memory, lanes, 3)
            for n in (2, 4, 8):
                if n % lanes == 0 and (n, lanes) != (8, 1):
                    self.matrix(lanes, n, 3)
                    self.matrix(lanes, n, 1, extreme=True)
            self.matrix(lanes, 4, 0, shift=16, extreme=True)
            self.matrix(lanes, 4, 0, shift=31, extreme=True)
            # Reset inside the first row's accumulation (after the third transfer of the first group).
            self.matrix(lanes, 4, 3, name=f"matrix-reset-{lanes}", abort_after=2 * lanes + 1, relaunch=True)
            # Reset after the first MAC has changed the accumulator, then relaunch.
            self.run(f"reset-mac-{lanes}", EXTREMES, memory, lanes, 3, abort_after=2 * lanes, relaunch=True)
        for opcode in range(LAST_OPCODE + 1, 256):
            self.run(f"illegal-{opcode:02x}", image([word(1, rd=2, imm=0xabcd),
                     word(opcode, rd=1, ra=2, imm=0x44)]), [0x5a5a] * 256)
        self.run("zero-loop-count", image([word(2, rd=2), word(6, rd=2, imm=33), word(7)]),
                 [0] * 256, wait=3, relaunch=True)
        self.run("loop-without-count", image([word(1, imm=42), word(8)]), [0] * 256, relaunch=True)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--simulator", choices=("icarus", "verilator"), default="icarus")
    parser.add_argument("--mode", choices=("test", "bench", "waves"), default="test")
    args = parser.parse_args()
    runner = Runner(args.simulator)
    if args.mode == "test":
        runner.suite()
    elif args.mode == "bench":
        runner.benchmark()
    else:
        runner.vector(4, 8, name="vector-wave", wave=True)
        runner.vector(4, 8, wait=3, name="stalled-wave", wave=True)
        runner.matrix(4, 4, name="matrix-wave", wave=True)
        runner.run("overflow-wave", EXTREMES, vector_memory(8), 4, wave=True)
        print(f"Waveforms and text traces: {runner.root}/vector-wave.vcd, stalled-wave.vcd, "
              "matrix-wave.vcd and overflow-wave.vcd (.log for text)")
    (runner.root / f"{args.mode}-results.json").write_text(json.dumps(runner.results, indent=2) + "\n")
    print(f"PASS: SIMD4 {args.simulator}: {len(runner.results)} cases, "
          f"{sum(row['launches'] for row in runner.results)} completed launches")


if __name__ == "__main__":
    main()
