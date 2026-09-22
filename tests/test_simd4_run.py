"""The runner's own guards: fixture widths, simulator complaints, and the images it builds."""

from pathlib import Path
import tempfile
import unittest

from tools.simd4_model import LOOP, MAC, MACU, RDA, SETLOOP, execute, word
from tools.simd4_run import (EXTREMES, OVERFLOW, RDA_SWEEP, UNUSED_FIELD_BITS, Runner, failure_reasons,
                             fault_after_mac, matrix_memory, write_hex)


def register(state, lane, reg):
    return state >> (lane * 64 + reg * 16) & 0xffff


class RunnerGuardTests(unittest.TestCase):
    def test_write_hex_rejects_values_wider_than_the_array(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "rows.hex"
            write_hex(path, [0, (1 << 25) - 1], 25)
            self.assertEqual(path.read_text(), "0000000\n1ffffff\n")
            for value in (1 << 25, -1):
                with self.subTest(value=value), self.assertRaises(ValueError):
                    write_hex(path, [value], 25)

    def test_failure_reasons_name_each_rejected_condition(self):
        passing = "  105 ns transfer lane=0 write=0 address=00 data=0008\nRESULT lanes=4\nPASS: SIMD4 checks\n"
        self.assertEqual(failure_reasons(0, passing), [])
        self.assertEqual(failure_reasons(1, passing), ["exit status 1"])
        self.assertEqual(failure_reasons(0, "RESULT lanes=4\n"), ["no PASS line"])
        for complaint in ("WARNING: tests/simd4_tb.sv:211: Excess hex digits (1 of 'f0...') while reading 448-bit words.",
                          "ERROR: tests/simd4_tb.sv:9: something",
                          "%Warning: tests/simd4_tb.sv:211: $readmem file ended before specified final address",
                          "[7000] %Warning: tests/simd4_tb.sv:1: $warning",
                          "%Error: tests/simd4_tb.sv:1: Verilog $stop",
                          "VCD warning: something"):
            with self.subTest(complaint=complaint):
                self.assertEqual(failure_reasons(0, passing + complaint + "\n"), ["simulator warning or error"])

    def test_expect_nonzero_acc_needs_an_abort_point(self):
        runner = Runner.__new__(Runner)
        with self.assertRaises(ValueError):
            runner.run("no-abort", OVERFLOW, [0] * 256, 1, expect_nonzero_acc=True)

    def test_fault_after_mac_rejects_words_that_retire(self):
        for bad in (word(SETLOOP), word(LOOP), word(0xff)):
            with self.subTest(bad=bad):
                result = execute(fault_after_mac(bad), [0] * 256, lanes=2)
                self.assertTrue(result.fault)
                self.assertEqual([result.final_state >> (128 + lane * 32) & 0xffffffff for lane in range(2)],
                                 [0xffff0367, 0xffff0367])
        with self.assertRaises(ValueError):
            fault_after_mac(word(MAC))

    def test_extreme_image_reads_back_nonzero_through_the_ignored_field_words(self):
        # The RDA with ra/rb set reads fffc from a live accumulator; the bits [17:16] words execute.
        result = execute(EXTREMES, [0] * 256, lanes=4)
        self.assertFalse(result.fault)
        retire = {r >> (4 * 96 + 56) & 0xff: r for r in result.retirements}
        code = EXTREMES
        marked = code.index(word(RDA, rd=2, ra=3, rb=1, imm=16))
        self.assertEqual(register(retire[marked], 0, 2), 0xfffc)
        self.assertEqual(register(retire[marked], 0, 3), 0x8001)  # r3 untouched
        for w in (word(MAC, ra=0, rb=1) | UNUSED_FIELD_BITS, word(RDA, rd=3, imm=8) | UNUSED_FIELD_BITS):
            self.assertIn(w, code)
        # Lane-distinct MUL/MACU tail: lane 3 ends with 0x303 * 0x101 + 0x303^2, lane 0 with zero.
        macu_alias = code.index(word(MACU, rd=1, ra=3, rb=3))
        self.assertEqual([result.retirements[macu_alias] >> (256 + lane * 32) & 0xffffffff for lane in (0, 3)],
                         [0, 0x000c180c])

    def test_sweep_and_overflow_images(self):
        sweep = execute(RDA_SWEEP, [0] * 256, lanes=1)
        self.assertFalse(sweep.fault)
        self.assertEqual(len(sweep.retirements), 2 + 3 + 36 + 4 + 36 + 2 + 36 + 1)
        reads = {register(r, 0, 2 + (i & 1)) for i, r in enumerate(sweep.retirements[5:41])}
        self.assertEqual(len(reads), 32)  # a negative accumulator gives 32 distinct windows
        overflow = execute(OVERFLOW, [0] * 256, lanes=1)
        self.assertEqual([r >> 64 & 0xffffffff for r in overflow.retirements[2:5]], [0x3fff0001, 0x7ffe0002, 0xbffd0003])

    def test_corner_matrices_overflow_and_ordinary_operands_are_live(self):
        from programs.simd4.matrix_mac import reference
        for n, c00 in ((2, 0x7ffe0002), (4, 0xfffc0004), (8, 0xfff80008)):
            with self.subTest(n=n):
                memory = matrix_memory(n, extreme=True)
                self.assertEqual(reference(memory[:n * n], memory[64:64 + n * n], n, 16)[0], c00 >> 16)
                self.assertEqual(reference(memory[:n * n], memory[64:64 + n * n], n, 0)[0], c00 & 0xffff)
                if n > 2:
                    columns = [{memory[64 + k * n + c] for k in range(n)} for c in range(1, n)]
                    self.assertTrue(all(len(column) == 4 for column in columns))
        ordinary = matrix_memory(4)
        self.assertNotIn(0, ordinary[:16] + ordinary[64:80])


if __name__ == "__main__":
    unittest.main()
