"""A base for directed programs that run on the emulator and the RTL in step-tick mode and are
compared trace for trace (Track 2: O1's interrupts, O5's protection; issue #34's atomics), with
helpers they share. Not a test module itself: discovery looks for test_*.py."""
import tempfile
import unittest
from pathlib import Path

import test_rv32_rtl as integer_tests
from tools.rv32_asm import ADDI, CSRRW, FINISH, LI, MSIP, MTIMECMP, MTVEC, RAM, SW
from tools.rv32_rtl import cycle_relation, diff_traces, run_emulator, run_rtl, write_image

HANDLER = RAM + 0x400  # handlers live here; bodies must stay below
SAVE = RAM + 0x2000    # where handlers store what they saw

DISARM_TIMER = LI(27, MTIMECMP) + [ADDI(26, 0, -1), SW(26, 27, 0), SW(26, 27, 4)]
DISARM_SOFTWARE = LI(27, MSIP) + [SW(0, 27, 0)]


def set_timer(value):
    """mtimecmp = value (below 2^32): low word to all ones first, then high 0, then the low word."""
    return LI(27, MTIMECMP) + [ADDI(26, 0, -1), SW(26, 27, 0), SW(0, 27, 4)] + LI(26, value) + [SW(26, 27, 0)]


def stored(trace, address):
    """The last value a trace line stored at `address`, or None."""
    value = None
    for line in trace:
        if f"mem[{address:08x}]<-" in line:
            value = int(line.split(f"mem[{address:08x}]<-")[1].split("/")[0], 16)
    return value


def at_handler(body, handler):
    """`body`, padded to HANDLER, then `handler`; mtvec is set by the body's first words."""
    words = LI(5, HANDLER) + [CSRRW(0, MTVEC, 5)] + list(body)
    assert len(words) <= (HANDLER - RAM) // 4, "the body overlaps the handler"
    return words + [0] * ((HANDLER - RAM) // 4 - len(words)) + list(handler)


def dump(*regs):
    """Store registers at SAVE + 0x40 onwards, then finish: the trace shows each store's value."""
    words = LI(28, SAVE + 0x40)
    for i, reg in enumerate(regs):
        words.append(SW(reg, 28, 4 * i))
    return words + FINISH()


class StepTicksCase(unittest.TestCase):
    """Builds the emulator and the simulator once per class, as the RTL tests do."""
    setUpClass = classmethod(integer_tests.RtlTest.setUpClass.__func__)
    tearDownClass = classmethod(integer_tests.RtlTest.tearDownClass.__func__)

    def run_both(self, words, ticks="steps", stall=None, seed=None, input_script=None, limit=200000, halt="done",
                 gpu_seed=None):
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            hex_path, bin_path = write_image(words, directory, "image")
            script = None
            if input_script is not None:
                script = Path(directory) / "input.txt"
                script.write_text(input_script)
            emulator = run_emulator(self.emulator, bin_path, Path(directory) / "emu.trace", limit=limit, input_script=script)
            rtl = run_rtl(self.simulator, hex_path, Path(directory) / "rtl.trace", stall=stall, seed=seed,
                          input_script=script, ticks=ticks, max_cycles=4000000, gpu_seed=gpu_seed)
        report = f"\n--- simulator ---\n{rtl.noise}--- console ---\n{rtl.console}--- stderr ---\n{rtl.stderr}{emulator.stderr}"
        self.assertEqual(rtl.status, 0, report)
        self.assertEqual(rtl.noise, "", report)
        self.assertEqual(emulator.halt["halt"], halt, report)
        self.assertEqual(rtl.halt["halt"], halt, report)
        return emulator, rtl

    def assert_same(self, words, **kwargs):
        """Step ticks: identical traces and a pass on both backends."""
        emulator, rtl = self.run_both(words, **kwargs)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual((emulator.halt["outcome"], rtl.halt["outcome"]), ("pass", "pass"), emulator.stderr + rtl.stderr)
        self.assertEqual(rtl.halt["steps"], len(rtl.trace))
        return emulator, rtl

    def assert_relation(self, rtl):
        """The testbench's cycle and transfer counts follow the trace (a trap-free run)."""
        text, holds = cycle_relation(rtl)
        self.assertTrue(holds, text)
