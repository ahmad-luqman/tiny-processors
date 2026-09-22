import unittest

from programs.simd4.matrix_mac import (expected_counts, program as matrix_program, reference as matrix_reference,
                                       word_count)
from programs.simd4.vector_add import program
from tools.simd4_model import (ADDI, CLRA, HLT, LANE, LAST_OPCODE, LDI, LOOP, MAC, MACU, MUL, RDA, SETLOOP,
                               execute, image, signed, snapshot, state_bits, word)


def register(state, lane, reg):
    return state >> (lane * 64 + reg * 16) & 0xffff


def accumulator(state, lanes, lane):
    return state >> (lanes * 64 + lane * 32) & 0xffffffff


class ModelTests(unittest.TestCase):
    def test_kernel_words(self):
        self.assertEqual(program(4, 16)[:9], [0x02000000, 0x07000004, 0x05400000,
                         0x05800040, 0x03d80000, 0x06c00080, 0x04000004, 0x08000002, 0])

    def test_multiply_words(self):
        self.assertEqual((MUL, MAC, MACU, CLRA, RDA, LAST_OPCODE), (9, 10, 11, 12, 13, 13))
        self.assertEqual([word(MUL, rd=3, ra=1, rb=2), word(MAC, ra=2, rb=3), word(MACU, ra=1, rb=1),
                          word(CLRA), word(RDA, rd=2, imm=16)],
                         [0x09d80000, 0x0a2c0000, 0x0b140000, 0x0c000000, 0x0d800010])

    def test_snapshot_layout(self):
        registers = [[1, 2, 3, 4], [5, 6, 7, 8]]
        state = snapshot(registers, [0x89abcdef, 0x01234567], 0xfe, 0xbeef)
        self.assertEqual(state & (1 << 64) - 1, 0x0004000300020001)
        self.assertEqual(state >> 64 & (1 << 64) - 1, 0x0008000700060005)
        self.assertEqual(state >> 128 & 0xffffffff, 0x89abcdef)
        self.assertEqual(state >> 160 & 0xffffffff, 0x01234567)
        self.assertEqual(state >> 192 & 0xff, 0xfe)
        self.assertEqual(state >> 200, 0xbeef)
        self.assertEqual(register(state, 1, 2), 7)
        self.assertEqual(accumulator(state, 2, 1), 0x01234567)
        run = execute(image([word(HLT)]), [0] * 256, lanes=2)
        self.assertEqual((run.lanes, run.state_bits, run.record_bits), (2, 216, 256))
        self.assertEqual([state_bits(lanes) for lanes in (1, 2, 4)], [120, 216, 408])

    def test_vector_results_independent_of_lane_count(self):
        memory = [0xa55a] * 256
        a = [65535, 32767, 32768, 0, 1, 2, 3, 4]
        b = [1, 1, 32768, 65535, 2, 3, 4, 5]
        memory[:8], memory[64:72] = a, b
        for lanes in (1, 2, 4):
            with self.subTest(lanes=lanes):
                result = execute(program(lanes, 8), memory, lanes)
                self.assertEqual(result.memory[128:136], [0, 32768, 0, 65535, 3, 5, 7, 9])
                self.assertEqual(result.memory[:128], memory[:128])
                self.assertEqual(result.memory[136:], memory[136:])
                self.assertEqual(len(result.transfers), 24)
                self.assertEqual(len(result.retirements), 3 + 6 * (8 // lanes))
                self.assertFalse(result.fault)

    def test_alias_arithmetic_and_store_order(self):
        code = image([word(2), word(4, rd=0, ra=0, imm=65535), word(6, rd=0, ra=1), 0])
        result = execute(code, [0] * 256)
        self.assertEqual([t & 0xffff for t in result.transfers], [65535, 0, 1, 2])
        self.assertEqual(result.memory[0], 2)

    def test_pc_and_address_wrap(self):
        code = image([0])
        code[254] = word(1, imm=255)
        code[255] = word(5, rd=1, ra=0, imm=2)
        memory = [0] * 256
        memory[1] = 42
        result = execute(code, memory, entry=254)
        self.assertEqual([t >> 16 & 255 for t in result.transfers], [1] * 4)
        self.assertEqual([r >> (4 * 96 + 56) for r in result.retirements], [254, 255, 0])
        self.assertEqual(register(result.final_state, 0, 1), 42)

    def test_faults_do_not_retire(self):
        for bad in (word(LAST_OPCODE + 1), word(255), word(SETLOOP), word(LOOP)):
            with self.subTest(word=bad):
                result = execute(image([word(LDI, imm=42), bad]), [0] * 256)
                self.assertTrue(result.fault)
                self.assertEqual(len(result.retirements), 1)
                self.assertEqual(result.transfers, [])
                self.assertEqual(register(result.final_state, 0, 0), 42)
                self.assertEqual(result.base_cycles, 4)
        # A fault leaves a live accumulator alone, even with MAC-shaped fields in the bad word.
        result = execute(image([word(LDI, imm=42), word(MAC, ra=0, rb=0), word(LAST_OPCODE + 1, ra=0, rb=0)]),
                         [0] * 256, lanes=2)
        self.assertTrue(result.fault)
        self.assertEqual([accumulator(result.final_state, 2, lane) for lane in range(2)], [1764, 1764])

    def test_matrix_kernel_words(self):
        # Four lanes, one column group: setup, then the unrolled row body.
        self.assertEqual(matrix_program(4, 4)[:9], [0x02000000, 0x04000000, 0x01400000, 0x07000004,
                         0x0c000000, 0x05900000, 0x05c00040, 0x0a2c0000, 0x05900001])
        self.assertEqual(matrix_program(4, 4)[17:23], [0x0d800000, 0x03d00000, 0x06b00080, 0x04500004,
                         0x08000004, 0])
        self.assertEqual(matrix_program(2, 4)[22:26], [0x02000000, 0x04000002, 0x01400000, 0x07000004])

    def test_matrix_kernel_matches_direct_product_and_predicted_counts(self):
        a = [0x7fff, 0x8000, 0xffff, 0x0001] * 4
        # Each row of B is the previous row rotated left, so every column holds all four corners.
        b = [[0x0001, 0x7fff, 0x8000, 0xffff][(c + r) % 4] for r in range(4) for c in range(4)]
        memory = [0x5a5a] * 256
        memory[:16], memory[64:80] = a, b
        for lanes in (1, 2, 4):
            for shift in (0, 16):
                with self.subTest(lanes=lanes, shift=shift):
                    result = execute(matrix_program(lanes, 4, shift), memory, lanes)
                    self.assertFalse(result.fault)
                    self.assertEqual(result.memory[128:144], matrix_reference(a, b, 4, shift))
                    self.assertEqual(result.memory[:128] + result.memory[144:], memory[:128] + memory[144:])
                    self.assertEqual((len(result.retirements), len(result.transfers)), expected_counts(lanes, 4))
        # Row 0 of A times column 0 of B (1, 32767, -32768, -1) by hand:
        # 32767*1 + (-32768)*32767 + (-1)*(-32768) + 1*(-1) = 32767 - 1073709056 + 32768 - 1
        # = -1073643522 = 0xc0017ffe; RDA 0 keeps 7ffe, RDA 16 keeps c001.
        self.assertEqual(matrix_reference(a, b, 4, 0)[0], 0x7ffe)
        self.assertEqual(matrix_reference(a, b, 4, 16)[0], 0xc001)
        self.assertEqual(expected_counts(4, 4), (77, 144))
        self.assertEqual(expected_counts(1, 4), (305, 144))
        self.assertEqual(expected_counts(2, 8), (977, 1088))

    def test_reject_bad_kernel_dimensions_and_words(self):
        for lanes, n in ((3, 6), (4, 2), (2, 3), (1, 8), (4, 16)):
            with self.subTest(lanes=lanes, n=n), self.assertRaises(ValueError):
                matrix_program(lanes, n)
        with self.assertRaisesRegex(ValueError, "8x8 on 1 lane"):
            matrix_program(1, 8)
        self.assertEqual([word_count(lanes, n) for lanes, n in ((1, 4), (2, 8), (4, 8), (1, 8))], [89, 137, 69, 273])
        with self.assertRaises(ValueError):
            matrix_program(4, 4, shift=32)
        for lanes, length in ((0, 4), (3, 6), (4, 3), (4, 0), (4, 68)):
            with self.subTest(lanes=lanes, length=length), self.assertRaises(ValueError):
                program(lanes, length)
        for args in ((256, 0, 0, 0, 0), (1, 4, 0, 0, 0), (1, 0, 0, 0, 65536)):
            with self.assertRaises(ValueError):
                word(*args)

    def accumulate(self, op, a, b, times=1):
        """One lane: load a and b, apply op `times` times, return (acc, r2 after RDA 0, r3 after RDA 16)."""
        code = image([word(LDI, rd=0, imm=a), word(LDI, rd=1, imm=b)] + [word(op, ra=0, rb=1)] * times +
                     [word(RDA, rd=2), word(RDA, rd=3, imm=16), word(HLT)])
        result = execute(code, [0] * 256, lanes=1)
        self.assertFalse(result.fault)
        return (accumulator(result.final_state, 1, 0), register(result.final_state, 0, 2),
                register(result.final_state, 0, 3))

    def test_extreme_products_signed_and_unsigned(self):
        # (a, b, repetitions): (signed accumulator, unsigned accumulator), hand-computed bit patterns.
        cases = {
            (0xffff, 0xffff, 1): (0x00000001, 0xfffe0001),  # (-1)(-1) vs 65535^2
            (0x8000, 0x8000, 1): (0x40000000, 0x40000000),  # (-32768)^2 = 32768^2 = 2^30
            (0x8000, 0x7fff, 1): (0xc0008000, 0x3fff8000),  # -32768 * 32767
            (0xffff, 0x0001, 1): (0xffffffff, 0x0000ffff),  # sign extension of -1
            (0x7fff, 0x7fff, 3): (0xbffd0003, 0xbffd0003),  # 3 * 1073676289 passes 2^31
            (0xffff, 0xffff, 3): (0x00000003, 0xfffa0003),  # 3 * 4294836225 wraps past 2^32
            (0x0003, 0x0004, 1): (0x0000000c, 0x0000000c),  # ordinary
            (0x0000, 0xffff, 5): (0x00000000, 0x00000000),
        }
        for (a, b, times), (signed_acc, unsigned_acc) in cases.items():
            with self.subTest(a=a, b=b, times=times):
                expected = (signed(a) * signed(b) * times) % 2**32
                self.assertEqual(expected, signed_acc)
                self.assertEqual((a * b * times) % 2**32, unsigned_acc)
                self.assertEqual(self.accumulate(MAC, a, b, times),
                                 (signed_acc, signed_acc & 0xffff, signed_acc >> 16))
                self.assertEqual(self.accumulate(MACU, a, b, times),
                                 (unsigned_acc, unsigned_acc & 0xffff, unsigned_acc >> 16))

    def test_mul_keeps_low_sixteen_bits(self):
        for a, b in ((0x7fff, 0x7fff), (0xffff, 0xffff), (0x8000, 0x0002), (0x0123, 0x0456)):
            with self.subTest(a=a, b=b):
                code = image([word(LDI, rd=0, imm=a), word(LDI, rd=1, imm=b), word(MUL, rd=2, ra=0, rb=1), word(HLT)])
                result = execute(code, [0] * 256, lanes=2)
                low = (a * b) % 65536
                for lane in range(2):
                    self.assertEqual(register(result.final_state, lane, 2), low)
                    self.assertEqual(accumulator(result.final_state, 2, lane), 0)  # untouched by MUL

    def test_rda_shift_truncation_and_clear(self):
        # acc = 3 * 32767^2 = 0xbffd0003, past 2^31, so the pattern reads as negative: shifts sign-extend.
        base = [word(LDI, rd=0, imm=0x7fff), word(LDI, rd=1, imm=0x7fff)] + [word(MAC, ra=0, rb=1)] * 3
        for shift, expected in ((0, 0x0003), (1, 0x8001), (4, 0xd000), (16, 0xbffd), (31, 0xffff), (33, 0x8001)):
            with self.subTest(shift=shift):
                result = execute(image(base + [word(RDA, rd=2, imm=shift), word(HLT)]), [0] * 256, lanes=1)
                self.assertEqual(register(result.final_state, 0, 2), expected)
        result = execute(image(base + [word(CLRA), word(RDA, rd=2), word(RDA, rd=3, imm=31), word(HLT)]),
                         [0] * 256, lanes=1)
        self.assertEqual(accumulator(result.final_state, 1, 0), 0)
        self.assertEqual((register(result.final_state, 0, 2), register(result.final_state, 0, 3)), (0, 0))

    def test_mac_aliases_and_per_lane_accumulators(self):
        # MAC r0,r0 squares; RDA into r0 overwrites the operand only after the product is captured.
        code = image([word(LANE, rd=0), word(ADDI, rd=0, ra=0, imm=0xfffe), word(MAC, ra=0, rb=0),
                      word(RDA, rd=0), word(MAC, ra=0, rb=0), word(RDA, rd=1), word(HLT)])
        result = execute(code, [0] * 256, lanes=4)
        for lane, (square, acc) in enumerate(((4, 20), (1, 2), (0, 0), (1, 2))):  # r0 = -2, -1, 0, 1
            self.assertEqual(register(result.final_state, lane, 0), square)
            self.assertEqual(register(result.final_state, lane, 1), acc)
            self.assertEqual(accumulator(result.final_state, 4, lane), acc)
        self.assertEqual(len(result.retirements), 7)
        self.assertEqual(result.transfers, [])


if __name__ == "__main__":
    unittest.main()
