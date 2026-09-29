"""Track 0: the M extension and the Zicntr counters on both backends.

Every program runs on the emulator and the RTL. Programs that read only `instret`
are compared trace for trace; programs that read `cycle` or `time` read device
time (docs/rv32.md) and are checked structurally on each backend instead. Results
are also asserted against hand-computed values and a Python reference of the M
extension, so neither backend is checked only against the other.
"""
import random
import unittest

import test_rv32_rtl as integer_tests
from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_f_asm import arithmetic, fli
from tools.rv32_rtl import cycle_relation, diff_traces, reads_device_time

effects = integer_tests.effects
MASK = 0xFFFFFFFF
MD_WAITS = 33  # cycles in MD_WAIT per M instruction: 32 steps, then the cycle that reads the result


def signed(value):
    return value - (1 << 32) if value & 0x80000000 else value


def reference(funct3, a, b):
    """The M extension from the specification's definitions, in Python integers."""
    sa, sb = signed(a), signed(b)
    if funct3 == 0: return (a * b) & MASK
    if funct3 == 1: return ((sa * sb) >> 32) & MASK
    if funct3 == 2: return ((sa * b) >> 32) & MASK
    if funct3 == 3: return ((a * b) >> 32) & MASK
    if funct3 == 4:
        if b == 0: return MASK
        if sa == -2**31 and sb == -1: return a
        quotient = abs(sa) // abs(sb)  # C and RISC-V round toward zero
        return (-quotient if (sa < 0) != (sb < 0) else quotient) & MASK
    if funct3 == 5: return MASK if b == 0 else a // b
    if funct3 == 6:
        if b == 0: return a
        if sa == -2**31 and sb == -1: return 0
        remainder = abs(sa) % abs(sb)
        return (-remainder if sa < 0 else remainder) & MASK
    return a if b == 0 else a % b


EDGES = (0, 1, 2, 3, 7, 0x7FFFFFFF, 0x80000000, 0x80000001, 0xFFFFFFFF, 0xFFFFFFFE, 0xFFFFFFF9,
         0x0000B505, 0xFFFF4AFB, 0x55555555, 0xAAAAAAAA, 0x00010000)
DATA = RAM + 0x1000  # a scratch word past every image here


def last_restart(trace):
    """The index of the last trace line at the reset vector: where the run after a reset begins."""
    return [i for i, line in enumerate(trace) if line.split()[1] == "80000000"][-1]


class Track0Harness:
    """test_rv32_rtl's emulator-and-RTL harness, borrowed without its tests."""
    setUpClass = classmethod(integer_tests.RtlTest.setUpClass.__func__)
    tearDownClass = classmethod(integer_tests.RtlTest.tearDownClass.__func__)
    run_both = integer_tests.RtlTest.run_both
    assert_same_pass = integer_tests.RtlTest.assert_same_pass
    assert_same_double_fault = integer_tests.RtlTest.assert_same_double_fault


class MultiplyDivideTest(Track0Harness, unittest.TestCase):
    def check_cases(self, cases, **kwargs):
        """Each case loads rs1/rs2, runs one M instruction and must retire `expected` in rd on both
        backends; rd rotates over every register, unless a case gives it as a fourth element, and
        aliases a source in some cases."""
        words, checks = [], []
        for i, (funct3, a, b, *given_rd) in enumerate(cases):
            rd = given_rd[0] if given_rd else i % 32
            rs1, rs2 = (rd, rd) if i % 7 == 3 else ((rd, 5) if i % 7 == 5 else (6, 7))
            if rs1 == rs2 and a != b:
                rs1, rs2 = 6, 7
            if 0 in (rs1, rs2):
                rs1, rs2 = 6, 7
            words += LI(rs1, a) + LI(rs2, b)
            words.append(M_OPS[funct3](rd, rs1, rs2))
            expected = reference(funct3, a, b)
            checks.append((len(words) - 1, "" if rd == 0 else f"x{rd}={expected:08x}", (funct3, hex(a), hex(b))))
        emulator, rtl = self.assert_same_pass(words + FINISH(), max_cycles=200 * len(words) + 1000, **kwargs)
        for index, effect, case in checks:
            self.assertEqual(effects(rtl.trace[index]), effect, f"case={case}, line={rtl.trace[index]}")
        text, holds = cycle_relation(rtl)
        self.assertTrue(holds, text)
        self.assertEqual(rtl.halt["md_waits"], MD_WAITS * len(cases))
        return emulator, rtl

    def test_hand_computed_results_and_the_two_corner_cases(self):
        rows = [
            (MUL, 7, 0xFFFFFFFD, 0xFFFFFFEB),           # 7 * -3 = -21
            (MUL, 0x10000, 0x10000, 0),                 # 2^32 wraps to zero
            (MULH, 0x80000000, 0x80000000, 0x40000000), # (-2^31)^2 = 2^62
            (MULH, 0xFFFFFFFF, 0x00000001, 0xFFFFFFFF), # -1 * 1: high half of -1
            (MULHSU, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFF),  # -1 * (2^32 - 1) = -2^32 + 1
            (MULHU, 0xFFFFFFFF, 0xFFFFFFFF, 0xFFFFFFFE),   # (2^32 - 1)^2 = 0xfffffffe_00000001
            (DIV, 0xFFFFFFF9, 2, 0xFFFFFFFD),           # -7 / 2 = -3, toward zero
            (REM, 0xFFFFFFF9, 2, 0xFFFFFFFF),           # -7 % 2 = -1, the dividend's sign
            (DIV, 7, 0xFFFFFFFE, 0xFFFFFFFD),           # 7 / -2 = -3
            (REM, 7, 0xFFFFFFFE, 1),
            (DIVU, 0x80000000, 3, 0x2AAAAAAA),
            (REMU, 0x80000000, 3, 2),
            (DIV, 0x80000000, 0xFFFFFFFF, 0x80000000),  # the one signed overflow
            (REM, 0x80000000, 0xFFFFFFFF, 0),
            (DIV, 5, 0, 0xFFFFFFFF),                    # division by zero: all ones ...
            (DIV, 0xFFFFFFF9, 0, 0xFFFFFFFF),           # ... whatever the dividend's sign
            (DIVU, 5, 0, 0xFFFFFFFF),
            (REM, 0xFFFFFFFB, 0, 0xFFFFFFFB),           # and the dividend as the remainder
            (REMU, 5, 0, 5),
        ]
        words, checks = [], []
        for op, a, b, expected in rows:
            self.assertEqual(reference(M_OPS.index(op), a, b), expected, (op.__name__, a, b))
            words += LI(6, a) + LI(7, b) + [op(8, 6, 7)]
            checks.append((len(words) - 1, f"x8={expected:08x}"))
        for stall in (0, 2):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(words + FINISH(), stall=stall)
                for index, effect in checks:
                    self.assertEqual(effects(rtl.trace[index]), effect, rtl.trace[index])
                self.assertEqual(rtl.halt["md_waits"], MD_WAITS * len(rows))
                self.assertTrue(cycle_relation(rtl)[1], cycle_relation(rtl)[0])

    def test_seeded_vectors_against_the_python_reference(self):
        rng = random.Random(20260929)
        cases = [(funct3, a, b) for funct3 in range(8) for a in EDGES[:8] for b in EDGES[:8]]
        cases += [(rng.randrange(8), rng.choice(EDGES) if rng.random() < 0.3 else rng.getrandbits(32),
                   rng.choice(EDGES) if rng.random() < 0.3 else rng.getrandbits(32)) for _ in range(300)]
        self.check_cases(cases, seed=7)

    def test_every_operation_writes_x0_as_a_no_op_and_aliases_its_sources(self):
        cases = [(funct3, a, b) for funct3 in range(8) for a, b in ((0xFFFFFFF9, 0xFFFFFFF9), (6, 0), (0x80000000, 0xFFFFFFFF))]
        cases += [(funct3, 0xFFFFFFF9, 0x80000003, 0) for funct3 in range(8)]  # rd = x0 for every operation
        _, rtl = self.check_cases(cases, stall=1)
        x0_words = {f"{M_OPS[funct3](0, 6, 7):08x}" for funct3 in range(8)}
        self.assertEqual({line.split()[2] for line in rtl.trace} & x0_words, x0_words, "each x0 case retired")

    def test_back_to_back_dependent_operations(self):
        # Each operation reads the one before it with nothing in between, and one writes its own
        # source; the expected values chain through the Python reference.
        a, b = 0x0012D687, 0xFFFFFFF3  # 1234567 and -13
        chain = [(MUL, 8, 6, 7), (DIV, 9, 8, 7), (REM, 10, 9, 7), (DIV, 8, 8, 7), (MULHU, 11, 10, 8),
                 (REMU, 12, 11, 7), (MULH, 13, 12, 12), (DIVU, 14, 13, 6), (MULHSU, 15, 14, 9)]
        registers = {6: a, 7: b}
        words, checks = LI(6, a) + LI(7, b), []
        for op, rd, rs1, rs2 in chain:
            registers[rd] = reference(M_OPS.index(op), registers[rs1], registers[rs2])
            words.append(op(rd, rs1, rs2))
            checks.append((len(words) - 1, f"x{rd}={registers[rd]:08x}"))
        self.assertEqual(registers[9], a)  # 1234567 * -13 / -13: the product fits 32 bits
        for stall in (0, 2):
            with self.subTest(stall=stall):
                _, rtl = self.assert_same_pass(words + FINISH(), stall=stall)
                for index, effect in checks:
                    self.assertEqual(effects(rtl.trace[index]), effect, rtl.trace[index])
                self.assertEqual(rtl.halt["md_waits"], MD_WAITS * len(chain))
                self.assertTrue(cycle_relation(rtl)[1], cycle_relation(rtl)[0])

    def test_operation_directly_after_a_load_and_after_a_floating_operation(self):
        # The M operation reads the register the load or the FPU conversion just wrote. fli
        # clobbers x6, so the operands live elsewhere.
        value, divisor = 0x07654321, 0xFFFFFFFB
        words = LI(9, DATA) + LI(10, value) + [SW(10, 9, 0)] + LI(7, divisor)
        words += [LW(8, 9, 0), MUL(11, 8, 7)]
        after_load = len(words) - 1
        words += fli(1, 0x40490FDB) + [arithmetic(11, 0, rd=13, rs1=1), DIV(14, 10, 13)]  # fcvt.w.s x13 = 3
        after_fp = len(words) - 1
        words += [arithmetic(0, 0, rd=2, rs1=1, rs2=1), REM(15, 10, 7)]  # fadd.s, then an M op
        after_fadd = len(words) - 1
        for kwargs in ({"stall": 3}, {"seed": 11}):
            with self.subTest(**kwargs):
                _, rtl = self.assert_same_pass(words + FINISH(), **kwargs)
                self.assertEqual(effects(rtl.trace[after_load]), f"x11={reference(0, value, divisor):08x}")
                self.assertTrue(effects(rtl.trace[after_fp - 1]).startswith("x13=00000003"), rtl.trace[after_fp - 1])
                self.assertEqual(effects(rtl.trace[after_fp]), f"x14={reference(4, value, 3):08x}")
                self.assertEqual(effects(rtl.trace[after_fadd]), f"x15={reference(6, value, divisor):08x}")
                self.assertEqual(rtl.halt["md_waits"], MD_WAITS * 3)
                self.assertTrue(cycle_relation(rtl)[1], cycle_relation(rtl)[0])

    def test_cost_is_fixed_and_independent_of_the_operands(self):
        # 33 wait cycles for every operation and every operand, including division by zero:
        # the unit always runs all 32 steps (docs/rv32-groundwork.md).
        for op, a, b in ((MUL, 0, 0), (MULHU, MASK, MASK), (DIV, 5, 0), (REMU, MASK, 1)):
            with self.subTest(op=op.__name__):
                words = LI(6, a) + LI(7, b) + [op(8, 6, 7)] + FINISH()
                _, rtl = self.assert_same_pass(words, stall=0)
                self.assertEqual(rtl.halt["md_waits"], MD_WAITS)
                # Four cycles per instruction, one more for the done store, and the waits.
                self.assertEqual(rtl.halt["cycles"], 4 * len(words) + 1 + MD_WAITS)

    def test_reset_during_the_wait_discards_the_result(self):
        # The prefix reads rd before the operation, so a replay that saw a stale result would show.
        words = [ADDI(8, 8, 0)] + LI(6, 0xFFFFFFF9) + LI(7, 2) + [DIV(8, 6, 7), ADDI(9, 8, 0)] + FINISH()
        emulator, baseline = self.assert_same_pass(words)
        start_cycle = 4 * 5 + 2  # five instructions, then the DIV's FETCH and DECODE; EXECUTE starts the unit
        for reset_at in (start_cycle + 1, start_cycle + 2, start_cycle + 17, start_cycle + MD_WAITS, start_cycle + MD_WAITS + 1):
            with self.subTest(reset_at=reset_at):
                _, rtl = self.run_both(words, reset_at=reset_at)
                restart = last_restart(rtl.trace)
                self.assertGreater(restart, 0, "the reset landed inside the program")
                self.assertEqual([" ".join(line.split()[1:]) for line in rtl.trace[restart:]],
                                 [" ".join(line.split()[1:]) for line in emulator.trace])
                self.assertFalse(any(line.split()[2] == f"{DIV(8, 6, 7):08x}" for line in rtl.trace[:restart]),
                                 "the interrupted divide never retired")
                self.assertEqual(rtl.halt["outcome"], "pass")
        self.assertEqual(effects(baseline.trace[5]), "x8=fffffffd")


class CounterTest(Track0Harness, unittest.TestCase):

    def test_instret_counts_retirements_and_is_trace_comparable(self):
        # Each read sees the instructions retired before it; a trapped instruction does not
        # retire, so the handler's first read is one behind the step count.
        handler_at = RAM + 0x100
        handler = [RDINSTRET(12), CSRRS(13, MEPC, 0), ADDI(13, 13, 4), CSRRW(0, MEPC, 13), MRET()]
        body = [RDINSTRET(1), ADDI(0, 0, 0), RDINSTRET(2), RDINSTRETH(3), ECALL(), RDINSTRET(4)]
        words = LI(5, handler_at) + [CSRRW(0, MTVEC, 5)] + body + FINISH()
        words += [0] * ((handler_at - RAM) // 4 - len(words)) + handler
        self.assertFalse(reads_device_time([f"1 80000000 {word:08x}" for word in words]))
        for stall in (0, 3):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(words, stall=stall)
                by_word = [(line.split()[2], effects(line)) for line in rtl.trace]
                self.assertEqual(by_word[3], (f"{RDINSTRET(1):08x}", "x1=00000003"))
                self.assertEqual(by_word[5], (f"{RDINSTRET(2):08x}", "x2=00000005"))
                self.assertEqual(by_word[6], (f"{RDINSTRETH(3):08x}", "x3=00000000"))
                self.assertIn(" trap 11 ", rtl.trace[7])
                self.assertEqual(effects(rtl.trace[8]), "x12=00000007", "the ecall did not retire")
                self.assertEqual(effects(rtl.trace[13]), "x4=0000000c", "seven, the handler's five, then the read")

    def test_instret_after_each_kind_of_instruction(self):
        # With no trap, a read at trace line i has i retirements before it, whatever came before
        # it: an M op, a store, a load, a taken branch (whose skipped word never retires), a
        # branch not taken, and FPU operations.
        words = LI(9, DATA) + LI(6, 100) + LI(7, 7)
        words += [DIV(8, 6, 7), RDINSTRET(20), SW(8, 9, 0), RDINSTRET(21), LW(10, 9, 0), RDINSTRET(22),
                  BEQ(8, 10, 8), ADDI(0, 0, 1), RDINSTRET(23), BNE(8, 10, 8), RDINSTRET(24)]
        words += fli(1, 0x3F800000) + [arithmetic(0, 0, rd=2, rs1=1, rs2=1), RDINSTRET(25),
                                        arithmetic(11, 1, rd=16, rs1=2), RDINSTRET(26), RDINSTRETH(27)]
        skipped = f"{ADDI(0, 0, 1):08x}"
        reads = {f"{RDINSTRET(rd):08x}": rd for rd in range(20, 27)}
        self.assertFalse(reads_device_time([f"1 80000000 {word:08x}" for word in words]))
        for stall in (0, 3):
            with self.subTest(stall=stall):
                _, rtl = self.assert_same_pass(words + FINISH(), stall=stall)
                seen = []
                for i, line in enumerate(rtl.trace):
                    word = line.split()[2]
                    self.assertNotEqual(word, skipped, "the taken branch's shadow retired")
                    if word in reads:
                        self.assertEqual(effects(line), f"x{reads[word]}={i:08x}", line)
                        seen.append(reads[word])
                    if word == f"{RDINSTRETH(27):08x}":
                        self.assertEqual(effects(line), "x27=00000000")
                self.assertEqual(seen, list(range(20, 27)))
                self.assertEqual(effects(rtl.trace[len(LI(9, DATA) + LI(6, 100) + LI(7, 7))]), "x8=0000000e")

    def test_cycle_and_time_read_device_ticks(self):
        words = [RDCYCLE(1), RDCYCLE(2), RDTIME(3), RDCYCLEH(4), RDTIMEH(5), ADDI(0, 0, 0), ADDI(0, 0, 0),
                 RDCYCLE(6)] + FINISH()
        self.assertTrue(reads_device_time([f"1 80000000 {word:08x}" for word in words]))
        for stall in (0, 2):
            with self.subTest(stall=stall):
                emulator, rtl = self.run_both(words, stall=stall)
                for run in (emulator, rtl):
                    self.assertEqual((run.halt["halt"], run.halt["outcome"]), ("done", "pass"), run.stderr)
                read = lambda run, i: int(effects(run.trace[i]).split("=")[1], 16)
                # The emulator: a device tick is an executed instruction, so each read shows the
                # steps before it, whatever the stalls.
                self.assertEqual([read(emulator, i) for i in (0, 1, 2, 3, 4, 7)], [0, 1, 2, 0, 0, 7])
                # The RTL: a tick is a clock cycle. Instruction k's EXECUTE is its third cycle, and
                # every instruction here costs four cycles plus the stall of its fetch. Since Track 1
                # `time` reads the CLINT's mtime, the count of the reading cycle itself, one more
                # than `cycle`, which counts the cycles before it.
                per = 4 + stall
                self.assertEqual([read(rtl, i) for i in (0, 1, 2, 3, 4, 7)],
                                 [2 + stall, 2 + stall + per, 3 + stall + 2 * per, 0, 0, 2 + stall + 7 * per])
                self.assertEqual(rtl.console, emulator.console)
                self.assertIsNotNone(diff_traces(rtl.trace, emulator.trace), "device time differs by design")

    def test_writes_to_counters_and_absent_counters_are_illegal(self):
        illegal = [CSRRW(0, CYCLE, 1), CSRRW(1, INSTRET, 0), CSRRS(1, CYCLE, 2), CSRRC(1, TIMEH, 2),
                   CSRRWI(0, INSTRETH, 0), CSRRSI(1, CYCLE, 1), CSRRCI(1, TIME, 3),
                   CSRRS(1, 0xC03, 0),   # hpmcounter3: Zihpm is not implemented
                   CSRRS(1, 0xB00, 0),   # mcycle: the machine-mode counters are not implemented
                   CSRRS(1, 0xC83, 0)]
        for word in illegal:
            with self.subTest(word=f"{word:08x}"):
                words = [ADDI(1, 0, 1), word, ADDI(2, 0, 2)] + FINISH()
                self.assert_same_double_fault(words, 2, word, stall=1)
        # Reading forms with a zero rs1 field are reads, not writes, and retire.
        reads = [CSRRS(1, INSTRET, 0), CSRRC(2, INSTRETH, 0), CSRRSI(3, INSTRET, 0), CSRRCI(4, INSTRET, 0)]
        emulator, rtl = self.assert_same_pass(reads + FINISH(), stall=1)
        self.assertEqual([effects(line) for line in rtl.trace[:4]],
                         ["x1=00000000", "x2=00000000", "x3=00000002", "x4=00000003"])

    def test_counters_restart_at_reset(self):
        words = [ADDI(0, 0, 0)] * 6 + [RDINSTRET(1), RDCYCLE(2)] + FINISH()
        _, rtl = self.run_both(words, reset_at=15)
        restart = last_restart(rtl.trace)
        self.assertGreater(restart, 0)
        self.assertEqual(effects(rtl.trace[restart + 6]), "x1=00000006")
        self.assertEqual(effects(rtl.trace[restart + 7]), f"x2={4 * 7 + 2:08x}")


if __name__ == "__main__":
    unittest.main()
