"""F2 architectural effects: literal anchors, exact oracle vectors, and faults.

These tests compare full CPU retirement on both simulators, not just the FPU.
"""
from pathlib import Path
import random
import subprocess
import unittest

import test_rv32_rtl as integer_tests
from tools.rv32_asm import *
from tools.rv32_f_asm import arithmetic, fp, flw, fsw, fli
from tools.fp32_vectors import ANCHORS, EDGES, oracle, verify_reference_sources
from tools.rv32_rtl import ROOT, cycle_relation, diff_traces


class FloatingTest(unittest.TestCase):
    setUpClass = classmethod(integer_tests.RtlTest.setUpClass.__func__)
    tearDownClass = classmethod(integer_tests.RtlTest.tearDownClass.__func__)
    run_both = integer_tests.RtlTest.run_both
    assert_same_pass = integer_tests.RtlTest.assert_same_pass
    assert_same_double_fault = integer_tests.RtlTest.assert_same_double_fault

    def check_vectors(self, rows, answers, *, dynamic=False, seed=None):
        words, checks = [], []
        for i, ((op, rm, a, b, c), (result, flags, error)) in enumerate(zip(rows, answers, strict=True)):
            self.assertEqual(error, 0)
            # Rotate through all destinations, including f0 and x0, and alias sources.
            rd = i % 32
            words += [CSRRWI(0, 3, 0)]
            words += fli(1, a ^ 0xffffffff if op in (9, 10) else a) + fli(2, b) + fli(3, c)
            if op in (9, 10): words += LI(1, a)
            if dynamic: words += [CSRRWI(0, 2, rm)]
            words.append(arithmetic(op, 7 if dynamic else rm, rd))
            effect = '' if 11 <= op <= 15 and rd == 0 else f' {"x" if 11 <= op <= 15 else "f"}{rd}={result:08x}'
            if flags: effect += f' fcsr={((rm << 5) if dynamic else 0) | flags:02x}'
            checks.append((len(words)-1, effect, (op, rm, a, b, c)))
            words += [CSRRS(7, 1, 0)]
            checks.append((len(words)-1, f' x7={flags:08x}', (op, rm, a, b, c)))
        emu, rtl = self.assert_same_pass(words + FINISH(), seed=seed, max_cycles=3000000)
        for index, effect, request in checks:
            self.assertEqual(integer_tests.effects(emu.trace[index]), effect.lstrip(),
                             f'request={request}, trace={emu.trace[index]}')
        self.assertTrue(cycle_relation(rtl)[1], cycle_relation(rtl))

    def test_literal_numeric_anchors_static_and_dynamic(self):
        rows = [r for r in ANCHORS if r[0] <= 17 and r[1] <= 4] + [
            (13, 0, 0x7f800001, 0, 0, 0, 16, 0), # FEQ(sNaN,0): quiet comparison still raises NV
            (15, 0, 0x7fc00000, 0, 0, 0, 16, 0), # FLE(qNaN,0): signaling comparison
            (11, 0, 0x7fc00000, 0, 0, 0x7fffffff, 16, 0),
            (12, 0, 0x7fc00000, 0, 0, 0xffffffff, 16, 0),
        ]
        for dynamic in (False, True):
            self.check_vectors([r[:5] for r in rows], [r[5:] for r in rows], dynamic=dynamic, seed=7)

    def test_seeded_exact_arithmetic(self):
        verify_reference_sources()
        subprocess.run(['make', '-s', 'build/fp32/reference'], cwd=ROOT, check=True)
        rng = random.Random(20260922)
        rows = [(op, rm, *(rng.choice(EDGES) if i < 4 else rng.getrandbits(32) for _ in range(3)))
                for op in range(18) for rm in range(5) for i in range(12)]
        answers = oracle(ROOT / 'build/fp32/reference', rows)
        self.check_vectors(rows, answers, seed=23)

    def test_moves_sign_classification_and_raw_memory(self):
        patterns = [0xff800000, 0xbf800000, 0x80000001, 0x80000000, 0, 1, 0x3f800000, 0x7f800000, 0x7f800001, 0x7fc12345]
        words, checks = LI(8, RAM + 0x18000), []
        # Invalid frm must not affect operations with no rounding field.
        words += [CSRRWI(0, 2, 7)]
        for i, value in enumerate(patterns):
            words += fli(0, value) + [fsw(0, 8, -4), flw(31, 8, -4), fp(0x70, 9, 31), fp(0x70, 10, 0, funct3=1)]
            checks += [(len(words)-2, f'x9={value:08x}'), (len(words)-1, f'x10={1 << i:08x}')]
            for mode in range(3):
                words += fli(2, 0x80000000) + [fp(0x10, 0, 31, 2, mode)]
                sign = 0x80000000 if mode == 0 else 0 if mode == 1 else (value ^ 0x80000000) & 0x80000000
                checks.append((len(words)-1, f'f0={(value & 0x7fffffff) | sign:08x}'))
        words += [CSRRS(10, 1, 0)]
        for stall in (0, 3):
            emu, _ = self.assert_same_pass(words + FINISH(), stall=stall)
            for index, effect in checks: self.assertTrue(emu.trace[index].endswith(effect), emu.trace[index])
            self.assertTrue(emu.trace[len(words)-1].endswith('x10=00000000'))

    def test_csr_aliases_all_forms_and_accrual(self):
        words = LI(1, 0xffffffff) + [CSRRW(2, 3, 1), CSRRS(3, 1, 0), CSRRS(4, 2, 0),
            CSRRWI(5, 1, 0), CSRRCI(6, 2, 3), CSRRSI(7, 1, 9),
            CSRRWI(8, 2, 0), CSRRC(9, 1, 1), CSRRS(10, 3, 0), CSRRWI(0, 3, 0)]
        # DZ then NV then exact: accrued bits persist; comparison to x0 still raises NV.
        words += fli(1, 0x3f800000) + fli(2, 0) + [arithmetic(7, 0), arithmetic(7, 0)]
        repeated_flags = (len(words)-2, len(words)-1)
        words += fli(1, 0x7fc00000) + [arithmetic(14, 0, rd=0), CSRRS(11, 1, 0)]
        words += fli(1, 0x3f800000) + fli(2, 0x3f800000) + [arithmetic(0, 0), CSRRS(12, 1, 0)]
        # Zero operand register is a write, unlike rs1=x0; immediate zero set is a read.
        words += [ADDI(1, 0, 0), CSRRS(0, 1, 1), CSRRSI(13, 1, 0), CSRRCI(0, 1, 24), CSRRS(14, 3, 0)]
        emu, _ = self.assert_same_pass(words + FINISH())
        for effect in ('x3=0000001f', 'x4=00000007', 'x10=00000000', 'x11=00000018', 'x12=00000018', 'x14=00000000'):
            self.assertTrue(any(line.endswith(effect) for line in emu.trace), effect)
        for index in repeated_flags:
            self.assertEqual(integer_tests.effects(emu.trace[index]), 'f4=7f800000 fcsr=08')
        self.assertEqual(integer_tests.effects(emu.trace[len(words)-4]), 'fcsr=18')
        self.assertEqual(integer_tests.effects(emu.trace[len(words)-3]), 'x13=00000018')

    def test_illegal_encodings_and_rounding(self):
        bad = [fp(1, 1, 1, 2), fp(0x2c, 1, 1, 1), fp(0x60, 1, 1, 2), fp(0x68, 1, 1, 2),
               fp(0x10, 1, 1, 2, 3), fp(0x14, 1, 1, 2, 2), fp(0x50, 1, 1, 2, 3),
               fp(0x70, 1, 1, 1), fp(0x70, 1, 1, 0, 2), fp(0x78, 1, 1, 0, 1),
               arithmetic(3, 0) | (1 << 25), i_type(0x07, 1, 1, 2, 0), s_type(0x27, 3, 1, 2, 0)]
        for op in range(13):
            bad += [arithmetic(op, rm) for rm in (5, 6)]
        for word in bad:
            self.assert_same_double_fault([word], 2, word)
        for rm in (5, 6, 7):
            word = arithmetic(0, 7)
            self.assert_same_double_fault([CSRRWI(0, 2, rm), word], 2, word)
        # Non-rounding min/max and comparisons ignore even reserved frm.
        words = [CSRRWI(0, 2, 7)] + fli(1, 0x3f800000) + fli(2, 0x40000000)
        expected = [(13, 'x4=00000000'), (14, 'x4=00000001'), (15, 'x4=00000001'),
                    (16, 'f4=3f800000'), (17, 'f4=40000000')]
        for op, _ in expected: words.append(arithmetic(op, 0))
        emu, _ = self.assert_same_pass(words + FINISH())
        for index, (_, effect) in enumerate(expected, len(words)-len(expected)):
            self.assertEqual(integer_tests.effects(emu.trace[index]), effect)
        # A valid static mode ignores an invalid frm.
        self.assert_same_pass([CSRRWI(0, 2, 7), arithmetic(0, 0)] + FINISH())

    def test_float_memory_faults(self):
        for addr, load_cause, store_cause in ((RAM+1, 4, 6), (UNMAPPED, 5, 7), (RAM+0x400000, 5, 7), (INPUT, 0, 7)):
            if load_cause:
                self.assert_same_double_fault(LI(8, addr) + [flw(0, 8)], load_cause, addr, stall=3)
            self.assert_same_double_fault(LI(8, addr) + [fsw(0, 8)], store_cause, addr, stall=3)

    def test_all_floating_sources_and_fused_aliases(self):
        words = []
        for reg in range(32): words += fli(reg, 0x3f800000)
        checks = []
        for reg in range(32):
            # Source and destination coincide, including rs3=f31 and rd=f0.
            words += [arithmetic(3, 0, reg, reg, (reg+1) % 32, reg), fp(0x70, 9, reg)]
            checks.append((len(words)-1, 'x9=40000000' if reg < 31 else 'x9=40400000'))
        words += fli(0, 0x40000000) + [arithmetic(3, 0, 0, 0, 0, 0)]
        checks.append((len(words)-1, 'f0=40c00000'))
        for mode, expected in ((0, 0x40c00000), (1, 0xc0c00000), (2, 0x40c00000)):
            words += [fp(0x10, 0, 0, 0, mode)]
            checks.append((len(words)-1, f'f0={expected:08x}'))
        emu, _ = self.assert_same_pass(words + FINISH(), seed=31)
        for index, expected in checks: self.assertTrue(emu.trace[index].endswith(expected), emu.trace[index])

    def test_fpu_wait_latency_is_pinned_independently_of_counter_identity(self):
        # F1's 1/3 division takes 32 clocks acceptance-to-valid, plus the CPU's
        # issue and response-acceptance clocks. Stalling memory must not change it.
        words = fli(1, 0x3f800000) + fli(2, 0x40400000) + [arithmetic(7, 0)] + FINISH()
        for stall in (0, 3):
            _, rtl = self.assert_same_pass(words, stall=stall)
            self.assertEqual(rtl.halt['fp_waits'], 34)
            self.assertEqual(rtl.halt['cycles'], 4*len(words) + 1 + 34 + stall*(len(words)+1))

    def test_reset_discards_pending_fpu_result(self):
        # Prefix observes reset state, so a replay also catches stale registers/flags.
        prefix = [fp(0x70, 9, 4), CSRRS(10, 3, 0)]
        for op, a, b in ((7, 0x3f800000, 0), (7, 0x3f800000, 0x40400000),
                         (8, 0x40000000, 0), (0, 0x3f800000, 0x00000001)):
            words = prefix + fli(1, a) + fli(2, b) + [arithmetic(op, 0)] + FINISH()
            emu, baseline = self.assert_same_pass(words)
            self.assertTrue(emu.trace[0].endswith('x9=00000000'))
            self.assertTrue(emu.trace[1].endswith('x10=00000000'))
            # Eight setup instructions cost 32 cycles; execute 35, issue 36.
            # Last FPU instruction retirement is before FINISH's five instructions.
            finish_cycles = len(FINISH()) * 4 + 1
            wb_cycle = baseline.halt['cycles'] - finish_cycles
            for reset_at in sorted({35, 36, 37, (37 + wb_cycle)//2, wb_cycle-2, wb_cycle-1}):
                _, rtl = self.run_both(words, reset_at=reset_at)
                start = [i for i, line in enumerate(rtl.trace) if line.split()[1] == '80000000'][-1]
                self.assertEqual([' '.join(line.split()[1:]) for line in rtl.trace[start:]],
                                 [' '.join(line.split()[1:]) for line in emu.trace])
                # Canceled arithmetic must never appear in the pre-reset prefix.
                self.assertFalse(any(int(line.split()[2], 16) == arithmetic(op, 0) for line in rtl.trace[:start]))
                self.assertEqual(rtl.halt['outcome'], 'pass')

    def test_reset_replay_uses_new_operand_and_flags(self):
        # RAM survives reset. First sqrt(2) is canceled; the restarted program
        # loads the stored 4 and must retire sqrt(4)=2 with no accrued NX.
        address = RAM + 0x10000
        words = LI(8, address) + [flw(1, 8)] + fli(2, 0x40800000) + [fsw(2, 8),
            arithmetic(8, 0), CSRRS(9, 1, 0)] + FINISH()
        words += [0] * ((address-RAM)//4-len(words)) + [0x40000000]
        _, rtl = self.run_both(words, reset_at=40)
        arithmetic_lines = [line for line in rtl.trace if int(line.split()[2], 16) == arithmetic(8, 0)]
        self.assertEqual(len(arithmetic_lines), 1)
        self.assertEqual(integer_tests.effects(arithmetic_lines[0]), 'f4=40000000')
        read_flags = [line for line in rtl.trace if int(line.split()[2], 16) == CSRRS(9, 1, 0)]
        self.assertEqual(len(read_flags), 1)
        self.assertEqual(integer_tests.effects(read_flags[0]), 'x9=00000000')
        fresh_words = list(words)
        fresh_words[-1] = 0x40800000
        _, fresh = self.assert_same_pass(fresh_words)
        # Setup is 7 instructions with 2 memory cycles: issue at cycle 34;
        # reset after 40 cancels exactly 7 issue/wait cycles.
        self.assertEqual(rtl.halt['fp_waits'] - fresh.halt['fp_waits'], 7)

    def test_trap_handler_preserves_float_state_and_resumes(self):
        handler_at = RAM + 0x200
        bad = arithmetic(0, 5)
        words = LI(5, handler_at) + [CSRRW(0, MTVEC, 5)] + fli(4, 0x7fc12345) + [CSRRWI(0, 1, 8), bad,
            fp(0x70, 9, 4), CSRRS(10, 3, 0)] + FINISH()
        words += [0] * ((handler_at - RAM)//4 - len(words))
        words += [CSRRS(11, MEPC, 0), ADDI(11, 11, 4), CSRRW(0, MEPC, 11), MRET()]
        emu, _ = self.assert_same_pass(words, seed=13)
        self.assertTrue(any(line.endswith('x9=7fc12345') for line in emu.trace))
        self.assertTrue(any(line.endswith('x10=00000008') for line in emu.trace))
        self.assertEqual(sum(' trap ' in line for line in emu.trace), 1)
        # A second illegal F instruction before the handler retires is a double fault.
        words = LI(5, handler_at) + [CSRRW(0, MTVEC, 5), bad]
        words += [0] * ((handler_at-RAM)//4-len(words)) + [bad]
        emu, rtl = self.run_both(words)
        self.assertIsNone(diff_traces(rtl.trace, emu.trace))
        self.assertEqual(rtl.halt['halt'], 'double-fault')
        self.assertEqual(rtl.halt['cause'], 2)
        self.assertTrue(rtl.trace[-1].endswith(f' trap 2 {bad:08x}'))


if __name__ == '__main__':
    unittest.main()
