"""Generate Python reference fixtures and run SIMD4 simulation/checks/reports."""

import argparse
import json
from pathlib import Path
import random
import re
import subprocess

from programs.simd4.matrix_mac import (A_BASE, B_BASE, C_BASE, expected_counts, program as matrix_program,
                                       reference as matrix_reference, word_count)
from programs.simd4.vector_add import program as vector_program
from tools.simd4_model import (ADD, ADDI, CLRA, HLT, LANE, LAST_OPCODE, LDI, LOAD, LOOP, MAC, MACU, MUL, RDA,
                               SETLOOP, STORE, execute, image, word)


RESULT = re.compile(r"RESULT lanes=(\d+) wait=(\d+) cycles=(\d+) stalls=(\d+) transfers=(\d+) instructions=(\d+) fault=(\d+)")
# Neither simulator fails a run for a fixture row wider than its array: Icarus prints a
# column-0 WARNING and keeps the low bits, Verilator prints nothing. Icarus also exits 0
# after $error. So any simulator complaint fails the case, and no $display in the
# testbench may begin with one of these words.
SIMULATOR_COMPLAINT = re.compile(r"^(?:\[[^\]]*\] )?(?:WARNING|ERROR|VCD warning|%Warning|%Error)\b", re.MULTILINE)
SIMULATOR_TIMEOUT = 20
CORNERS = (0x7fff, 0x8000, 0xffff, 0x0001)  # 32767, -32768, -1, 1
UNUSED_FIELD_BITS = 0x30000                  # instruction bits [17:16], which every opcode ignores


def write_hex(path, values, bits):
    """Write one row per value at the width of the testbench array that will $readmemh it."""
    digits = (bits + 3) // 4
    for value in values:
        if not 0 <= value < 1 << bits:
            raise ValueError(f"{path.name}: {value:#x} does not fit {bits} bits")
    path.write_text("".join(f"{value:0{digits}x}\n" for value in values))


def failure_reasons(returncode, log):
    """Why a simulator run is rejected: a nonzero exit, a missing PASS line, or a complaint."""
    return [reason for reason, hit in ((f"exit status {returncode}", returncode),
                                       ("no PASS line", "PASS: SIMD4" not in log),
                                       ("simulator warning or error", SIMULATOR_COMPLAINT.search(log))) if hit]


def vector_memory(length):
    rng = random.Random(0x51D4)
    memory = [rng.randrange(65536) for _ in range(256)]
    a = [65535, 32767, 32768, 0, 1, 65535, 123, 456]
    b = [1, 1, 32768, 65535, 65535, 65535, 456, 123]
    memory[:min(length, 8)] = a[:min(length, 8)]
    memory[64:64 + min(length, 8)] = b[:min(length, 8)]
    return memory


def matrix_memory(n, extreme=False):
    """Row-major A at A_BASE and B at B_BASE for the matrix kernel.

    The ordinary variant uses small nonzero signed values so the waveform products stay
    readable and every partial sum is live. The extreme variant rotates the corner values
    32767, -32768, -1 and 1 through the rows of A and the columns of B (a 2x2 row holds two
    of them), then fills row 0 of A and column 0 of B with 32767 so that C[0][0] accumulates
    n * 32767^2, which passes 2^31 for n = 4 and wraps past 2^32 for n = 8.
    """
    rng = random.Random(0x3A7 + n)
    memory = [rng.randrange(65536) for _ in range(256)]
    if extreme:
        memory[A_BASE:A_BASE + n * n] = [0x7fff if i < n else CORNERS[(i + i // n) % 4] for i in range(n * n)]
        memory[B_BASE:B_BASE + n * n] = [0x7fff if i % n == 0 else CORNERS[(3 * i + i // n) % 4] for i in range(n * n)]
    else:
        small = [v for v in range(-9, 10) if v]
        memory[A_BASE:A_BASE + n * n] = [rng.choice(small) % 65536 for _ in range(n * n)]
        memory[B_BASE:B_BASE + n * n] = [rng.choice(small) % 65536 for _ in range(n * n)]
    return memory


def extreme_products():
    """The hand-computed operand pairs from the model tests, back to back in one launch.

    Each group clears, accumulates a pair once, three or five times with MAC then MACU,
    reads both halves back, multiplies the read-back halves into r3, and stores the two
    halves at the same addresses in every lane. The tails exercise RDA shifts 1, 31, 33
    (mod 32) and 4, the MAC r0, r0 alias, the fields CLRA, RDA, MAC and MACU must ignore,
    instruction bits [17:16], and lane-distinct MUL/MACU operands.
    """
    words = [word(LANE, rd=3), word(LDI, rd=2, imm=0x00c0)]  # r3 = lane until the first MUL, r2 = store base
    for a, b, times in ((0xffff, 0xffff, 1), (0x8000, 0x8000, 1), (0x8000, 0x7fff, 1), (0xffff, 0x0001, 1),
                        (0x7fff, 0x7fff, 3), (0xffff, 0xffff, 3), (0x0003, 0x0004, 1), (0x0000, 0xffff, 5)):
        for op in (MAC, MACU):
            words += [word(LDI, rd=0, imm=a), word(LDI, rd=1, imm=b), word(CLRA)]
            words += [word(op, ra=0, rb=1)] * times
            words += [word(RDA, rd=0), word(RDA, rd=1, imm=16), word(MUL, rd=3, ra=0, rb=1)]
            words += [word(STORE, rd=0, ra=2), word(STORE, rd=1, ra=2, imm=1), word(ADDI, rd=2, ra=2, imm=2)]
    # acc = 3 * 32767^2 = bffd0003: windows past bit 31 (shift 31; 33 wraps to 1), then MAC r0, r0
    # squares the read-back and RDA 4 reads it back for the store.
    words += [word(LDI, rd=0, imm=0x7fff), word(LDI, rd=1, imm=0x7fff), word(CLRA)] + [word(MAC, ra=0, rb=1)] * 3
    words += [word(RDA, rd=0, imm=1), word(RDA, rd=1, imm=31), word(RDA, rd=3, imm=33), word(MAC, ra=0, rb=0),
              word(RDA, rd=0, imm=4), word(STORE, rd=0, ra=2), word(STORE, rd=1, ra=2, imm=1),
              word(STORE, rd=3, ra=2, imm=2)]
    # Ignored fields, on a live accumulator so the read-back values are nonzero: RDA ignores
    # ra/rb (reads fffc), CLRA ignores every field, MAC/MACU ignore rd, MUL may write an operand.
    words += [word(MAC, ra=1, rb=1), word(RDA, rd=2, ra=3, rb=1, imm=16), word(CLRA, rd=3, ra=1, rb=2, imm=0xffff),
              word(MAC, rd=2, ra=0, rb=1), word(MACU, rd=1, ra=0, rb=1), word(RDA, rd=2, imm=2),
              word(MUL, rd=0, ra=0, rb=1), word(MUL, rd=1, ra=0, rb=1)]
    # Instruction bits [17:16] set on every accumulator opcode: word() cannot emit them.
    words += [w | UNUSED_FIELD_BITS for w in (word(MAC, ra=0, rb=1), word(MACU, ra=1, rb=1), word(RDA, rd=3, imm=8),
                                              word(MUL, rd=2, ra=0, rb=1), word(CLRA))]
    # Lane-distinct operands through MUL, MACU and a MACU self-alias; MAC/MACU with rd set.
    # r3 = lane * 0x101, so lane 3 accumulates 0x303 * 0x101 + 0x303^2 = 000c180c.
    words += [word(LANE, rd=3), word(LDI, rd=1, imm=0x0101), word(MUL, rd=3, ra=3, rb=1),
              word(MAC, rd=3, ra=3, rb=1), word(MACU, rd=1, ra=3, rb=3), word(RDA, rd=3), word(HLT)]
    return image(words)


def rda_sweep():
    """Read a negative accumulator (via MAC, then the same bits via MACU) and a positive one back
    through every shift, plus out-of-range immediates."""
    shifts = list(range(32)) + [32, 47, 0x7fff, 0xffff]
    words = [word(LDI, rd=0, imm=0x7fff), word(LDI, rd=1, imm=0x7fff)]
    for accumulate in ([word(MAC, ra=0, rb=1)] * 3,                  # bffd0003, reads as negative
                       [word(CLRA)] + [word(MACU, ra=0, rb=1)] * 3,   # the same bits through the unsigned path
                       [word(CLRA), word(MAC, ra=0, rb=1)]):          # 3fff0001, positive
        # Alternating rd shows the previous read-back surviving in the other register.
        words += accumulate + [word(RDA, rd=2 + (i & 1), imm=shift) for i, shift in enumerate(shifts)]
    return image(words + [word(HLT)])


def fault_after_mac(bad):
    """acc = sext(0xabcd) * 3 = ffff0367 and r0 = 3 in every lane before a word that must fault."""
    code = image([word(LDI, rd=2, imm=0xabcd), word(LDI, rd=0, imm=3), word(MAC, ra=2, rb=0), bad])
    if not execute(code, [0] * 256).fault:
        raise ValueError(f"word {bad:#010x} does not fault")
    return code


def overflow_then_read():
    """Three MACs of 7fff^2 and their read-back, for resets taken while a MAC or an RDA is executing."""
    return image([word(LDI, rd=0, imm=0x7fff), word(LDI, rd=1, imm=0x7fff)] + [word(MAC, ra=0, rb=1)] * 3 +
                 [word(RDA, rd=2), word(RDA, rd=3, imm=16), word(STORE, rd=2, ra=1), word(HLT)])


EXTREMES = extreme_products()
RDA_SWEEP = rda_sweep()
OVERFLOW = overflow_then_read()


class Runner:
    def __init__(self, simulator):
        self.simulator = simulator
        self.root = Path("build/simd4") / simulator
        self.root.mkdir(parents=True, exist_ok=True)
        self.results = []

    def binary(self, lanes):
        return (["vvp", f"build/simd4-{lanes}.vvp"] if self.simulator == "icarus"
                else [f"build/verilator-simd4-{lanes}/simd4_sim"])

    def run(self, name, code, memory, lanes=4, wait=0, entry=0, abort_after=None, abort_at_retire=None,
            relaunch=False, wave=False, expect_nonzero_acc=False, binary_lanes=None):
        if expect_nonzero_acc and abort_after is None and abort_at_retire is None:
            raise ValueError(f"{name}: expect_nonzero_acc needs an abort point")
        reference = execute(code, memory, lanes, entry)
        prefix = self.root / name
        write_hex(Path(f"{prefix}.program.hex"), code, 32)
        write_hex(Path(f"{prefix}.memory.hex"), memory, 16)
        write_hex(Path(f"{prefix}.expected-memory.hex"), reference.memory, 16)
        write_hex(Path(f"{prefix}.retire.hex"), reference.retirements, reference.record_bits)
        write_hex(Path(f"{prefix}.transfers.hex"), reference.transfers, 25)
        write_hex(Path(f"{prefix}.final.hex"), [reference.final_state], reference.state_bits)
        # Row order matches the META_* localparams in tests/simd4_tb.sv.
        write_hex(Path(f"{prefix}.meta.hex"), [len(reference.retirements), len(reference.transfers),
                  reference.base_cycles, int(reference.fault), entry, reference.state_bits,
                  reference.record_bits], 32)
        command = self.binary(binary_lanes or lanes) + [f"+case={prefix}", f"+wait={wait}"]
        if abort_after is not None:
            command.append(f"+abort-after={abort_after}")
        if abort_at_retire is not None:
            command.append(f"+abort-at-retire={abort_at_retire}")
        if relaunch:
            command.append("+relaunch")
        if expect_nonzero_acc:
            command.append("+expect-nonzero-acc")
        if wave:
            command += [f"+wave={prefix}.vcd", "+trace"]
        log_path = Path(f"{prefix}.log")
        try:
            process = subprocess.run(command, capture_output=True, text=True, timeout=SIMULATOR_TIMEOUT)
        except subprocess.TimeoutExpired as timeout:
            # The captured streams are bytes here even under text mode.
            log = "".join((stream or b"").decode(errors="replace") for stream in (timeout.stdout, timeout.stderr))
            log_path.write_text(log)
            raise RuntimeError(f"{name} timed out after {SIMULATOR_TIMEOUT} s ({self.simulator}):\n{log}") from timeout
        log = process.stdout + process.stderr
        log_path.write_text(log)
        reasons = failure_reasons(process.returncode, log)
        if reasons:
            raise RuntimeError(f"{name} failed ({self.simulator}): {', '.join(reasons)}\n{log}")
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
        result.update(name=name, launches=len(matches), abort_after=abort_after, abort_at_retire=abort_at_retire)
        self.results.append(result)
        return result

    def expect_rejection(self, name, pattern, **kwargs):
        """A run the testbench must refuse, with `pattern` in its log; the suite's negative control."""
        try:
            self.run(name, **kwargs)
        except RuntimeError as error:
            if not re.search(pattern, str(error)):
                raise RuntimeError(f"{name}: rejected for another reason:\n{error}") from error
        else:
            raise RuntimeError(f"{name}: the testbench accepted a fixture it must reject")
        self.results.append(dict(name=name, launches=0, rejected=pattern))

    def vector(self, lanes, length, wait=0, name=None, **kwargs):
        code, memory = vector_program(lanes, length), vector_memory(length)
        # A direct array calculation checks the interpreter too.
        expected = memory.copy()
        expected[128:128 + length] = [(memory[i] + memory[64 + i]) % 65536 for i in range(length)]
        run = execute(code, memory, lanes)
        if run.fault:
            raise RuntimeError(f"vector kernel lanes={lanes} length={length}: reference faulted")
        if run.memory != expected:
            raise RuntimeError(f"vector kernel lanes={lanes} length={length}: interpreter disagrees with "
                               f"direct Python addition at word {next(i for i in range(256) if run.memory[i] != expected[i])}")
        return self.run(name or f"vector-{length}-lanes-{lanes}-wait-{wait}", code, memory,
                        lanes, wait, **kwargs)

    def matrix(self, lanes, n, wait=0, shift=0, extreme=False, name=None, **kwargs):
        code, memory = matrix_program(lanes, n, shift), matrix_memory(n, extreme)
        # A direct Python matrix product checks the interpreter and the kernel shape too.
        expected = memory.copy()
        expected[C_BASE:C_BASE + n * n] = matrix_reference(memory[A_BASE:A_BASE + n * n],
                                                           memory[B_BASE:B_BASE + n * n], n, shift)
        run = execute(code, memory, lanes)
        label = f"matrix kernel lanes={lanes} n={n} shift={shift} extreme={extreme}"
        if run.fault:
            raise RuntimeError(f"{label}: reference faulted")
        if run.memory != expected:
            raise RuntimeError(f"{label}: interpreter disagrees with direct Python product at word "
                               f"{next(i for i in range(256) if run.memory[i] != expected[i])}")
        if (len(run.retirements), len(run.transfers)) != expected_counts(lanes, n):
            raise RuntimeError(f"{label}: retired {len(run.retirements)} instructions and {len(run.transfers)} "
                               f"transfers, predicted {expected_counts(lanes, n)}")
        variant = "-extreme" if extreme else ""
        return self.run(name or f"matrix-{n}-lanes-{lanes}-wait-{wait}-shift-{shift}{variant}", code, memory,
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
                ops = [LDI, LANE, ADD, ADDI, LOAD, STORE, MUL, MAC, MAC, MACU, CLRA, RDA, RDA]
                words = [word(LANE)] + [word(rng.choice(ops), rng.randrange(4), rng.randrange(4),
                                            rng.randrange(4), rng.randrange(65536)) for _ in range(40)] + [word(HLT)]
                self.run(f"mixed-mac-{lanes}-{index}", image(words), memory, lanes, index % 4)
            self.run(f"extremes-{lanes}", EXTREMES, memory, lanes, 3)
            self.run(f"rda-sweep-{lanes}", RDA_SWEEP, memory, lanes, 1)
            for n in (2, 4, 8):
                if n % lanes == 0 and word_count(lanes, n) <= 256:
                    self.matrix(lanes, n, 3)
                    self.matrix(lanes, n, 1, extreme=True)
                    self.matrix(lanes, n, 0, shift=16, extreme=True)
            self.matrix(lanes, 4, 0, shift=31, extreme=True)
            # Reset inside row 0 of column group 0 of the corner matrices: after both k=0 operand
            # loads, the first MAC and one transfer into the k=1 A load (2*lanes+1 transfers), while
            # the next request (the following lane, or the B load on one lane) is pending.
            self.matrix(lanes, 4, 3, extreme=True, name=f"matrix-reset-{lanes}", abort_after=2 * lanes + 1,
                        relaunch=True, expect_nonzero_acc=True)
            # Reset while the accumulators hold the first MACU result (fffe0001) and its store is
            # pending; the relaunch must start from zero.
            self.run(f"reset-mac-{lanes}", EXTREMES, memory, lanes, 3, abort_after=2 * lanes, relaunch=True,
                     expect_nonzero_acc=True)
            # Reset while the second MAC (after three retirements) or the first RDA (after five) is
            # in EXECUTE with acc = 3fff0001 / bffd0003: the pending write must not land.
            for retired, phase in ((3, "mac"), (5, "rda")):
                self.run(f"reset-in-{phase}-{lanes}", OVERFLOW, memory, lanes, abort_at_retire=retired,
                         relaunch=True, expect_nonzero_acc=True)
            if lanes != 4:
                # The full sweep below runs at four lanes; repeat a sample on one and two lanes.
                for tag, bad in (("0e", word(LAST_OPCODE + 1, rd=1, ra=2, imm=0x44)), ("1a", word(0x1a, rd=1, ra=2, imm=0x44)),
                                 ("8d", word(0x8d, rd=1, ra=2, imm=0x44)), ("ff", word(0xff, rd=1, ra=2, imm=0x44)),
                                 ("setloop-0", word(SETLOOP)), ("loop-0", word(LOOP))):
                    self.run(f"fault-{tag}-lanes-{lanes}", fault_after_mac(bad), [0x5a5a] * 256, lanes)
        for opcode in range(LAST_OPCODE + 1, 256):
            self.run(f"illegal-{opcode:02x}", fault_after_mac(word(opcode, rd=1, ra=2, imm=0x44)), [0x5a5a] * 256)
        self.run("zero-loop-count", image([word(LANE, rd=2), word(STORE, rd=2, imm=33), word(LDI, rd=0, imm=3),
                                           word(LDI, rd=1, imm=0xabcd), word(MAC, ra=1, rb=0), word(SETLOOP)]),
                 [0] * 256, wait=3, relaunch=True)
        self.run("loop-without-count", fault_after_mac(word(LOOP)), [0] * 256, relaunch=True)
        # A two-lane fixture through the four-lane binary must be refused by the width handshake.
        self.expect_rejection("width-mismatch", "Fixture snapshot width", code=OVERFLOW, memory=[0] * 256,
                              lanes=2, binary_lanes=4)


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
