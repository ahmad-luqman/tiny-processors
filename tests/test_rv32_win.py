"""Tests for the native window (tools/rv32win.c) under SDL's dummy video driver.

No window opens and no key is pressed: the tests replay a scripted input
through the window and require the same checkpoints and console as the
headless emulator, and they round-trip a recording. Skipped when SDL3 is not
installed; `make test-rv32-win` builds the diagnostic first so the replay
test runs rather than skips.
"""

import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_asm import JAL  # noqa: E402
from tools.rv32_devices import diag_checksum, frame_hash, parse_input_script, render_diag_frame  # noqa: E402
from tools.rv32_run_emu import build_emulator, build_window, halt_line, sdl3_flags  # noqa: E402

DIAG_IMAGE = ROOT / "build/rv32/diag.bin"
DIAG_INPUT = ROOT / "programs/rv32/diag.input"
# The dummy driver opens no window; the software renderer needs no GPU.
HEADLESS = {**os.environ, "SDL_VIDEO_DRIVER": "dummy", "SDL_RENDER_DRIVER": "software"}


@unittest.skipUnless(sdl3_flags() is not None, "SDL3 is not installed (brew install sdl3)")
class WindowTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.TemporaryDirectory()
        cls.window = Path(cls.workdir.name) / "rv32win"
        cls.emulator = Path(cls.workdir.name) / "rv32emu"
        build_window(cls.window)
        build_emulator(cls.emulator)

    @classmethod
    def tearDownClass(cls):
        cls.workdir.cleanup()

    def run_window(self, *args, timeout=60):
        completed = subprocess.run([str(self.window), *args, "--fps", "0"], capture_output=True, text=True,
                                   env=HEADLESS, timeout=timeout)
        return completed.returncode, completed.stdout, completed.stderr

    def test_scripted_replay_matches_the_headless_emulator(self):
        """The diagnostic through the window: the same checkpoints, console, and pass as rv32emu,
        and the recording of the run is the script it replayed."""
        if not DIAG_IMAGE.exists():
            self.skipTest("build/rv32/diag.bin is not built (make check-rv32-image)")
        with tempfile.TemporaryDirectory() as directory:
            checkpoints = Path(directory) / "checkpoints"
            record = Path(directory) / "record"
            status, stdout, stderr = self.run_window("--image", str(DIAG_IMAGE), "--input", str(DIAG_INPUT),
                                                     "--checkpoints", str(checkpoints), "--record", str(record))
            self.assertEqual(status, 0, stderr)
            expected = [f"frame {n} {frame_hash(render_diag_frame(n)):08x}" for n in (1, 2)]
            self.assertEqual(checkpoints.read_text().splitlines(), expected)
            self.assertEqual(stdout.splitlines()[-1], f"PASS {diag_checksum():08x}")
            halt = halt_line(stderr, "rv32win:")
            self.assertEqual((halt["halt"], halt["outcome"], halt["traps"]), ("done", "pass", 4))
            self.assertEqual(parse_input_script(record.read_text()), parse_input_script(DIAG_INPUT.read_text()))
            # The recording replays on the headless emulator to the same checkpoints.
            replayed = Path(directory) / "replayed"
            completed = subprocess.run([str(self.emulator), "--image", str(DIAG_IMAGE), "--input", str(record),
                                        "--checkpoints", str(replayed)], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertEqual(replayed.read_text(), checkpoints.read_text())

    def test_a_guest_that_never_presents_still_ends(self):
        """The run loop pumps events between instruction slices, so a limit ends a guest that never
        presents, with the headless exit status and halt line."""
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "spin.bin"
            image.write_bytes(JAL(0, 0).to_bytes(4, "little"))
            status, stdout, stderr = self.run_window("--image", str(image), "--max-instructions", "600000")
            self.assertEqual((status, stdout), (2, ""))
            halt = halt_line(stderr, "rv32win:")
            self.assertEqual((halt["halt"], halt["steps"], halt["outcome"]), ("limit", 600000, "error=instruction-limit pc=80000000"))

    def test_bad_options_are_refused(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "spin.bin"
            image.write_bytes(JAL(0, 0).to_bytes(4, "little"))
            for args in (["--scale", "0"], ["--scale", "9"], ["--scale", "x"], ["--fps", "-1"], ["--bogus", "1"], ["--image"]):
                with self.subTest(args=args):
                    status, stdout, stderr = self.run_window("--image", str(image), *args)
                    self.assertEqual((status, stdout), (2, ""))
                    self.assertIsNone(halt_line(stderr, "rv32win:"), "the run never starts")
            status, _, stderr = self.run_window("--image", str(image), "--record", str(image))
            self.assertEqual(status, 2)
            self.assertIn("record file", stderr)
            self.assertIn("would overwrite", stderr)


if __name__ == "__main__":
    unittest.main()
