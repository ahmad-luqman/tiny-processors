"""Differential tests for the RV32 RTL core (rtl/rv32/) against the emulator.

Every program is assembled with the shared encoder, run on the emulator and on
the RTL testbench, and the two retirement traces must be identical line for
line (docs/rv32-emulator.md, "Retirement trace contract"), under fixed and
random memory stalls. A few lines are also asserted literally from
hand-computed values so the RTL is not checked only against the emulator.
The stops the slice defines (docs/rv32-rtl.md) are checked against the
emulator's trap lines and halt reasons.
"""

import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_rtl import compile_testbench, diff_traces, rtl_halt_line, run_emulator, run_rtl, write_image

ROOT = Path(__file__).resolve().parents[1]
STALLS = (0, 1, 3)
SEED = 7


def require(tool, hint):
    if shutil.which(tool) is None:
        raise RuntimeError(f"missing {tool} ({hint})")


class RtlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.TemporaryDirectory()
        work = Path(cls.workdir.name)
        compiler = os.environ.get("HOST_CC", "cc")
        require(compiler, "xcode-select --install")
        cls.emulator = work / "rv32emu"
        subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(cls.emulator),
                        str(ROOT / "tools" / "rv32emu.c")], check=True)
        prebuilt = os.environ.get("RV32_RTL_SIM")
        if prebuilt:
            if not Path(prebuilt).exists():
                raise RuntimeError(f"RV32_RTL_SIM={prebuilt} does not exist")
            cls.simulator = prebuilt
        else:
            require("iverilog", "brew install icarus-verilog")
            cls.simulator = work / "rv32_tb.vvp"
            compile_testbench(cls.simulator)

    @classmethod
    def tearDownClass(cls):
        cls.workdir.cleanup()

    def run_both(self, words, stall=None, seed=None, limit=100000, max_cycles=None):
        """Run one image on both backends; the caller decides what must agree."""
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            hex_path, bin_path = write_image(words, directory, "image")
            emulator = run_emulator(self.emulator, bin_path, Path(directory) / "emu.trace", limit=limit)
            rtl = run_rtl(self.simulator, hex_path, Path(directory) / "rtl.trace",
                          stall=stall, seed=seed, max_cycles=max_cycles)
        self.assertEqual(rtl.status, 0, rtl.stderr)
        self.assertIsNotNone(rtl.halt, rtl.stderr)
        self.assertIsNotNone(emulator.halt, emulator.stderr)
        return emulator, rtl

    def assert_same_pass(self, words, **kwargs):
        """Both backends retire every instruction identically and end on the pass word."""
        emulator, rtl = self.run_both(words, **kwargs)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual((emulator.halt["halt"], emulator.halt["outcome"]), ("done", "pass"), emulator.stderr)
        self.assertEqual((rtl.halt["halt"], rtl.halt["done"], rtl.halt["outcome"]), ("done", 0x5555, "pass"), rtl.stderr)
        self.assertEqual(rtl.halt["steps"], len(rtl.trace))
        self.assertEqual(rtl.stdout, emulator.stdout)
        return emulator, rtl

    def assert_prefix_then_fault(self, words, cause, value, **kwargs):
        """The RTL's trace is the emulator's through the first trap line; the emulator then double-faults."""
        emulator, rtl = self.run_both(words, **kwargs)
        self.assertEqual(rtl.halt["halt"], "fault", rtl.stderr)
        self.assertEqual((rtl.halt["cause"], rtl.halt["tval"]), (cause, value), rtl.stderr)
        self.assertEqual(rtl.trace, emulator.trace[:len(rtl.trace)])
        self.assertTrue(rtl.trace[-1].endswith(f" trap {cause} {value:08x}"), rtl.trace[-1])
        self.assertEqual(emulator.trace[len(rtl.trace):], [f"{len(rtl.trace) + 1} 00000000 00000000 trap 1 00000000"],
                         "the emulator fetches the missing handler at mtvec = 0, then stops")
        self.assertEqual(emulator.halt["halt"], "double-fault")
        self.assertEqual(rtl.halt["steps"], len(rtl.trace))
        return emulator, rtl

    # The canonical loop: docs/rv32-rtl.md, "Run and verify".
    def test_loop_matches_emulator_with_and_without_stalls(self):
        n = 10
        for stall in STALLS:
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(program_loop(n), stall=stall)
        with self.subTest(seed=SEED):
            emulator, rtl = self.assert_same_pass(program_loop(n), seed=SEED)
        # Hand-computed anchors, independent of the emulator.
        self.assertEqual(rtl.trace[0], "1 80000000 00000297 x5=80000000")
        self.assertEqual(rtl.trace[1], "2 80000004 10028293 x5=80000100")
        stores = [line.split(" ", 3)[3] for line in rtl.trace if "mem[80000100]<-" in line]
        self.assertEqual(stores[-1], "mem[80000100]<-00000037/4", "sum 1..10 stored")
        self.assertIn("mem[80000105]<-000000a8/1", rtl.trace[-10], "sb shows the narrowed byte in lane 1")
        self.assertTrue(rtl.trace[-9].endswith("x11=0000a800 mem[80000104]->0000a800/4"),
                        "the word around the byte was never written, so the other lanes read zero")
        self.assertTrue(rtl.trace[-1].endswith("mem[00100000]<-00005555/4"), "the done store retires last")
        self.assertEqual(len(rtl.trace), 18 + 6 * n, "5 setup, 6 per iteration, 8 after the loop, 5 to finish")

    def test_cycle_count_follows_the_state_machine(self):
        for stall in (0, 2):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(program_loop(), stall=stall)
                steps = len(emulator.trace)
                memory = sum("mem[" in line for line in emulator.trace)
                transfers = steps + memory
                self.assertEqual(rtl.halt["transfers"], transfers)
                self.assertEqual(rtl.halt["stalls"], stall * transfers)
                self.assertEqual(rtl.halt["cycles"], 4 * (steps - memory) + 5 * memory + stall * transfers)

    def test_every_subset_instruction_directed(self):
        data = RAM + 0x200
        words = [
            ADDI(1, 0, -1),          # 0  x1 = ffffffff
            SUB(2, 0, 1),            # 1  x2 = 0 - (-1) = 1
            SUB(3, 1, 2),            # 2  x3 = -2
            ADDI(0, 1, 5),           # 3  discarded: no x0 field in the trace
            LUI(4, 0x80000),         # 4  x4 = 80000000
            AUIPC(5, 1),             # 5  x5 = 80000014 + 1000
            JAL(6, 8),               # 6  -> 8, x6 = 8000001c
            ADDI(7, 0, 1),           # 7  skipped
            BEQ(7, 0, 8),            # 8  taken -> 10
            ADDI(7, 0, 2),           # 9  skipped
            BNE(7, 0, 8),            # 10 not taken
            ADDI(7, 7, 3),           # 11 x7 = 3
            BEQ(7, 0, 8),            # 12 not taken
            BNE(7, 0, 8),            # 13 taken -> 15
            ADDI(7, 0, 9),           # 14 skipped
            JAL(8, 8),               # 15 -> 17, x8 = 80000040
            JAL(0, 8),               # 16 -> 18
            JAL(0, -4),              # 17 backward -> 16
        ] + LI(9, data) + LI(10, 0x11223344) + [
            SB(10, 9, 0),            # 22 lane 0 <- 44
        ] + LI(11, 0xAB) + [SB(11, 9, 1)] + LI(12, 0xCD) + [SB(12, 9, 2)] + LI(13, 0xEF) + [
            SB(13, 9, 3),            # 31 lane 3 <- ef
            LW(14, 9, 0),            # 32 x14 = efcdab44
            LW(15, 9, 4),            # 33 x15 = 0: never written
            SW(1, 9, 4),             # 34
            LW(16, 9, 4),            # 35 x16 = ffffffff
        ] + FINISH()
        for stall in (0, 2):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(words, stall=stall)
        pcs = [int(line.split()[1], 16) - RAM for line in rtl.trace][:19]
        self.assertEqual([pc // 4 for pc in pcs], [0, 1, 2, 3, 4, 5, 6, 8, 10, 11, 12, 13, 15, 17, 16, 18, 19, 20, 21])
        effects = {line.split()[0]: line.split(" ", 3)[3] if line.count(" ") >= 3 else "" for line in rtl.trace}
        self.assertEqual(effects["1"], "x1=ffffffff")
        self.assertEqual(effects["2"], "x2=00000001")
        self.assertEqual(effects["3"], "x3=fffffffe")
        self.assertEqual(rtl.trace[3], "4 8000000c 00508013", "the x0 write leaves no trace")
        self.assertEqual(effects["6"], "x5=80001014")
        self.assertEqual(effects["7"], "x6=8000001c")
        self.assertEqual(effects["13"], "x8=80000040")
        stores = [line.split(" ", 3)[3] for line in rtl.trace if f"mem[{data:08x}]<-" in line
                  or f"mem[{data + 1:08x}]<-" in line or f"mem[{data + 2:08x}]<-" in line
                  or f"mem[{data + 3:08x}]<-" in line]
        self.assertEqual(stores, [f"mem[{data:08x}]<-00000044/1", f"mem[{data + 1:08x}]<-000000ab/1",
                                  f"mem[{data + 2:08x}]<-000000cd/1", f"mem[{data + 3:08x}]<-000000ef/1"])
        loads = [line.split(" ", 3)[3] for line in rtl.trace if "]->" in line]
        self.assertEqual(loads, [f"x14=efcdab44 mem[{data:08x}]->efcdab44/4",
                                 f"x15=00000000 mem[{data + 4:08x}]->00000000/4",
                                 f"x16=ffffffff mem[{data + 4:08x}]->ffffffff/4"])

    def test_illegal_encodings_and_ecall_ebreak_fault(self):
        mul = r_type(0x33, 1, 0, 2, 3, 1)
        fence_i = i_type(0x0F, 0, 1, 0, 0)
        cases = [(mul, 2, mul), (fence_i, 2, fence_i), (0xFFFFFFFF, 2, 0xFFFFFFFF),
                 (CSRRW(0, MSTATUS, 1), 2, CSRRW(0, MSTATUS, 1)), (SRAI(1, 1, 0x20 | 0x400 | 1), 2, None),
                 (ECALL(), 11, 0), (EBREAK(), 3, RAM + 4)]
        for word, cause, value in cases:
            with self.subTest(word=f"{word:08x}"):
                if value is None:
                    value = word
                words = [ADDI(1, 0, 1), word, ADDI(2, 0, 2)] + FINISH()
                emulator, rtl = self.assert_prefix_then_fault(words, cause, value, stall=1)
                self.assertEqual(rtl.trace, ["1 80000000 00100093 x1=00000001",
                                             f"2 80000004 {word:08x} trap {cause} {value:08x}"])

    def test_unsupported_encodings_halt_without_a_trace_line(self):
        prefix = LI(1, RAM + 0x100)
        cases = [JALR(0, 1, 0), LB(2, 1, 0), LHU(2, 1, 0), SH(1, 1, 0), BLT(1, 0, 8), BGEU(1, 0, 8),
                 SLTI(2, 1, 1), ANDI(2, 1, 1), SLLI(2, 1, 1), SRAI(2, 1, 1), SLL(2, 1, 1), SRA(2, 1, 1),
                 XOR(2, 1, 1), FENCE(), CSRRW(0, MTVEC, 1), CSRRS(2, MEPC, 0), MRET()]
        for word in cases:
            with self.subTest(word=f"{word:08x}"):
                words = prefix + [word, ADDI(2, 0, 2)] + FINISH()
                emulator, rtl = self.run_both(words, stall=1)
                self.assertEqual(rtl.halt["halt"], "unsupported", rtl.stderr)
                self.assertEqual((rtl.halt["pc"], rtl.halt["word"]), (RAM + 8, word))
                self.assertEqual(len(rtl.trace), 2, "the two li instructions retired, the unsupported one did not")
                self.assertEqual(rtl.trace, emulator.trace[:2])
                self.assertGreater(len(emulator.trace), 2, "the emulator executes the instruction the slice refuses")
                self.assertNotIn("trap 2", emulator.trace[2], "it is not illegal on the machine")
                self.assertEqual(rtl.halt["steps"], 2)

    def test_memory_and_target_faults(self):
        cases = [
            ("load outside the map", LI(1, 0x20000000) + [LW(2, 1, 0)], 5, 0x20000000),
            ("misaligned load", LI(1, RAM + 0x102) + [LW(2, 1, 0)], 4, RAM + 0x102),
            ("misaligned store", LI(1, RAM + 0x101) + [SW(1, 1, 0)], 6, RAM + 0x101),
            ("misaligned address outside the map", LI(1, 0x20000002) + [LW(2, 1, 0)], 4, 0x20000002),
            ("word store to the console", LI(1, CONSOLE) + [SW(1, 1, 0)], 7, CONSOLE),
            ("byte store to the console status", LI(1, CONSOLE) + [SB(1, 1, 5)], 7, CONSOLE + 5),
            ("load from the done register", LI(1, DONE) + [LW(2, 1, 0)], 5, DONE),
            ("byte store to the done register", LI(1, DONE) + [SB(1, 1, 0)], 7, DONE),
            ("store past the end of RAM", LI(1, RAM + 0x400000) + [SW(1, 1, 0)], 7, RAM + 0x400000),
            ("jal to a non-word target", [ADDI(1, 0, 1), JAL(0, 6)], 0, RAM + 4 + 6),
            ("taken branch to a non-word target", [ADDI(1, 0, 1), BEQ(1, 1, -2)], 0, RAM + 4 - 2),
            ("fetch outside RAM", [ADDI(1, 0, 1), JAL(0, -8)], 1, RAM - 4),
        ]
        for name, words, cause, value in cases:
            with self.subTest(name=name):
                emulator, rtl = self.assert_prefix_then_fault(words + FINISH(), cause, value, stall=2)
                self.assertEqual(len(rtl.trace), 3 if name == "fetch outside RAM" else len(words))
                self.assertNotIn("]<-", rtl.trace[-1], "a faulting store writes nothing")

    def test_console_bytes_and_done_words(self):
        say = LI(1, CONSOLE)
        for byte in b"Hi\n":
            say += LI(2, byte) + [SB(2, 1, 0)]
        emulator, rtl = self.assert_same_pass(say + FINISH(), stall=1)
        self.assertEqual(rtl.stdout, "Hi\n")
        self.assertIn("mem[10000000]<-00000048/1", rtl.trace[4])
        for word, outcome in (((3 << 16) | 0x3333, "fail=3"), (0x7777, "error=reserved-reset-word"),
                              (0xDEAD, "error=undefined-done-word")):
            with self.subTest(word=f"{word:08x}"):
                emulator, rtl = self.run_both(FINISH(word), stall=0)
                self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
                self.assertEqual((rtl.halt["halt"], rtl.halt["done"], rtl.halt["outcome"]), ("done", word, outcome))
                self.assertEqual((emulator.halt["halt"], emulator.halt["outcome"]), ("done", outcome))

    def test_runaway_hits_the_cycle_limit(self):
        emulator, rtl = self.run_both([ADDI(1, 1, 1), JAL(0, -4)], limit=50, max_cycles=200)
        self.assertEqual(rtl.halt["halt"], "limit")
        self.assertEqual(rtl.halt["cycles"], 200)
        self.assertEqual(emulator.halt["halt"], "limit")
        self.assertEqual(rtl.trace, emulator.trace[:len(rtl.trace)])
        self.assertEqual(len(rtl.trace), 50, "200 cycles at 4 per instruction")

    def test_terminal_outcome_wins_over_the_limit_on_the_same_edge(self):
        # The loop retires its done store on cycle 336; a limit of exactly 336 must still report done.
        emulator, rtl = self.assert_same_pass(program_loop(), stall=0, max_cycles=336)
        self.assertEqual(rtl.halt["cycles"], 336)
        self.assertEqual(rtl.stderr.count("rv32_tb: halt="), 1, rtl.stderr)
        # One cycle earlier the done store has been accepted but not retired: that is a limit, not a pass.
        emulator, rtl = self.run_both(program_loop(), stall=0, max_cycles=335)
        self.assertEqual((rtl.halt["halt"], rtl.halt["outcome"], rtl.halt.get("done")), ("limit", "error=limit", None), rtl.stderr)
        self.assertEqual(len(rtl.trace), 77)
        emulator, rtl = self.run_both([ADDI(1, 0, 1), ECALL()], stall=0, max_cycles=6)
        self.assertEqual((rtl.halt["halt"], rtl.halt["cycles"], rtl.halt["cause"]), ("fault", 6, 11), rtl.stderr)
        self.assertEqual(rtl.stderr.count("rv32_tb: halt="), 1, rtl.stderr)


class HelperTest(unittest.TestCase):
    def test_diff_traces_reports_first_difference_with_context(self):
        same = ["1 80000000 00000013", "2 80000004 00000013"]
        self.assertIsNone(diff_traces(same, list(same)))
        self.assertEqual(diff_traces(same, ["1 80000000 00000013", "2 80000004 00100093 x1=00000001"]),
                         "line 2: RTL '2 80000004 00000013', emulator '2 80000004 00100093 x1=00000001'; "
                         "preceding ['1 80000000 00000013']")
        self.assertEqual(diff_traces(same, same + ["3 80000008 00000013"]),
                         "traces agree for 2 line(s), then the emulator continues to 3")
        self.assertEqual(diff_traces(same + ["3 80000008 00000013"], same),
                         "traces agree for 2 line(s), then the RTL continues to 3")
        self.assertEqual(diff_traces([], []), None)

    def test_rtl_halt_line_parsing(self):
        line = "noise\nrv32_tb: halt=done cycles=336 steps=78 stalls=0 transfers=102 done=00005555 pass\n"
        self.assertEqual(rtl_halt_line(line), {"halt": "done", "cycles": 336, "steps": 78, "stalls": 0,
                                               "transfers": 102, "done": 0x5555, "outcome": "pass"})
        fault = "rv32_tb: halt=fault cycles=9 steps=2 stalls=0 transfers=2 cause=2 tval=deadbeef error=fault"
        self.assertEqual(rtl_halt_line(fault)["cause"], 2)
        self.assertEqual(rtl_halt_line(fault)["tval"], 0xDEADBEEF)
        self.assertEqual(rtl_halt_line(fault)["outcome"], "error=fault")
        unsupported = "rv32_tb: halt=unsupported cycles=9 steps=2 stalls=0 transfers=3 pc=80000008 word=00008067 error=unsupported"
        self.assertEqual((rtl_halt_line(unsupported)["pc"], rtl_halt_line(unsupported)["word"]), (0x80000008, 0x8067))
        self.assertIsNone(rtl_halt_line("no halt line here"))


if __name__ == "__main__":
    unittest.main()
