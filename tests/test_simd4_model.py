import unittest

from programs.simd4.vector_add import program
from tools.simd4_model import execute, image, word


class ModelTests(unittest.TestCase):
    def test_kernel_words(self):
        self.assertEqual(program(4, 16)[:9], [0x02000000, 0x07000004, 0x05400000,
                         0x05800040, 0x03d80000, 0x06c00080, 0x04000004, 0x08000002, 0])

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
        self.assertEqual([r >> (4 * 64 + 56) for r in result.retirements], [254, 255, 0])
        self.assertEqual(result.final_state >> 16 & 0xffff, 42)

    def test_faults_do_not_retire(self):
        for bad in (word(9), word(255), word(7), word(8)):
            with self.subTest(word=bad):
                result = execute(image([word(1, imm=42), bad]), [0] * 256)
                self.assertTrue(result.fault)
                self.assertEqual(len(result.retirements), 1)
                self.assertEqual(result.transfers, [])
                self.assertEqual(result.final_state & 0xffff, 42)
                self.assertEqual(result.base_cycles, 4)

    def test_reject_bad_kernel_dimensions_and_words(self):
        for lanes, length in ((0, 4), (3, 6), (4, 3), (4, 0), (4, 68)):
            with self.subTest(lanes=lanes, length=length), self.assertRaises(ValueError):
                program(lanes, length)
        for args in ((256, 0, 0, 0, 0), (1, 4, 0, 0, 0), (1, 0, 0, 0, 65536)):
            with self.assertRaises(ValueError):
                word(*args)


if __name__ == "__main__":
    unittest.main()
