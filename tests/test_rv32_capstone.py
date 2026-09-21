"""M7 directed C rules oracles plus pinned native session at both optimization levels."""
from pathlib import Path
import re
import sys
import unittest
ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_capstone_native import build, run_session, INPUT, EXPECTED
from tools.rv32_devices import parse_input_script


class CapstoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.libs = [build(opt) for opt in ("O0", "O2")]

    def test_directed_rules(self):
        for lib in self.libs:
            for name in ("shapes", "collisions", "clear_score", "timing", "random_restart", "transitions", "render"):
                with self.subTest(library=lib._name, check=name):
                    line = getattr(lib, "check_" + name)()
                    self.assertEqual(line, 0, f"C assertion failed: tests/rv32_capstone_native.c:{line}")

    def test_pinned_session(self):
        expected_hex = re.search(r"^RV32_CAPSTONE_HEX := ([0-9a-f]{8})$", (ROOT / "Makefile").read_text(), re.M)
        self.assertIsNotNone(expected_hex)
        for lib in self.libs:
            checkpoints, checksum, frames = run_session(lib, parse_input_script(INPUT.read_text()))
            self.assertEqual(frames, 68)
            self.assertEqual(checkpoints, EXPECTED.read_text().splitlines())
            self.assertEqual(f"{checksum:08x}", expected_hex[1])

    def test_bad_sessions_fail(self):
        lib = self.libs[-1]
        for script in ("", "frame 0 down Q\nframe 1 up Q\n", "frame 0 down UP\n" * 17):
            with self.subTest(script=script), self.assertRaises(ValueError):
                run_session(lib, parse_input_script(script), max_frames=3)


if __name__ == "__main__":
    unittest.main()
