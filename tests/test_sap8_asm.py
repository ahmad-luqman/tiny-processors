from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.sap8_asm import AssemblyError, assemble


ROOT = Path(__file__).resolve().parents[1]


class AssemblerTests(unittest.TestCase):
    def test_all_instruction_encodings(self):
        program, data = assemble("LDI 255\nLDA 0\nSTA 1\nADD 2\nSUB 3\nJMP 4\nJZ 5\nOUT\nHLT")
        self.assertEqual(program[:9], [0x00FF, 0x0100, 0x0201, 0x0302, 0x0403,
                                      0x0504, 0x0605, 0x0700, 0x0800])
        self.assertEqual(program[9:], [0x0800] * 247)
        self.assertEqual(data, [0] * 256)

    def test_forward_backward_labels_and_comments(self):
        program, _ = assemble("; comment\nstart: ldi 0b11\nJZ end\nJMP start\nend:\nOUT ; comment\nHLT")
        self.assertEqual(program[:5], [0x0003, 0x0603, 0x0500, 0x0700, 0x0800])

    def test_data_does_not_advance_program_address(self):
        program, data = assemble(".data 0 0xff\nJMP end\n.data 255 0b10\nend: HLT")
        self.assertEqual(program[:2], [0x0501, 0x0800])
        self.assertEqual((data[0], data[255]), (255, 2))
        self.assertEqual(sum(data), 257)

    def test_full_address_space(self):
        program, _ = assemble("JMP last\n" + "OUT\n" * 254 + "last: HLT")
        self.assertEqual(len(program), 256)
        self.assertEqual(program[0], 0x05FF)

    def test_reject_overlong_program_and_out_of_range_label(self):
        for source in ("HLT\n" * 257, "HLT\n" * 256 + "end:"):
            with self.subTest(source=source[-20:]), self.assertRaises(AssemblyError):
                assemble(source)

    def test_reject_bad_operands(self):
        for source in ("LDI -1", "LDI 256", "LDI missing", "LDI 0xgg", "LDI 1.5",
                       "JMP", "LDI 1 2", "OUT 3", "HLT 1", "WAT 5"):
            with self.subTest(source=source), self.assertRaisesRegex(AssemblyError, "line 1:"):
                assemble(source)

    def test_reject_bad_labels_and_data(self):
        for source in ("again: OUT\nagain: HLT", "9bad: HLT", "a:b: HLT",
                       "x: .data 0 1", ".data 0", ".data 256 1", ".data 0 -1",
                       ".data 0 1\n.data 0 2"):
            with self.subTest(source=source), self.assertRaises(AssemblyError):
                assemble(source)

    def test_add_matches_hand_encoded_smoke_program(self):
        program, _ = assemble((ROOT / "programs/sap8/add.asm").read_text())
        self.assertEqual(program[:6], [0x0007, 0x02F0, 0x0005, 0x03F0, 0x0700, 0x0800])

    def test_loop_encodings_and_initial_data(self):
        program, data = assemble((ROOT / "programs/sap8/sum_loop.asm").read_text())
        self.assertEqual(program[:11], [0x01F1, 0x0608, 0x03F2, 0x02F2, 0x01F1,
                                       0x04F0, 0x02F1, 0x0500, 0x01F2, 0x0700, 0x0800])
        self.assertEqual(data[240:243], [1, 3, 0])

    def run_cli(self, source, program, data):
        return subprocess.run([sys.executable, str(ROOT / "tools/sap8_asm.py"), str(source),
                               "--program", str(program), "--data", str(data)],
                              capture_output=True, text=True)

    def test_cli_writes_readmemh_images(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / "test.asm"
            source.write_text("LDI 42\nOUT\nHLT\n.data 255 128")
            program, data = path / "build/program.hex", path / "build/data.hex"
            result = self.run_cli(source, program, data)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(program.read_text().splitlines(), ["002a", "0700"] + ["0800"] * 254)
            self.assertEqual(data.read_text().splitlines(), ["00"] * 255 + ["80"])

    def test_cli_rejects_invalid_source_without_writing_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source, program, data = path / "bad.asm", path / "program.hex", path / "data.hex"
            source.write_text("JMP missing")
            result = self.run_cli(source, program, data)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("line 1", result.stderr)
            self.assertFalse(program.exists())
            self.assertFalse(data.exists())

    def test_cli_protects_source_and_distinct_outputs(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            source = path / "test.asm"
            source.write_text("HLT")
            for program, data in ((source, path / "data.hex"), (path / "same.hex", path / "same.hex")):
                result = self.run_cli(source, program, data)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("distinct paths", result.stderr)
                self.assertEqual(source.read_text(), "HLT")


if __name__ == "__main__":
    unittest.main()
