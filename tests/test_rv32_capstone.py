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
from tools.rv32_capstone_native import Capstone, build, run_session, INPUT, EXPECTED
from tools.rv32_devices import EVENT_PRESS, parse_input_script


class CapstoneTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.libs = [build(opt) for opt in ("O0", "O2")]

    def test_directed_rules(self):
        for lib in self.libs:
            for name in ("shapes", "collisions", "clear_score", "timing", "random_restart", "transitions", "quit_batches", "render", "runtime_render", "digit_ui", "digit_menu", "g3d_screen"):
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

    def test_g3d_menu_session(self):
        """The 3D screen through all three shaders, rendered by the C reference.

        The firmware renders the same frames on the device; the shared checkpoints
        are what hold the two to identical pixels.
        """
        expected_hex = re.search(r"^RV32_3D_MENU_HEX := ([0-9a-f]{8})$", (ROOT / "Makefile").read_text(), re.M)
        self.assertIsNotNone(expected_hex)
        script = parse_input_script((ROOT / "programs/rv32/g3d.input").read_text())
        for lib in self.libs:
            checkpoints, checksum, frames = run_session(lib, script)
            self.assertEqual(frames, 21)
            self.assertEqual(f"{checksum:08x}", expected_hex[1])
            self.assertEqual(checkpoints, (ROOT / "programs/rv32/g3d.expected").read_text().splitlines())

    def test_digit_menu_session(self):
        """Two digits drawn with the keyboard and classified by the software model.

        The firmware runs the accelerator as well and stops if the two disagree,
        so these checkpoints describe the CPU result on every backend.
        """
        expected_hex = re.search(r"^RV32_DIGIT_MENU_HEX := ([0-9a-f]{8})$", (ROOT / "Makefile").read_text(), re.M)
        self.assertIsNotNone(expected_hex)
        script = parse_input_script((ROOT / "programs/rv32/digit.input").read_text())
        for lib in self.libs:
            checkpoints, checksum, frames = run_session(lib, script)
            self.assertEqual(frames, 199)
            self.assertEqual(f"{checksum:08x}", expected_hex[1])
            self.assertEqual(checkpoints, (ROOT / "programs/rv32/digit.expected").read_text().splitlines())

    def test_digit_session_classifies_a_one_then_a_seven(self):
        """The session is only worth replaying if it actually reads the strokes."""
        script = parse_input_script((ROOT / "programs/rv32/digit.input").read_text())
        lib = self.libs[-1]
        seen, previous = [], 0
        game = Capstone(lib)
        queue, keys, pending, index = [], 0, list(script), 0
        for iteration in range(1, 400):
            while index < len(pending) and pending[index][0] <= iteration - 1:
                word = pending[index][1]
                queue.append(word)
                bit = 1 << (word & 31)
                keys = (keys | bit) if word & EVENT_PRESS else (keys & ~bit)
                index += 1
            while queue:
                game.event(queue.pop(0))
            if game.quit:
                break
            game.frame(keys)
            game.draw()
            # R clears the screen and its counter, so a classification is a frame
            # where the counter rose, not simply a frame where it is non-zero.
            runs = lib.native_digit_runs(game.state)
            if runs > previous:
                seen.append((runs, lib.native_digit_predicted(game.state),
                             lib.native_digit_status(game.state)))
            previous = runs
        # Both lists are asserted against literals: comparing statuses with
        # [0] * len(statuses) passes on an empty list and cannot fail on length.
        self.assertEqual([entry[1] for entry in seen], [1, 7],
                         "the drawn strokes must read as a one and a seven")
        self.assertEqual([entry[2] for entry in seen], [0, 0])

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
