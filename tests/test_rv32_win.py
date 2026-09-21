"""Tests for the native window (tools/rv32win.c) under SDL's dummy video driver.

No window opens and no key is pressed: the tests replay a scripted input
through the window and require the same checkpoints and console as the
headless emulator, and they round-trip a recording. Skipped when SDL3 is not
installed; `make test-rv32-win` builds the diagnostic first so the replay
test runs rather than skips.
"""

import os
from pathlib import Path
import signal
import subprocess
import time
import sys
import tempfile
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_asm import DISPLAY, FINISH, JAL, LI, SW  # noqa: E402
from tools.rv32_devices import diag_checksum, frame_hash, parse_input_script, render_diag_frame  # noqa: E402
from tools.rv32_run_emu import build_emulator, build_window, halt_line, sdl3_missing  # noqa: E402

DIAG_IMAGE = ROOT / "build/rv32/diag.bin"
DIAG_INPUT = ROOT / "programs/rv32/diag.input"
# The dummy driver opens no window; the software renderer needs no GPU.
HEADLESS = {**os.environ, "SDL_VIDEO_DRIVER": "dummy", "SDL_RENDER_DRIVER": "software"}


@unittest.skipIf(sdl3_missing(), sdl3_missing())
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
        """Run the window unthrottled under the dummy driver; `--fps 0` goes first so a test's last
        argument is really last (an option without a value must be refused as such)."""
        completed = subprocess.run([str(self.window), "--fps", "0", *args], capture_output=True, text=True,
                                   env=HEADLESS, timeout=timeout)
        return completed.returncode, completed.stdout, completed.stderr

    def write_image(self, directory, words):
        image = Path(directory) / "image.bin"
        image.write_bytes(b"".join(word.to_bytes(4, "little") for word in words))
        return image

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

    def test_record_starts_with_frame_zero_and_replays_a_present_only_guest(self):
        """The record file is opened before frame 0's events are delivered, so a script's frame-0
        lines are recorded; a bad script or an aliased output truncates nothing."""
        words = LI(1, DISPLAY) + [SW(0, 1, 0), SW(0, 1, 0)] + FINISH()
        with tempfile.TemporaryDirectory() as directory:
            image = self.write_image(directory, words)
            script = Path(directory) / "script.txt"
            script.write_text("frame 0 down A\nframe 1 up a\nframe 2 down 20\n")
            record = Path(directory) / "record"
            status, _, stderr = self.run_window("--image", str(image), "--input", str(script), "--record", str(record))
            self.assertEqual(status, 0, stderr)
            self.assertEqual(record.read_text(), "frame 0 down A\nframe 1 up A\nframe 2 down 20\n")
            script.write_text("frame 1 sideways A\n")
            status, _, stderr = self.run_window("--image", str(image), "--input", str(script), "--record", str(record))
            self.assertEqual(status, 2)
            self.assertIn("input script", stderr)
            self.assertEqual(record.read_text(), "frame 0 down A\nframe 1 up A\nframe 2 down 20\n", "the old recording survives a refused run")

    def test_closing_the_window_stops_the_run_and_keeps_the_outputs(self):
        """A present-forever guest at 60 frames a second: SDL turns SIGTERM into a quit event, the
        run halts as `stopped` with status 2, and the recording and checkpoints written so far are
        closed and complete."""
        words = LI(1, DISPLAY) + [SW(0, 1, 0), JAL(0, -4)]
        with tempfile.TemporaryDirectory() as directory:
            image = self.write_image(directory, words)
            script = Path(directory) / "script.txt"
            script.write_text("frame 0 down A\nframe 3 up A\n")
            record, checkpoints = Path(directory) / "record", Path(directory) / "checkpoints"
            process = subprocess.Popen([str(self.window), "--image", str(image), "--fps", "60", "--input", str(script),
                                        "--record", str(record), "--checkpoints", str(checkpoints)],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, env=HEADLESS)
            time.sleep(2.0)  # long enough that SDL's start-up on a loaded machine leaves most of it for frames
            process.send_signal(signal.SIGTERM)
            stdout, stderr = process.communicate(timeout=30)
            self.assertEqual((process.returncode, stdout), (2, ""), stderr)
            halt = halt_line(stderr, "rv32win:")
            self.assertEqual(halt["halt"], "stopped")
            self.assertRegex(halt["outcome"], r"^error=host-stopped pc=[0-9a-f]{8}$")
            frames = len(checkpoints.read_text().splitlines())
            self.assertGreaterEqual(frames, 30, "two seconds at 60 frames a second, minus start-up, is well over 30")
            self.assertLessEqual(frames, 150, "paced: unthrottled, two seconds would be thousands of presents")
            self.assertEqual(record.read_text(), "frame 0 down A\nframe 3 up A\n")
            self.assertNotIn("event(s) lost", stderr, "a stopped run is not a passing one, so nothing is rejected")

    def test_a_guest_that_never_presents_still_ends(self):
        """The run loop pumps events between instruction slices, so a limit ends a guest that never
        presents, with the headless exit status and halt line."""
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "spin.bin"
            image.write_bytes(JAL(0, 0).to_bytes(4, "little"))
            for limit in (600000, 500000):  # past a slice boundary, and exactly on one
                status, stdout, stderr = self.run_window("--image", str(image), "--max-instructions", str(limit))
                self.assertEqual((status, stdout), (2, ""))
                halt = halt_line(stderr, "rv32win:")
                self.assertEqual((halt["halt"], halt["steps"], halt["outcome"]), ("limit", limit, "error=instruction-limit pc=80000000"))

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
            status, _, stderr = self.run_window("--image", str(image), "--record", f"{directory}/out", "--checkpoints", f"{directory}/./out")
            self.assertEqual(status, 2)
            self.assertIn("checkpoints file", stderr)
            self.assertFalse((Path(directory) / "out").exists(), "nothing was created")


if __name__ == "__main__":
    unittest.main()
