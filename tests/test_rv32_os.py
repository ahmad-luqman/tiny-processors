"""Track 2, O2 onward: the console's receive side, the RAM disk, and the kernel (docs/rv32-os.md).

The RAM disk tests use the program ELFs `make check-rv32-os-image` builds. The kernel's console
session runs on the emulator and the RTL in step-tick mode, where the whole trace, interrupt
lines included, must agree; the Make targets run it on QEMU and with cycle ticks.
"""
import os
import shutil
import struct
import tempfile
import unittest
from pathlib import Path

import test_rv32_rtl as integer_tests
from tools import rv32_ramdisk
from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_image import flatten, parse_elf
from tools.rv32_rtl import ROOT, diff_traces, run_emulator, run_rtl, write_image

OS = ROOT / "build/rv32/os"
PROGRAMS = ("sh", "hello", "primes", "pong", "tetris", "menu", "syscheck", "fault")


def elfs(*names):
    paths = [OS / f"{name}.elf" for name in names]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise unittest.SkipTest(f"{missing[0]} is missing: run make check-rv32-os-image")
    return paths


class RamdiskTest(unittest.TestCase):
    def test_round_trip(self):
        blob = rv32_ramdisk.build(elfs(*PROGRAMS), frozenset({"menu"}))
        entries = rv32_ramdisk.parse(blob)
        self.assertEqual([e["name"] for e in entries], list(PROGRAMS))
        for index, entry in enumerate(entries):
            with self.subTest(entry["name"]):
                self.assertEqual(entry["load"] % rv32_ramdisk.SLOT_SIZE, rv32_ramdisk.SLOT_BASE % rv32_ramdisk.SLOT_SIZE)
                self.assertEqual(entry["entry"], entry["load"])
                self.assertEqual(entry["flags"], rv32_ramdisk.ACCELERATORS if entry["name"] == "menu" else 0)
                self.assertLessEqual(entry["size"], entry["memory"])
                self.assertLessEqual(entry["memory"], rv32_ramdisk.SLOT_SIZE - rv32_ramdisk.STACK_SIZE)
                elf = parse_elf((OS / f"{entry['name']}.elf").read_bytes())
                self.assertEqual(entry["data"], flatten(elf, entry["load"]))
        self.assertEqual(len({e["load"] for e in entries}), len(entries), "one slot each")
        self.assertEqual(blob, (OS / "ramdisk.img").read_bytes(), "the committed build rule makes the same bytes")

    def test_refusals(self):
        hello, primes = elfs("hello", "primes")
        with tempfile.TemporaryDirectory() as directory:
            twin = Path(directory) / "twin.elf"
            shutil.copy(hello, twin)
            long_name = Path(directory) / ("x" * 24 + ".elf")
            shutil.copy(hello, long_name)
            cases = {"same slot": ([hello, twin], frozenset()), "same name": ([hello, hello], frozenset()),
                     "long name": ([long_name], frozenset()), "unknown accelerator program": ([hello], frozenset({"menu"})),
                     "kernel image": ([ROOT / "build/rv32/os/kernel.elf"], frozenset())}
            for name, (paths, accelerators) in cases.items():
                with self.subTest(name), self.assertRaises(rv32_ramdisk.RamdiskError):
                    rv32_ramdisk.build(paths, accelerators)
        blob = rv32_ramdisk.build([hello, primes])
        for name, bad in (("magic", b"XXXX" + blob[4:]), ("short", blob[:8]),
                          ("count", blob[:4] + struct.pack("<I", 1000) + blob[8:]),
                          ("offset", blob[:16 + 24 + 16] + struct.pack("<I", len(blob)) + blob[16 + 24 + 20:])):
            with self.subTest(name), self.assertRaises(rv32_ramdisk.RamdiskError):
                rv32_ramdisk.parse(bad)


class ConsoleReceiveTest(unittest.TestCase):
    """O2: the console's RBR and LSR.DR on both backends, trace for trace."""
    setUpClass = classmethod(integer_tests.RtlTest.setUpClass.__func__)
    tearDownClass = classmethod(integer_tests.RtlTest.tearDownClass.__func__)

    def test_bytes_wait_from_reset_and_are_taken_in_order(self):
        words = LI(5, CONSOLE) + [LBU(6, 5, 5), LBU(7, 5, 0), LBU(8, 5, 5), LBU(9, 5, 0), LBU(10, 5, 5), LBU(11, 5, 0)]
        words += [SB(7, 5, 0), SB(9, 5, 0)] + FINISH()  # echo them
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            hex_path, bin_path = write_image(words, directory, "image")
            source = Path(directory) / "input.txt"
            source.write_bytes(b"ok")
            emulator = run_emulator(self.emulator, bin_path, Path(directory) / "emu.trace", console_input=source)
            rtl = run_rtl(self.simulator, hex_path, Path(directory) / "rtl.trace", stall=1, console_input=source)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual((emulator.console, rtl.console), ("ok", "ok"))
        loads = [line.split("->")[1].split("/")[0] for line in rtl.trace if "->" in line]
        self.assertEqual(loads, ["00000021", "0000006f", "00000021", "0000006b", "00000020", "00000000"])


class KernelTest(unittest.TestCase):
    """The kernel's console session in step-tick mode: the emulator and the RTL agree trace for trace."""
    setUpClass = classmethod(integer_tests.RtlTest.setUpClass.__func__)
    tearDownClass = classmethod(integer_tests.RtlTest.tearDownClass.__func__)

    def test_console_session_is_trace_identical_in_step_ticks(self):
        image = OS / "kernel.bin"
        if not image.exists():
            self.skipTest("run make check-rv32-os-image")
        session = ROOT / "programs/rv32/os/session.txt"
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            hex_path = Path(directory) / "kernel.hex"
            from tools.rv32_image import write_hex
            write_hex(hex_path, image.read_bytes())
            emulator = run_emulator(self.emulator, image, Path(directory) / "emu.trace", console_input=session)
            rtl = run_rtl(self.simulator, hex_path, Path(directory) / "rtl.trace", seed=11, ticks="steps",
                          console_input=session, max_cycles=30000000, timeout=600)
        self.assertEqual(rtl.halt["outcome"], "pass", rtl.stderr)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual(rtl.console, (ROOT / "programs/rv32/os/session.expected").read_text())
        self.assertGreater(rtl.halt["interrupts"], 10, "the timer ticked and the kernel took it")
        self.assertIn("kernel: pid 9 fault killed: cause 5", rtl.console)


if __name__ == "__main__":
    unittest.main()
