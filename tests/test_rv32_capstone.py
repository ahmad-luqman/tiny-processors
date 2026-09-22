"""M7 directed C rules oracles plus pinned native session at both optimization levels."""
from pathlib import Path
import ctypes
import re
import subprocess
import tempfile
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
            for name in ("shapes", "collisions", "clear_score", "timing", "random_restart", "transitions", "quit_batches", "render", "runtime_render"):
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

    def test_g1_menu_session(self):
        expected_hex=re.search(r"^RV32_GFX_MENU_HEX := ([0-9a-f]{8})$",(ROOT/'Makefile').read_text(),re.M)
        self.assertIsNotNone(expected_hex)
        for lib in self.libs:
            checkpoints,checksum,frames=run_session(lib,parse_input_script((ROOT/'programs/rv32/gfx.input').read_text()))
            self.assertEqual(frames,8)
            self.assertEqual(f"{checksum:08x}",expected_hex[1])
            self.assertEqual(checkpoints,(ROOT/'programs/rv32/gfx.expected').read_text().splitlines())

    def test_bad_sessions_fail(self):
        lib = self.libs[-1]
        for script, message in (("", "no quit within"),
                                ("frame 0 down Q\nframe 1 up Q\n", "come after the quit"),
                                ("frame 0 down UP\n" * 17, "would overflow")):
            with self.subTest(script=script), self.assertRaisesRegex(ValueError, message):
                run_session(lib, parse_input_script(script), max_frames=3)

    def test_cli_bad_input_and_surface_type(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "bad.input"
            for content in (None, "frame 0 sideways Q\n"):
                if content is not None:
                    path.write_text(content)
                completed = subprocess.run(
                    [sys.executable, str(ROOT / "tools/rv32_capstone_native.py"), "--input", str(path)],
                    capture_output=True, text=True, timeout=30)
                self.assertNotEqual(completed.returncode, 0)
                self.assertIn(str(path) + ":", completed.stderr)
                self.assertNotIn("Traceback", completed.stderr)
        lib = self.libs[-1]
        state = ctypes.create_string_buffer(lib.native_size())
        with self.assertRaises(ctypes.ArgumentError):
            lib.runtime_draw(state, ctypes.byref(ctypes.c_uint32()))


if __name__ == "__main__":
    unittest.main()
