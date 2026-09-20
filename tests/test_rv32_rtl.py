"""Differential tests for the RV32 RTL core (rtl/rv32/) against the emulator.

Every program is assembled with the shared encoder, run on the emulator and on
the RTL testbench, and the two retirement traces must be identical line for
line (docs/rv32-emulator.md, "Retirement trace contract"), under fixed and
random memory stalls. A few lines are also asserted literally from
hand-computed values so the RTL is not checked only against the emulator.
Traps vector on both backends and a double fault halts both, so every
program's trace, including its trap lines, must be identical (docs/rv32-rtl.md).
"""

import os
from pathlib import Path
import shutil
import tempfile
import unittest

from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_devices import FB_SIZE, event_word, frame_hash
from tools.rv32_image import to_hex_words
from tools.rv32_rtl import (ROOT, Run, check_passed, compile_testbench, cycle_relation, diff_traces, has_value_changes,
                            rtl_halt_line, run_backend, run_emulator, run_rtl, simulator_command, simulator_noise, write_image)
from tools.rv32_run_emu import build_emulator

STALLS = (0, 1, 3)
SEED = 7
OPCODES = (0x37, 0x17, 0x6F, 0x67, 0x63, 0x03, 0x23, 0x13, 0x33, 0x0F, 0x73)


def require(tool, hint):
    if shutil.which(tool) is None:
        raise RuntimeError(f"missing {tool} ({hint})")


def effects(line):
    """What a trace line says after `step pc word`: the register and memory effects, or ''."""
    parts = line.split(" ", 3)
    return parts[3] if len(parts) == 4 else ""


class RtlTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.TemporaryDirectory()
        work = Path(cls.workdir.name)
        require(os.environ.get("HOST_CC", "cc"), "xcode-select --install")
        cls.emulator = work / "rv32emu"
        build_emulator(cls.emulator)
        prebuilt = os.environ.get("RV32_RTL_SIM")
        if prebuilt is not None:
            if not prebuilt or not Path(prebuilt).exists():
                raise RuntimeError(f"RV32_RTL_SIM={prebuilt!r} is not a simulator binary")
            cls.simulator = prebuilt
        else:
            require("iverilog", "brew install icarus-verilog")
            cls.simulator = work / "rv32_tb.vvp"
            compile_testbench(cls.simulator)

    @classmethod
    def tearDownClass(cls):
        cls.workdir.cleanup()

    def run_both(self, words, stall=None, seed=None, limit=100000, max_cycles=None, checkpoints=False,
                 input_script=None):
        """Run one image on both backends; the caller decides what must agree. With `checkpoints`
        both write their `frame N <hash>` lines; `input_script` is text for both `+input`/`--input`."""
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            hex_path, bin_path = write_image(words, directory, "image")
            script = None
            if input_script is not None:
                script = Path(directory) / "input.txt"
                script.write_text(input_script)
            emu_checkpoints = Path(directory) / "emu.checkpoints" if checkpoints else None
            rtl_checkpoints = Path(directory) / "rtl.checkpoints" if checkpoints else None
            emulator = run_emulator(self.emulator, bin_path, Path(directory) / "emu.trace", limit=limit,
                                    checkpoints=emu_checkpoints, input_script=script)
            rtl = run_rtl(self.simulator, hex_path, Path(directory) / "rtl.trace",
                          stall=stall, seed=seed, max_cycles=max_cycles, checkpoints=rtl_checkpoints,
                          input_script=script)
        report = f"\n--- simulator output ---\n{rtl.noise}--- guest console ---\n{rtl.console}--- stderr ---\n{rtl.stderr}"
        self.assertEqual(rtl.status, 0, report)
        self.assertEqual(rtl.noise, "", "the simulator printed something of its own" + report)
        self.assertIsNotNone(rtl.halt, report)
        self.assertIsNotNone(emulator.halt, emulator.stderr)
        return emulator, rtl

    def assert_same_pass(self, words, **kwargs):
        """Both backends retire every instruction identically and end on the pass word."""
        emulator, rtl = self.run_both(words, **kwargs)
        self.assertEqual(emulator.status, 0, emulator.stderr)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual((emulator.halt["halt"], emulator.halt["outcome"]), ("done", "pass"), emulator.stderr)
        self.assertEqual((rtl.halt["halt"], rtl.halt["done"], rtl.halt["outcome"]), ("done", 0x5555, "pass"), rtl.stderr)
        self.assertEqual(rtl.halt["steps"], len(rtl.trace))
        self.assertEqual(rtl.console, emulator.console)
        return emulator, rtl

    def assert_same_double_fault(self, words, cause, value, **kwargs):
        """With mtvec at 0 the first trap vectors to address 0, whose fetch faults: both backends
        print both trap lines and halt as a double fault, with the first trap's CSRs intact."""
        emulator, rtl = self.run_both(words, **kwargs)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual((rtl.halt["halt"], rtl.halt["cause"], rtl.halt["tval"]), ("double-fault", 1, 0), rtl.stderr)
        self.assertEqual(emulator.halt["halt"], "double-fault")
        self.assertTrue(rtl.trace[-2].endswith(f" trap {cause} {value:08x}"), rtl.trace[-2])
        self.assertEqual(rtl.trace[-1], f"{len(rtl.trace)} 00000000 00000000 trap 1 00000000")
        self.assertEqual(rtl.halt["steps"], len(rtl.trace))
        return emulator, rtl

    HANDLER_AT = RAM + 0x200

    def with_handler(self, body, handler, finish=FINISH()):
        """`body` with mtvec pointing at `handler`, laid out the way the emulator tests do it."""
        words = LI(5, self.HANDLER_AT) + [CSRRW(0, MTVEC, 5)] + list(body) + list(finish)
        self.assertLessEqual(len(words), (self.HANDLER_AT - RAM) // 4, "the body overlaps the handler")
        return words + [0] * ((self.HANDLER_AT - RAM) // 4 - len(words)) + list(handler)

    # The canonical loop: docs/rv32-rtl.md, "Run and verify".
    def test_loop_matches_emulator_with_and_without_stalls(self):
        n = 10
        for stall in STALLS:
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(program_loop(n), stall=stall)
        with self.subTest(seed=SEED):
            emulator, rtl = self.assert_same_pass(program_loop(n), seed=SEED)
            # The count depends on the simulator's $random, so only its bounds are pinned.
            self.assertGreater(rtl.halt["stalls"], 0, "seeded stalls must actually stall")
            self.assertLessEqual(rtl.halt["stalls"], 3 * rtl.halt["transfers"])
        # Hand-computed anchors, independent of the emulator.
        self.assertEqual(rtl.trace[0], "1 80000000 00000297 x5=80000000")
        self.assertEqual(rtl.trace[1], "2 80000004 10028293 x5=80000100")
        stores = [effects(line) for line in rtl.trace if "mem[80000100]<-" in line]
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
            LW(0, 9, 4),             # 36 the load happens, the write is discarded
        ] + FINISH()
        for stall in (0, 2):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(words, stall=stall)
        pcs = [int(line.split()[1], 16) - RAM for line in rtl.trace][:19]
        self.assertEqual([pc // 4 for pc in pcs], [0, 1, 2, 3, 4, 5, 6, 8, 10, 11, 12, 13, 15, 17, 16, 18, 19, 20, 21])
        by_step = {line.split()[0]: effects(line) for line in rtl.trace}
        self.assertEqual(by_step["1"], "x1=ffffffff")
        self.assertEqual(by_step["2"], "x2=00000001")
        self.assertEqual(by_step["3"], "x3=fffffffe")
        self.assertEqual(rtl.trace[3], "4 8000000c 00508013", "the x0 write leaves no trace")
        self.assertEqual(by_step["6"], "x5=80001014")
        self.assertEqual(by_step["7"], "x6=8000001c")
        self.assertEqual(by_step["13"], "x8=80000040")
        lanes = [f"mem[{data + lane:08x}]<-" for lane in range(4)]
        stores = [effects(line) for line in rtl.trace if any(lane in line for lane in lanes)]
        self.assertEqual(stores, [f"mem[{data:08x}]<-00000044/1", f"mem[{data + 1:08x}]<-000000ab/1",
                                  f"mem[{data + 2:08x}]<-000000cd/1", f"mem[{data + 3:08x}]<-000000ef/1"])
        loads = [effects(line) for line in rtl.trace if "]->" in line]
        self.assertEqual(loads, [f"x14=efcdab44 mem[{data:08x}]->efcdab44/4",
                                 f"x15=00000000 mem[{data + 4:08x}]->00000000/4",
                                 f"x16=ffffffff mem[{data + 4:08x}]->ffffffff/4",
                                 f"mem[{data + 4:08x}]->ffffffff/4"])

    def test_immediate_fields_come_from_the_right_bits(self):
        # Each case sets one high bit of a split J or B field; the target is zero-filled
        # RAM (an illegal word) or lies below RAM (a fetch fault), so the trap line names it.
        for offset, cause in ((0x400, 2), (0x800, 2), (0x1000, 2), (0x80000, 2), (-0x100000, 1)):
            with self.subTest(jal=hex(offset)):
                target = (RAM + offset) & M
                self.assert_same_double_fault([JAL(0, offset)] + FINISH(), cause, 0 if cause == 2 else target)
        for offset, cause in ((64, 2), (2048, 2), (-2056, 1), (-4096, 1)):
            with self.subTest(beq=offset):
                target = (RAM + offset) & M
                self.assert_same_double_fault([BEQ(0, 0, offset)] + FINISH(), cause, 0 if cause == 2 else target)
        # S and I immediates: negative and maximum offsets reach the same word.
        for offset in (-4, 0x7FC, -2048, 2047 - 3):
            with self.subTest(store=offset):
                base = (RAM + 0x100 - offset) & M
                words = LI(1, base) + LI(2, 0x0BADF00D) + [SW(2, 1, offset), LW(3, 1, offset)] + FINISH()
                emulator, rtl = self.assert_same_pass(words, stall=1)
                self.assertEqual(effects(rtl.trace[4]), "mem[80000100]<-0badf00d/4")
                self.assertEqual(effects(rtl.trace[5]), "x3=0badf00d mem[80000100]->0badf00d/4")
        words = [ADDI(1, 0, -2048), ADDI(2, 0, 2047), LUI(3, 0xFFFFF), AUIPC(4, 0x80000)] + FINISH()
        emulator, rtl = self.assert_same_pass(words, stall=0)
        self.assertEqual([effects(line) for line in rtl.trace[:4]],
                         ["x1=fffff800", "x2=000007ff", "x3=fffff000", "x4=0000000c"])

    def test_not_taken_branch_with_a_misaligned_target_does_not_fault(self):
        words = [ADDI(1, 0, 1), BNE(1, 1, -2), BEQ(1, 0, 6), BEQ(1, 1, 8), ADDI(2, 0, 2)] + FINISH()
        emulator, rtl = self.assert_same_pass(words, stall=1)
        self.assertEqual([line.split()[1] for line in rtl.trace[:5]],
                         ["80000000", "80000004", "80000008", "8000000c", "80000014"])

    def test_illegal_encodings_and_ecall_ebreak_fault(self):
        mul = r_type(0x33, 1, 0, 2, 3, 1)
        fence_i = i_type(0x0F, 0, 1, 0, 0)
        bad_srai = SRAI(1, 1, 0x20 | 0x400 | 1)  # a funct7 bit set that neither srli nor srai allows
        illegal = [mul, fence_i, 0xFFFFFFFF, CSRRW(0, MSTATUS, 1), bad_srai]
        cases = [(word, 2, word) for word in illegal] + [(ECALL(), 11, 0), (EBREAK(), 3, RAM + 4)]
        for word, cause, value in cases:
            with self.subTest(word=f"{word:08x}"):
                words = [ADDI(1, 0, 1), word, ADDI(2, 0, 2)] + FINISH()
                emulator, rtl = self.assert_same_double_fault(words, cause, value, stall=1)
                self.assertEqual(rtl.trace, ["1 80000000 00100093 x1=00000001",
                                             f"2 80000004 {word:08x} trap {cause} {value:08x}",
                                             "3 00000000 00000000 trap 1 00000000"])

    def test_sub_word_loads_and_stores(self):
        data = RAM + 0x200
        words = LI(1, data) + LI(2, 0x80FF7F01) + [
            SW(2, 1, 0),             # 4  mem[data] = 80ff7f01
            LB(3, 1, 0),             # 5  x3 = 01
            LB(4, 1, 1),             # 6  x4 = 7f
            LB(5, 1, 2),             # 7  x5 = ffffffff
            LB(6, 1, 3),             # 8  x6 = ffffff80: sign-extended
            LBU(7, 1, 3),            # 9  x7 = 00000080: zero-extended
            LH(8, 1, 0),             # 10 x8 = 00007f01
            LH(9, 1, 2),             # 11 x9 = ffff80ff
            LHU(10, 1, 2),           # 12 x10 = 000080ff
            SH(2, 1, 4),             # 13 low halfword into lane 0..1 of data+4
            SH(9, 1, 6),             # 14 low halfword of x9 into lane 2..3
            LW(11, 1, 4),            # 15 x11 = 80ff7f01 again, assembled from two halves
            SB(6, 1, 9),             # 16 byte 80 into lane 1 of data+8
            LH(12, 1, 8),            # 17 x12 = ffff8000: the other byte was never written
        ] + LI(13, CONSOLE) + [
            LBU(14, 13, 5),          # 20 the console status byte: 0x20 in lane 1
        ] + FINISH()
        for stall in (0, 2):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(words, stall=stall)
        by_step = {line.split()[0]: effects(line) for line in rtl.trace}
        self.assertEqual(by_step["5"], "mem[80000200]<-80ff7f01/4")
        self.assertEqual(by_step["6"], "x3=00000001 mem[80000200]->00000001/1")
        self.assertEqual(by_step["7"], "x4=0000007f mem[80000201]->0000007f/1")
        self.assertEqual(by_step["8"], "x5=ffffffff mem[80000202]->000000ff/1", "the raw byte, then the extended register")
        self.assertEqual(by_step["9"], "x6=ffffff80 mem[80000203]->00000080/1")
        self.assertEqual(by_step["10"], "x7=00000080 mem[80000203]->00000080/1")
        self.assertEqual(by_step["11"], "x8=00007f01 mem[80000200]->00007f01/2")
        self.assertEqual(by_step["12"], "x9=ffff80ff mem[80000202]->000080ff/2")
        self.assertEqual(by_step["13"], "x10=000080ff mem[80000202]->000080ff/2")
        self.assertEqual(by_step["14"], "mem[80000204]<-00007f01/2")
        self.assertEqual(by_step["15"], "mem[80000206]<-000080ff/2")
        self.assertEqual(by_step["16"], "x11=80ff7f01 mem[80000204]->80ff7f01/4")
        self.assertEqual(by_step["17"], "mem[80000209]<-00000080/1")
        self.assertEqual(by_step["18"], "x12=ffff8000 mem[80000208]->00008000/2")
        self.assertEqual(by_step["21"], "x14=00000020 mem[10000005]->00000020/1", "a byte read the strobe identifies")

    def test_alu_operations_directed(self):
        words = LI(1, 0x7FFFFFFF) + [
            ADDI(2, 1, 1),           # 2  x2 = 80000000: the signed overflow wraps
            SLTI(3, 2, 0),           # 3  1: signed 80000000 < 0
            SLTIU(4, 2, -1),         # 4  1: unsigned 80000000 < ffffffff
            SLTIU(5, 2, 1),          # 5  0
            SLT(6, 2, 1),            # 6  1: signed
            SLTU(7, 2, 1),           # 7  0: unsigned
            XORI(8, 1, -1),          # 8  80000000
            ORI(9, 2, 0x7F),         # 9  8000007f
            ANDI(10, 9, -0x80),      # 10 80000000
            SLLI(11, 9, 31),         # 11 80000000
            SRLI(12, 2, 31),         # 12 00000001
            SRAI(13, 2, 31),         # 13 ffffffff
            SRAI(14, 2, 0),          # 14 80000000
            ADDI(15, 0, 33),         # 15 a shift amount past 31 uses its low five bits
            SLL(16, 1, 15),          # 16 fffffffe
            SRL(17, 2, 15),          # 17 40000000
            SRA(18, 2, 15),          # 18 c0000000
            XOR(19, 1, 2),           # 19 ffffffff
            OR(20, 1, 2),            # 20 ffffffff
            AND(21, 9, 2),           # 21 80000000
            SUB(22, 0, 2),           # 22 80000000: negating the minimum is itself
            ADD(23, 2, 2),           # 23 00000000
            FENCE(),                 # 24 retires with no effect
            SLT(0, 1, 2),            # 25 discarded
        ] + FINISH()
        for stall in (0, 1):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(words, stall=stall)
        expected = ["x2=80000000", "x3=00000001", "x4=00000001", "x5=00000000", "x6=00000001", "x7=00000000",
                    "x8=80000000", "x9=8000007f", "x10=80000000", "x11=80000000", "x12=00000001", "x13=ffffffff",
                    "x14=80000000", "x15=00000021", "x16=fffffffe", "x17=40000000", "x18=c0000000", "x19=ffffffff",
                    "x20=ffffffff", "x21=80000000", "x22=80000000", "x23=00000000", "", ""]
        self.assertEqual([effects(line) for line in rtl.trace[2:26]], expected)
        self.assertEqual(rtl.trace[24], "25 80000060 0ff0000f", "fence is a line with nothing after the word")

    def test_branches_at_the_signed_boundary_and_jalr(self):
        words = LI(1, 0x7FFFFFFF) + LI(2, 0x80000000) + [
            BLT(2, 1, 8), ADDI(3, 0, 1),     # 4  taken: -2^31 < 2^31-1 signed
            BLT(1, 2, 8), ADDI(3, 0, 2),     # 6  not taken
            BGE(1, 2, 8), ADDI(3, 0, 3),     # 8  taken
            BGE(2, 1, 8), ADDI(3, 0, 4),     # 10 not taken
            BLTU(1, 2, 8), ADDI(3, 0, 5),    # 12 taken: 7fffffff < 80000000 unsigned
            BLTU(2, 1, 8), ADDI(3, 0, 6),    # 14 not taken
            BGEU(2, 1, 8), ADDI(3, 0, 7),    # 16 taken
            BGEU(1, 2, 8), ADDI(3, 0, 8),    # 18 not taken
            BGE(1, 1, 8), ADDI(3, 0, 9),     # 20 equal: taken
            BGEU(2, 2, 8), ADDI(3, 0, 10),   # 22 equal: taken
            BLT(1, 1, 8), ADDI(3, 0, 11),    # 24 equal: not taken
            AUIPC(4, 0),                     # 26 x4 = pc
            JALR(5, 4, 17),                  # 27 bit 0 cleared: target x4 + 16, x5 = return address
            ADDI(3, 0, 12), ADDI(3, 0, 13),  # 28 skipped
            ADDI(3, 0, 14),                  # 30 landed
            AUIPC(6, 0),                     # 31 x6 = pc
            JALR(6, 6, 12),                  # 32 rd == rs1: the target uses the old value
            ADDI(3, 0, 15),                  # 33 skipped
            JAL(1, 12),                      # 34 call f
            ADDI(3, 0, 17),                  # 35 after the return
            JAL(0, 12),                      # 36 over f, to FINISH
            ADDI(3, 0, 16),                  # 37 f:
            JALR(0, 1, 0),                   # 38 ret
        ] + FINISH()
        for stall in (0, 3):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(words, stall=stall)
        indices = [(int(line.split()[1], 16) - RAM) // 4 for line in rtl.trace]
        self.assertEqual(indices[:30], [0, 1, 2, 3, 4, 6, 7, 8, 10, 11, 12, 14, 15, 16, 18, 19, 20, 22, 24, 25,
                                        26, 27, 30, 31, 32, 34, 37, 38, 35, 36])
        by_step = {line.split()[0]: effects(line) for line in rtl.trace}
        self.assertEqual(by_step["22"], f"x5={RAM + 28 * 4:08x}")
        self.assertEqual(by_step["25"], f"x6={RAM + 33 * 4:08x}")
        self.assertEqual(by_step["26"], f"x1={RAM + 35 * 4:08x}")
        self.assertEqual(effects(rtl.trace[-7]), "x3=00000011", "17 after the return, then the jump over f")

    def test_traps_vector_through_the_handler_and_mret_returns(self):
        # The handler records the CSRs, counts the trap, steps mepc past the instruction, and returns.
        handler = [CSRRS(10, MCAUSE, 0), CSRRS(11, MTVAL, 0), CSRRS(12, MEPC, 0), ADDI(20, 20, 1),
                   ADDI(12, 12, 4), CSRRW(0, MEPC, 12), MRET()]
        mul = r_type(0x33, 1, 0, 2, 3, 1)
        body = LI(1, UNMAPPED) + LI(2, RAM + 0x102) + [
            ECALL(),                 # cause 11, mtval 0
            EBREAK(),                # cause 3, mtval its PC
            mul,                     # cause 2, mtval the word
            LW(3, 1, 0),             # cause 5: refused at acceptance, then the next data access is fine
            SW(3, 2, 0),             # cause 6: misaligned, never reaches the bus
            LW(22, 2, -2),           # the word it would have hit is still zero
            LH(3, 2, 1),             # cause 4: an odd halfword address
            JAL(0, 2),               # cause 0: misaligned target, mepc is the jal itself
            BEQ(0, 0, 6),            # cause 0 again, for a taken branch
            SW(20, 2, -2),           # a real store right after the faults: 8 traps counted
            LW(21, 2, -2),
        ]
        words = self.with_handler(body, handler)
        for stall in (0, 2):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(words, stall=stall)
        traps = [line for line in rtl.trace if " trap " in line]
        self.assertEqual([effects(line) for line in traps],
                         ["trap 11 00000000", f"trap 3 {RAM + 8 * 4:08x}", f"trap 2 {mul:08x}", f"trap 5 {UNMAPPED:08x}",
                          f"trap 6 {RAM + 0x102:08x}", f"trap 4 {RAM + 0x103:08x}", f"trap 0 {RAM + 14 * 4 + 2:08x}",
                          f"trap 0 {RAM + 15 * 4 + 6:08x}"])
        self.assertEqual(len(traps), 8)
        self.assertIn("x22=00000000 mem[80000100]->00000000/4", rtl.trace[int(traps[4].split()[0]) + 7],
                      "the misaligned store wrote nothing")
        self.assertEqual(effects(rtl.trace[-6]), "x21=00000008 mem[80000100]->00000008/4")
        by_step = {line.split()[0]: effects(line) for line in rtl.trace}
        first = int(traps[0].split()[0])
        self.assertEqual([by_step[str(first + i)] for i in range(1, 8)],
                         ["x10=0000000b", "x11=00000000", f"x12={RAM + 7 * 4:08x}", "x20=00000001",
                          f"x12={RAM + 8 * 4:08x}", "", ""], "the handler's lines after the ecall")
        self.assertEqual(rtl.trace[first + 7].split()[1], f"{RAM + 8 * 4:08x}", "mret lands on the ebreak")

    def test_double_fault_and_nested_trap(self):
        # A handler that resets mtvec and then traps: the second trap is delivered to 0 (a nested
        # trap, after the handler retired instructions), and the fetch there is the double fault.
        handler = [CSRRS(10, MCAUSE, 0), CSRRW(0, MTVEC, 0), EBREAK()]
        words = self.with_handler([ECALL()], handler)
        emulator, rtl = self.run_both(words, stall=1)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual((rtl.halt["halt"], rtl.halt["cause"], rtl.halt["tval"]), ("double-fault", 1, 0))
        self.assertEqual([effects(line) for line in rtl.trace[-4:]],
                         ["x10=0000000b", "", f"trap 3 {self.HANDLER_AT + 8:08x}", "trap 1 00000000"])
        # A handler whose first word is illegal never retires: that is the double fault itself.
        words = self.with_handler([ECALL()], [0])
        emulator, rtl = self.run_both(words, stall=0)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual((rtl.halt["halt"], rtl.halt["cause"], rtl.halt["tval"]), ("double-fault", 2, 0))
        self.assertEqual(rtl.trace[-2:], [f"4 {RAM + 12:08x} 00000073 trap 11 00000000",
                                          f"5 {self.HANDLER_AT:08x} 00000000 trap 2 00000000"])
        # A handler whose first instruction is a refused load: the double fault comes from MEM.
        emulator, rtl = self.run_both(self.with_handler([ECALL()], [LW(1, 0, 0)]), stall=2)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual((rtl.halt["halt"], rtl.halt["cause"], rtl.halt["tval"]), ("double-fault", 5, 0))
        # A refused fetch as the very first trap, then its handler fetch: the M3 shape.
        emulator, rtl = self.assert_same_double_fault([JAL(0, -8)], 1, RAM - 8, stall=0)
        self.assertEqual(len(rtl.trace), 3)
        # mret as the handler's first instruction retires, so the re-executed ecall is a nested
        # trap, not a double fault: trap, mret, trap, mret ... until both limits.
        emulator, rtl = self.run_both(self.with_handler([ECALL()], [MRET()]), stall=0, limit=40, max_cycles=200)
        self.assertEqual((rtl.halt["halt"], emulator.halt["halt"]), ("limit", "limit"))
        common = min(len(rtl.trace), len(emulator.trace))
        self.assertGreater(common, 20)
        self.assertEqual(rtl.trace[:common], emulator.trace[:common])
        self.assertTrue(all(" trap 11 " in line for line in rtl.trace[3::2]), "every other line is the ecall")
        self.assertTrue(all(line.endswith(" 30200073") for line in rtl.trace[4::2]), "and the rest are the mret")
        # A nested trap after the handler retired instructions overwrites the CSRs: the handler
        # traps on ebreak the first time through and reads cause 3 and the ebreak's address the second.
        handler = [CSRRS(10, MCAUSE, 0), CSRRS(11, MEPC, 0), ADDI(12, 0, 3), BEQ(10, 12, 8), EBREAK()] + FINISH()
        emulator, rtl = self.assert_same_pass(self.with_handler([ECALL()], handler, finish=[]), stall=1)
        self.assertEqual([effects(line) for line in rtl.trace[4:12]],
                         ["x10=0000000b", f"x11={RAM + 12:08x}", "x12=00000003", "",
                          f"trap 3 {self.HANDLER_AT + 16:08x}", "x10=00000003", f"x11={self.HANDLER_AT + 16:08x}",
                          "x12=00000003"])

    def test_csr_reads_writes_and_masking(self):
        words = LI(5, RAM + 0x203) + [
            CSRRW(0, MTVEC, 5),      # 2  mtvec = ...200: bits [1:0] dropped
            CSRRS(6, MTVEC, 0),      # 3  x6 = 80000200
            CSRRWI(7, MTVAL, 3),     # 4  x7 = 0, mtval = 3
            CSRRSI(8, MTVAL, 4),     # 5  x8 = 3, mtval = 7
            CSRRCI(9, MTVAL, 1),     # 6  x9 = 7, mtval = 6
            CSRRS(10, MTVAL, 0),     # 7  x10 = 6
            CSRRSI(0, MTVAL, 0),     # 8  nothing to read or write
            CSRRWI(18, MTVAL, 31),   # 9  x18 = 6, mtval = 1f: the five-bit field is zero-extended
            CSRRS(19, MTVAL, 0),     # 10 x19 = 1f
            CSRRWI(0, MTVAL, 0),     # 11 csrrwi writes even with rd = 0 and uimm = 0
            CSRRS(20, MTVAL, 0),     # 12 x20 = 0
        ] + LI(11, 0xFFFFFFFF) + [
            CSRRC(12, MEPC, 11),     # 15 x12 = 0
            CSRRW(13, MEPC, 11),     # 16 x13 = 0, mepc = fffffffc
            CSRRS(14, MEPC, 0),      # 17 x14 = fffffffc
            CSRRW(15, MCAUSE, 11),   # 18 x15 = 0, mcause = ffffffff: no mask
            CSRRC(16, MCAUSE, 11),   # 19 x16 = ffffffff, mcause = 0
            CSRRS(17, MCAUSE, 0),    # 20 x17 = 0
            CSRRW(0, MEPC, 0),       # 21 csrrw with x0 writes zero: mepc back to its reset value
            CSRRS(21, MEPC, 0),      # 22 x21 = 0
        ] + FINISH()
        emulator, rtl = self.assert_same_pass(words, stall=1)
        self.assertEqual([effects(line) for line in rtl.trace[2:23]],
                         ["", "x6=80000200", "x7=00000000", "x8=00000003", "x9=00000007", "x10=00000006", "",
                          "x18=00000006", "x19=0000001f", "", "x20=00000000",
                          "x11=00000000", "x11=ffffffff", "x12=00000000", "x13=00000000", "x14=fffffffc",
                          "x15=00000000", "x16=ffffffff", "x17=00000000", "", "x21=00000000"])
        # mret with mepc at its reset value: the fetch at 0 faults, then vectors to mtvec = 0 and double-faults.
        emulator, rtl = self.assert_same_double_fault([ADDI(1, 0, 1), MRET()] + FINISH(), 1, 0, stall=0)
        self.assertEqual(rtl.trace[1], f"2 {RAM + 4:08x} 30200073")

    def test_full_program_shows_the_three_observations(self):
        # The waves program: sign extension, a discarded x0 write, a stalled store, a call and return.
        emulator, rtl = self.assert_same_pass(program_full(), stall=2)
        self.assertEqual([effects(line) for line in rtl.trace[3:7]],
                         ["mem[80000100]<-00000080/1", "x3=ffffff80 mem[80000100]->00000080/1",
                          "x4=00000080 mem[80000100]->00000080/1", ""])
        self.assertEqual([line.split()[1] for line in rtl.trace[7:12]],
                         [f"{RAM + i * 4:08x}" for i in (7, 10, 11, 8, 9)], "call, f, ret, after, over")
        self.assertEqual(effects(rtl.trace[8]), "x7=fffffff8")
        self.assertEqual((rtl.halt["cycles"], rtl.halt["stalls"]), (4 * 13 + 5 * 4 + 2 * 21, 2 * 21))

    def test_selfcheck_image_when_built(self):
        """The M1 firmware on the RTL: PASS on the console, the emulator's whole trace, the cycle formula."""
        bin_path = ROOT / "build" / "rv32" / "selfcheck.bin"
        if not bin_path.exists():
            self.skipTest("run `make check-rv32-image` first")
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            # The hex is derived from the bin here so both backends run the same image even
            # when build/rv32/selfcheck.hex is stale.
            hex_path = Path(directory) / "selfcheck.hex"
            hex_path.write_text("".join(f"{word}\n" for word in to_hex_words(bin_path.read_bytes())))
            emulator = run_emulator(self.emulator, bin_path, Path(directory) / "emu.trace")
            self.assertEqual(emulator.status, 0, emulator.stderr)
            self.assertIsNotNone(emulator.halt, emulator.stderr)
            self.assertEqual((emulator.halt["halt"], emulator.halt["outcome"], emulator.console),
                             ("done", "pass", "PASS 807d9fad\n"), emulator.stderr)
            self.assertEqual(len(emulator.trace), 32610)
            for stall, seed in ((0, None), (1, None), (None, SEED)):
                with self.subTest(stall=stall, seed=seed):
                    rtl = run_rtl(self.simulator, hex_path, Path(directory) / "rtl.trace", stall=stall, seed=seed)
                    self.assertEqual((rtl.status, rtl.noise), (0, ""), rtl.stderr)
                    self.assertIsNotNone(rtl.halt, rtl.stderr)
                    self.assertEqual((rtl.halt["halt"], rtl.halt["outcome"], rtl.console),
                                     ("done", "pass", "PASS 807d9fad\n"), rtl.stderr)
                    self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
                    relation, holds = cycle_relation(rtl)
                    self.assertTrue(holds, relation)
                    self.assertEqual(rtl.halt["transfers"], 40665, "32,610 fetches and 8,055 data accesses")
                    if stall is not None:
                        self.assertEqual(rtl.halt["cycles"], 138495 + stall * 40665)

    def test_decode_sweep_agrees_with_the_emulator(self):
        """Every opcode x funct3 x representative funct7: the whole trace and the halt reason
        agree, whether the word executes, traps as illegal, or faults for another reason."""
        words = set()
        for opcode in OPCODES:
            for funct3 in range(8):
                # funct7 only selects instructions in the register and immediate ALU classes.
                for funct7 in ((0, 1, 0x20, 0x7F) if opcode in (0x33, 0x13) else (0,)):
                    words.add(r_type(opcode, 1, funct3, 0, 0, funct7))
        for funct3 in range(1, 8):
            words.add(i_type(0x73, 1, funct3, 0, MTVEC))  # CSR forms on a CSR that exists
        outcomes = {"done": 0, "double-fault": 0, "limit": 0}
        for word in sorted(words):
            with self.subTest(word=f"{word:08x}"):
                emulator, rtl = self.run_both([word] + FINISH(), stall=0, limit=200, max_cycles=400)
                self.assertEqual(rtl.halt["halt"], emulator.halt["halt"])
                if rtl.halt["halt"] == "limit":  # jal x1, 0 spins on both: the limits differ, the prefix must not
                    self.assertEqual((rtl.halt["cycles"], rtl.halt["steps"]), (400, len(rtl.trace)))
                    self.assertGreaterEqual(len(rtl.trace), 400 // 5, "a spinning word still retires at the state machine's rate")
                    common = min(len(rtl.trace), len(emulator.trace))
                    self.assertEqual(rtl.trace[:common], emulator.trace[:common])
                else:
                    self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
                outcomes[rtl.halt["halt"]] += 1
        self.assertEqual(len(words), 143)
        self.assertGreater(outcomes["done"], 50, "most words execute and reach the done store")
        self.assertGreater(outcomes["double-fault"], 40, "the illegal ones and the jumps to nowhere trap")

    def test_memory_and_target_faults(self):
        cases = [
            ("load outside the map", LI(1, UNMAPPED) + [LW(2, 1, 0)], 5, UNMAPPED),
            ("misaligned load", LI(1, RAM + 0x102) + [LW(2, 1, 0)], 4, RAM + 0x102),
            ("misaligned store", LI(1, RAM + 0x101) + [SW(1, 1, 0)], 6, RAM + 0x101),
            ("misaligned address outside the map", LI(1, UNMAPPED + 2) + [LW(2, 1, 0)], 4, UNMAPPED + 2),
            ("word store to the console", LI(1, CONSOLE) + [SW(1, 1, 0)], 7, CONSOLE),
            ("byte store to the console status", LI(1, CONSOLE) + [SB(1, 1, 5)], 7, CONSOLE + 5),
            ("word load from the console", LI(1, CONSOLE) + [LW(2, 1, 4)], 5, CONSOLE + 4),
            ("byte store past the console", LI(1, CONSOLE) + [SB(1, 1, 8)], 7, CONSOLE + 8),
            ("load from the done register", LI(1, DONE) + [LW(2, 1, 0)], 5, DONE),
            ("byte store to the done register", LI(1, DONE) + [SB(1, 1, 0)], 7, DONE),
            ("word store past the done register", LI(1, DONE) + [SW(1, 1, 4)], 7, DONE + 4),
            ("store past the end of RAM", LI(1, RAM + 0x400000) + [SW(1, 1, 0)], 7, RAM + 0x400000),
            ("misaligned halfword load", LI(1, RAM + 0x201) + [LH(2, 1, 0)], 4, RAM + 0x201),
            ("misaligned halfword store", LI(1, RAM + 0x203) + [SH(1, 1, 0)], 6, RAM + 0x203),
            ("halfword load of the console status", LI(1, CONSOLE) + [LHU(2, 1, 4)], 5, CONSOLE + 4),
            ("byte load of the console TX register", LI(1, CONSOLE) + [LBU(2, 1, 0)], 5, CONSOLE),
            ("byte load of the done register", LI(1, DONE) + [LB(2, 1, 0)], 5, DONE),
            ("halfword store to the done register", LI(1, DONE) + [SH(1, 1, 0)], 7, DONE),
            ("jalr to a non-word target", LI(1, RAM + 0x100) + [JALR(0, 1, 2)], 0, RAM + 0x102),
            ("jalr with bit 0 set still checks bit 1", LI(1, RAM + 0x100) + [JALR(0, 1, 3)], 0, RAM + 0x102),
            ("fetch from a device address", LI(1, CONSOLE) + [JALR(0, 1, 0)], 1, CONSOLE),
            ("jal to a non-word target", [ADDI(1, 0, 1), JAL(0, 6)], 0, RAM + 4 + 6),
            ("taken branch to a non-word target", [ADDI(1, 0, 1), BEQ(1, 1, -2)], 0, RAM + 4 - 2),
            ("fetch outside RAM", [ADDI(1, 0, 1), JAL(0, -8)], 1, RAM - 4),
            ("byte load of the timer", LI(1, TIMER) + [LBU(2, 1, 0)], 5, TIMER),
            ("halfword store to the timer", LI(1, TIMER) + [SH(1, 1, 0)], 7, TIMER),
            ("word load of an unimplemented timer offset", LI(1, TIMER) + [LW(2, 1, 4)], 5, TIMER + 4),
            ("word store past the timer window", LI(1, TIMER) + [SW(1, 1, 16)], 7, TIMER + 16),
            ("fetch from the timer", LI(1, TIMER) + [JALR(0, 1, 0)], 1, TIMER),
            ("read of the present register", LI(1, DISPLAY) + [LW(2, 1, 0)], 5, DISPLAY),
            ("write to the frame count", LI(1, DISPLAY) + [SW(1, 1, 4)], 7, DISPLAY + 4),
            ("byte read of the width", LI(1, DISPLAY) + [LBU(2, 1, 8)], 5, DISPLAY + 8),
            ("halfword present", LI(1, DISPLAY) + [SH(1, 1, 0)], 7, DISPLAY),
            ("byte store past the framebuffer", LI(1, FB + FB_SIZE) + [SB(1, 1, 0)], 7, FB + FB_SIZE),
            ("word load past the framebuffer", LI(1, FB + FB_SIZE) + [LW(2, 1, 0)], 5, FB + FB_SIZE),
            ("byte load below the framebuffer", LI(1, FB - 1) + [LBU(2, 1, 0)], 5, FB - 1),
            ("fetch from the framebuffer", LI(1, FB) + [JALR(0, 1, 0)], 1, FB),
            ("store to the input queue", LI(1, INPUT) + [SW(1, 1, 0)], 7, INPUT),
            ("store to the held keys", LI(1, INPUT) + [SW(1, 1, 8)], 7, INPUT + 8),
            ("byte read of an event", LI(1, INPUT) + [LBU(2, 1, 0)], 5, INPUT),
            ("halfword read of the count", LI(1, INPUT) + [LHU(2, 1, 4)], 5, INPUT + 4),
            ("read of an unimplemented input offset", LI(1, INPUT) + [LW(2, 1, 12)], 5, INPUT + 12),
        ]
        for name, words, cause, value in cases:
            with self.subTest(name=name):
                emulator, rtl = self.assert_same_double_fault(words + FINISH(), cause, value, stall=2)
                # A fetch fault is one line past the jump that caused it; every other fault is the
                # last word's line; then the handler fetch at 0 adds the double-fault line.
                self.assertEqual(len(rtl.trace), len(words) + 2 if cause == 1 else len(words) + 1)
                self.assertNotIn("]<-", rtl.trace[-2], "a faulting store writes nothing")
        # The last RAM word, halfword, and byte are inside the map, for stores and loads.
        words = LI(1, RAM + 0x3FFFFC) + [SW(1, 1, 0), LW(2, 1, 0), SH(1, 1, 2), SB(1, 1, 3), LHU(3, 1, 2), LBU(4, 1, 3)] + FINISH()
        emulator, rtl = self.assert_same_pass(words, stall=0)
        self.assertEqual(effects(rtl.trace[3]), "x2=803ffffc mem[803ffffc]->803ffffc/4")
        self.assertEqual([effects(line) for line in rtl.trace[4:8]],
                         ["mem[803ffffe]<-0000fffc/2", "mem[803fffff]<-000000fc/1",
                          "x3=0000fcfc mem[803ffffe]->0000fcfc/2", "x4=000000fc mem[803fffff]->000000fc/1"])

    def test_console_bytes_and_done_words(self):
        say = LI(1, CONSOLE)
        for byte in b"Hi\n":
            say += LI(2, byte) + [SB(2, 1, 0)]
        emulator, rtl = self.assert_same_pass(say + FINISH(), stall=1)
        self.assertEqual(rtl.console, "Hi\n")
        self.assertIn("mem[10000000]<-00000048/1", rtl.trace[4])
        # A byte the host cannot decode as text must still be compared, not crash the runner.
        emulator, rtl = self.assert_same_pass(LI(1, CONSOLE) + LI(2, 0xFF) + [SB(2, 1, 0)] + FINISH(), stall=0)
        self.assertEqual(rtl.console, "\\xff")
        # Guest text that looks like a simulator message is still guest text.
        shout = LI(1, CONSOLE)
        for byte in b"ERROR: mine\n":
            shout += LI(2, byte) + [SB(2, 1, 0)]
        emulator, rtl = self.assert_same_pass(shout + FINISH(), stall=0)
        self.assertEqual((rtl.console, rtl.noise), ("ERROR: mine\n", ""))
        for word, outcome in (((3 << 16) | 0x3333, "fail=3"), ((255 << 16) | 0x3333, "fail=255"),
                              ((256 << 16) | 0x3333, "error=undefined-done-word"), (0x3333, "error=undefined-done-word"),
                              (0x7777, "error=reserved-reset-word"), (0xDEAD, "error=undefined-done-word")):
            with self.subTest(word=f"{word:08x}"):
                emulator, rtl = self.run_both(FINISH(word), stall=0)
                self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
                self.assertEqual((rtl.halt["halt"], rtl.halt["done"], rtl.halt["outcome"]), ("done", word, outcome))
                self.assertEqual((emulator.halt["halt"], emulator.halt["outcome"]), ("done", outcome))

    def test_timer_ticks_are_clock_cycles(self):
        """Device time (docs/rv32.md): on the RTL a TICKS read returns the number of the cycle that
        accepts it, so the value is hand-computed from the state machine; the emulator reads its
        instruction count instead, and that line is the only one the two traces may disagree on."""
        read_twice = LI(1, TIMER) + [LW(2, 1, 0), LW(3, 1, 0)] + FINISH()
        for stall in (0, 1):
            with self.subTest(stall=stall):
                emulator, rtl = self.run_both(read_twice, stall=stall)
                self.assertEqual((rtl.halt["halt"], rtl.halt["outcome"]), ("done", "pass"), rtl.stderr)
                # lui and addi take 4 cycles each (plus one stall per fetch); the first lw's MEM state
                # is then cycle 12, or 16 with one stall on each of its three fetches and its load.
                first = 12 + 4 * stall
                second = first + 5 + 2 * stall
                self.assertEqual(effects(rtl.trace[2]), f"x2={first:08x} mem[{TIMER:08x}]->{first:08x}/4")
                self.assertEqual(effects(rtl.trace[3]), f"x3={second:08x} mem[{TIMER:08x}]->{second:08x}/4")
                self.assertEqual(effects(emulator.trace[2]), f"x2=00000002 mem[{TIMER:08x}]->00000002/4",
                                 "the emulator counts executed instructions")
                self.assertEqual(rtl.trace[:2] + rtl.trace[4:], emulator.trace[:2] + emulator.trace[4:],
                                 "everything but the timer values is identical")
                self.assertEqual(rtl.console, emulator.console)
        # A write loads the count: the cycle of the store is V, the first read 5 cycles later is V + 5,
        # and the count wraps through zero on the way.
        wrap = LI(1, TIMER) + LI(3, 0xFFFFFFFE) + [SW(3, 1, 0), LW(2, 1, 0), LW(4, 1, 0)] + FINISH()
        emulator, rtl = self.run_both(wrap, stall=0)
        self.assertEqual((rtl.halt["halt"], rtl.halt["outcome"]), ("done", "pass"), rtl.stderr)
        self.assertEqual(effects(rtl.trace[4]), f"mem[{TIMER:08x}]<-fffffffe/4")
        self.assertEqual([effects(line) for line in rtl.trace[5:7]],
                         [f"x2=00000003 mem[{TIMER:08x}]->00000003/4", f"x4=00000008 mem[{TIMER:08x}]->00000008/4"])
        self.assertEqual([effects(line) for line in emulator.trace[5:7]],
                         [f"x2=ffffffff mem[{TIMER:08x}]->ffffffff/4", f"x4=00000000 mem[{TIMER:08x}]->00000000/4"])

    def test_display_and_framebuffer(self):
        """WIDTH and HEIGHT, byte/halfword/word stores into the framebuffer and reads back, two
        presents with FRAMES read between them. Both backends write the same checkpoint lines, and
        the hash is recomputed here from the same stores so neither backend is its own oracle."""
        last = FB_SIZE - 4
        words = LI(1, DISPLAY) + [LW(2, 1, 8), LW(3, 1, 12), LW(4, 1, 4)]
        words += LI(5, FB) + LI(6, 0x11223344) + [SW(6, 5, 0), SB(6, 5, 4), SH(6, 5, 6)]
        words += LI(7, last) + [ADD(7, 7, 5), SW(6, 7, 0), LW(8, 5, 4), LBU(9, 7, 3)]
        words += LI(10, 1) + [SW(10, 1, 0), LW(11, 1, 4), SB(6, 5, 8), SW(10, 1, 0), LW(12, 1, 4)] + FINISH()
        emulator, rtl = self.assert_same_pass(words, stall=1, checkpoints=True)
        pixels = bytearray(FB_SIZE)
        pixels[0:4] = (0x11223344).to_bytes(4, "little")
        pixels[4] = 0x44
        pixels[6:8] = (0x3344).to_bytes(2, "little")
        pixels[last:last + 4] = (0x11223344).to_bytes(4, "little")
        first = frame_hash(pixels)
        pixels[8] = 0x44
        second = frame_hash(pixels)
        self.assertEqual(rtl.checkpoints, [f"frame 1 {first:08x}", f"frame 2 {second:08x}"])
        self.assertEqual(emulator.checkpoints, rtl.checkpoints)
        self.assertEqual([effects(line) for line in rtl.trace[2:5]],
                         [f"x2=00000140 mem[{DISPLAY + 8:08x}]->00000140/4", f"x3=000000f0 mem[{DISPLAY + 12:08x}]->000000f0/4",
                          f"x4=00000000 mem[{DISPLAY + 4:08x}]->00000000/4"])
        self.assertEqual([effects(line) for line in rtl.trace[9:17]],
                         [f"mem[{FB:08x}]<-11223344/4", f"mem[{FB + 4:08x}]<-00000044/1", f"mem[{FB + 6:08x}]<-00003344/2",
                          "x7=00013000", "x7=00012bfc", f"x7={FB + last:08x}", f"mem[{FB + last:08x}]<-11223344/4",
                          f"x8=33440044 mem[{FB + 4:08x}]->33440044/4"])
        self.assertEqual(effects(rtl.trace[17]), f"x9=00000011 mem[{FB + last + 3:08x}]->00000011/1")
        self.assertEqual([effects(line) for line in rtl.trace[20:22]],
                         [f"mem[{DISPLAY:08x}]<-00000001/4", f"x11=00000001 mem[{DISPLAY + 4:08x}]->00000001/4"])
        self.assertEqual(effects(rtl.trace[24]), f"x12=00000002 mem[{DISPLAY + 4:08x}]->00000002/4")
        # No present, no checkpoint; a present with the value 0 still presents.
        emulator, rtl = self.assert_same_pass(LI(1, DISPLAY) + [SW(0, 1, 0)] + FINISH(), stall=0, checkpoints=True)
        self.assertEqual((rtl.checkpoints, emulator.checkpoints), ([f"frame 1 {frame_hash(bytes(FB_SIZE)):08x}"],) * 2)
        emulator, rtl = self.assert_same_pass(FINISH(), stall=0, checkpoints=True)
        self.assertEqual((rtl.checkpoints, emulator.checkpoints), ([], []))

    def test_input_events_arrive_at_frames(self):
        """A script's events are readable after the present that reaches their frame (frame 0 from
        reset); EVENT pops, COUNT counts, KEYS follows arrivals; the sequence the guest reads is
        identical on both backends although the RTL pushes one event per cycle."""
        down, up = event_word(True, 1), event_word(False, 1)
        script = "frame 0 down LEFT\nframe 0 up left\nframe 1 down A\nframe 2 up 8\n"
        words = LI(1, INPUT) + LI(2, DISPLAY) + [LW(3, 1, 4), LW(4, 1, 8), LW(5, 1, 0), LW(6, 1, 0), LW(7, 1, 0), LW(8, 1, 4)]
        words += [SW(0, 2, 0), LW(9, 1, 4), LW(10, 1, 8), LW(11, 1, 0), SW(0, 2, 0), LW(12, 1, 0), LW(13, 1, 8), LW(14, 1, 0)]
        for stall in (0, 2):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same_pass(words + FINISH(), stall=stall, input_script=script)
                self.assertEqual([effects(line) for line in rtl.trace[4:10]],
                                 [f"x3=00000002 mem[{INPUT + 4:08x}]->00000002/4", f"x4=00000000 mem[{INPUT + 8:08x}]->00000000/4",
                                  f"x5={down:08x} mem[{INPUT:08x}]->{down:08x}/4", f"x6={up:08x} mem[{INPUT:08x}]->{up:08x}/4",
                                  f"x7=00000000 mem[{INPUT:08x}]->00000000/4", f"x8=00000000 mem[{INPUT + 4:08x}]->00000000/4"])
                self.assertEqual([effects(line) for line in rtl.trace[11:14]],
                                 [f"x9=00000001 mem[{INPUT + 4:08x}]->00000001/4", f"x10=00000100 mem[{INPUT + 8:08x}]->00000100/4",
                                  f"x11={event_word(True, 8):08x} mem[{INPUT:08x}]->{event_word(True, 8):08x}/4"])
                self.assertEqual([effects(line) for line in rtl.trace[15:18]],
                                 [f"x12={event_word(False, 8):08x} mem[{INPUT:08x}]->{event_word(False, 8):08x}/4",
                                  f"x13=00000000 mem[{INPUT + 8:08x}]->00000000/4", f"x14=00000000 mem[{INPUT:08x}]->00000000/4"])
        # A burst of 16 events at frame 1 is queued whole before the guest's next load can see it:
        # the RTL holds that load off until the pushes end (visible as stalls at +stall=0), the
        # emulator queues them in one step, and both read COUNT 16.
        burst = "".join(f"frame 1 down {code}\n" for code in range(16))
        words = LI(1, INPUT) + LI(2, DISPLAY) + [SW(0, 2, 0), LW(3, 1, 4), LW(4, 1, 8)] + FINISH()
        emulator, rtl = self.assert_same_pass(words, stall=0, input_script=burst)
        self.assertEqual([effects(line) for line in rtl.trace[5:7]],
                         [f"x3=00000010 mem[{INPUT + 4:08x}]->00000010/4", f"x4=0000ffff mem[{INPUT + 8:08x}]->0000ffff/4"])
        self.assertGreater(rtl.halt["stalls"], 0, "the load waited for the burst")
        # A seventeenth event at the same frame is dropped by the host and reported on stderr.
        emulator, rtl = self.assert_same_pass(words, stall=0, input_script=burst + "frame 1 down 16\n")
        self.assertEqual(effects(rtl.trace[5]), f"x3=00000010 mem[{INPUT + 4:08x}]->00000010/4")
        self.assertEqual(effects(rtl.trace[6]), f"x4=0000ffff mem[{INPUT + 8:08x}]->0000ffff/4", "a dropped event never arrives")
        for run, name in ((rtl, "rv32_tb"), (emulator, "rv32emu")):
            self.assertIn(f"{name}: input queue full: dropped frame 1 event {event_word(True, 16):08x}", run.stderr)
        # Events for a frame that is never presented stay with the host.
        emulator, rtl = self.assert_same_pass(LI(1, INPUT) + [LW(3, 1, 4)] + FINISH(), stall=0,
                                              input_script="frame 3 down A\n")
        self.assertEqual(effects(rtl.trace[2]), f"x3=00000000 mem[{INPUT + 4:08x}]->00000000/4")

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
        # ecall traps on cycle 6 and the handler fetch at 0 double-faults on cycle 7.
        emulator, rtl = self.run_both([ADDI(1, 0, 1), ECALL()], stall=0, max_cycles=7)
        self.assertEqual((rtl.halt["halt"], rtl.halt["cycles"], rtl.halt["cause"]), ("double-fault", 7, 1), rtl.stderr)
        self.assertEqual(rtl.stderr.count("rv32_tb: halt="), 1, rtl.stderr)
        emulator, rtl = self.run_both([ADDI(1, 0, 1), ECALL()], stall=0, max_cycles=6)
        self.assertEqual((rtl.halt["halt"], rtl.halt["cycles"], len(rtl.trace)), ("limit", 6, 2), rtl.stderr)
        self.assertEqual(rtl.trace[-1], "2 80000004 00000073 trap 11 00000000", "the trap line is written before the limit")

    def test_testbench_refuses_bad_arguments_and_images(self):
        """Harness mistakes fail loudly on both simulators instead of reporting a pass."""
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            hex_path, _ = write_image(program_loop(), directory, "image")
            trace = Path(directory) / "rtl.trace"

            console = Path(directory) / "console"

            def run(extra_args=(), image=hex_path):
                command = simulator_command(self.simulator, image, trace=trace, console=console) + list(extra_args)
                return run_backend(command, trace, rtl_halt_line, 60, console=console)

            for args in (["+stall=abc"], ["+stall=3junk"], ["+stall="], ["+stall=-1"], ["+stall-seed=x"],
                         ["+max-cycles=0"], ["+max-cycles=abc"], ["+max-cycles=99999999999"],
                         ["+stall=2", "+stall-seed=7"], ["+wave=/nonexistent/dir/w.vcd"]):
                with self.subTest(args=args):
                    result = run(args)
                    self.assertNotEqual(result.status, 0, result.noise)
                    self.assertIsNone(result.halt)
                    self.assertNotEqual(result.noise, "", "the simulator must say why")
            for content in ("hello\n", "0002a403\n@1\n", "xxxxxxxx\n", ""):
                with self.subTest(image=content):
                    bad = Path(directory) / "bad.hex"
                    bad.write_text(content)
                    result = run(image=bad)
                    self.assertNotEqual(result.status, 0, result.noise)
                    self.assertIsNone(result.halt)
            script = Path(directory) / "input.txt"
            for content in ("frame 1 down\n", "frame x down A\n", "frame 1 press A\n", "frame 2 down A\nframe 1 up A\n",
                            "frame 1 down NOPE\n", "frame 1 down 32\n", "key 1 down A\n", "frame 1 down A extra\n"):
                with self.subTest(script=content):
                    script.write_text(content)
                    result = run([f"+input={script}"])
                    self.assertNotEqual(result.status, 0, result.noise)
                    self.assertIsNone(result.halt)
                    self.assertIn("Input script", result.noise)
            script.write_text("# only a comment\n\n  frame 0 down Left  \n")
            result = run([f"+input={script}"])
            self.assertEqual((result.status, result.halt["outcome"]), (0, "pass"), result.noise)
            result = run([f"+input={Path(directory) / 'missing.txt'}"])
            self.assertNotEqual(result.status, 0)
            self.assertIn("Cannot open input script", result.noise)
            result = run(image=Path(directory) / "missing.hex")
            self.assertNotEqual(result.status, 0)
            self.assertIn("Cannot open", result.noise)


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

    def test_rtl_halt_line_parsing_and_validation(self):
        line = "noise\nrv32_tb: halt=done cycles=336 steps=78 stalls=0 transfers=102 done=00005555 pass\n"
        self.assertEqual(rtl_halt_line(line), {"halt": "done", "cycles": 336, "steps": 78, "stalls": 0,
                                               "transfers": 102, "done": 0x5555, "outcome": "pass"})
        fault = "rv32_tb: halt=double-fault cycles=9 steps=2 stalls=0 transfers=2 cause=2 tval=deadbeef error=double-fault"
        self.assertEqual((rtl_halt_line(fault)["cause"], rtl_halt_line(fault)["tval"], rtl_halt_line(fault)["outcome"]),
                         (2, 0xDEADBEEF, "error=double-fault"))
        self.assertEqual(rtl_halt_line("rv32_tb: halt=limit cycles=200 steps=50 stalls=0 transfers=51 error=limit")["outcome"],
                         "error=limit")
        self.assertIsNone(rtl_halt_line("no halt line here"))
        for bad in ("rv32_tb: halt=done cycles=336 steps=78 stalls=0 transfers=102 done=xxxxxxxx pass",
                    "rv32_tb: halt=done cycles=",
                    "rv32_tb: halt=done cycles=336 steps=78 stalls=0 transfers=102 pass",
                    "rv32_tb: halt=limit cycles=335 steps=77 stalls=0 transfers=102 done=00005555 pass",
                    "rv32_tb: halt=crashed cycles=1 steps=0 stalls=0 transfers=0",
                    "rv32_tb: halt=fault cycles=9 steps=2 stalls=0 transfers=2 cause=2 tval=deadbeef error=fault",
                    "rv32_tb: halt=unsupported cycles=9 steps=2 stalls=0 transfers=3 pc=80000008 word=00008067 error=unsupported",
                    "rv32_tb: halt="):
            with self.subTest(line=bad):
                with self.assertRaises(ValueError) as raised:
                    rtl_halt_line(bad)
                self.assertIn("halt", str(raised.exception))

    def test_simulator_noise_keeps_everything_but_the_vcd_notice(self):
        self.assertEqual(simulator_noise("VCD info: dumpfile x.vcd opened for output.\n"), "")
        self.assertEqual(simulator_noise("VCD info: opened\nFATAL: tests/rv32_tb.sv:1: x\n"), "FATAL: tests/rv32_tb.sv:1: x\n")
        self.assertEqual(simulator_noise("ERROR: anything else\n"), "ERROR: anything else\n")

    def test_simulator_command(self):
        self.assertEqual(simulator_command("x.vvp", "img.hex", trace="t", console="c", stall=2),
                         ["vvp", "x.vvp", "+image=img.hex", "+trace=t", "+console=c", "+stall=2"])
        self.assertEqual(simulator_command("build/verilator-rv32/rv32_sim", "img.hex", wave="w", seed=7, max_cycles=9),
                         ["build/verilator-rv32/rv32_sim", "+verilator+quiet", "+image=img.hex", "+wave=w",
                          "+stall-seed=7", "+max-cycles=9"])

    def test_check_passed_rejects_anything_but_a_clean_pass(self):
        good = Run(0, "ERROR: guest text is fine\n", "", "rv32_tb: halt=done ...", [], {"halt": "done", "outcome": "pass"})
        check_passed(good)
        for run in (good._replace(status=3), good._replace(noise="FATAL: x\n"), good._replace(halt=None),
                    good._replace(halt={"halt": "done", "outcome": "fail=3"}),
                    good._replace(halt={"halt": "limit", "outcome": "error=limit"})):
            with self.subTest(run=run):
                with self.assertRaises(SystemExit):
                    check_passed(run)

    def test_cycle_relation(self):
        halt = {"cycles": 4 * 3 + 5 * 2 + 7, "stalls": 7, "transfers": 5 + 2}
        run = Run(0, "", "", "", ["1 a b x1=1", "2 a b mem[x]<-1/4", "3 a b", "4 a b x2=2 mem[y]->0/1", "5 a b"], halt)
        relation, holds = cycle_relation(run)
        self.assertEqual(relation, "cycles 29 = 4 x 3 + 5 x 2 + 7 stalls; transfers 7 = 5 fetches + 2 data")
        self.assertTrue(holds)
        self.assertEqual(cycle_relation(run._replace(halt=dict(halt, cycles=30)))[1], False)
        self.assertEqual(cycle_relation(run._replace(halt=dict(halt, transfers=8)))[1], False)
        relation, holds = cycle_relation(run._replace(trace=run.trace + ["6 a b trap 2 b"]))
        self.assertIsNone(holds, "a trap line makes the formula inapplicable")
        self.assertIn("(not exact: 1 trap lines)", relation)

    def test_has_value_changes(self):
        self.assertTrue(has_value_changes("$timescale 1ps $end\n$enddefinitions $end\n#0\n1!\n"))
        self.assertFalse(has_value_changes("$timescale 1ps $end\n$enddefinitions $end\n"), "a header-only dump")
        self.assertFalse(has_value_changes(""))

    def test_run_rtl_truncates_a_stale_trace(self):
        with tempfile.TemporaryDirectory() as directory:
            trace = Path(directory) / "rtl.trace"
            trace.write_text("1 80000000 00000013\n")
            result = run_rtl(Path(directory) / "missing.vvp", Path(directory) / "img.hex", trace)
            self.assertNotEqual(result.status, 0)
            self.assertEqual(result.trace, [], "a crash cannot inherit an old passing trace")
            self.assertIsNone(result.halt)

    def test_encoder_bounds_and_program_helpers(self):
        for bad in (lambda: ADDI(32, 0, 0), lambda: ADDI(1, 0, 0x1000), lambda: ADDI(1, 0, -0x801),
                    lambda: BEQ(0, 0, 3), lambda: BEQ(0, 0, 0x1000), lambda: JAL(0, 0x100000),
                    lambda: LUI(1, 0x100000), lambda: SW(1, 1, 0x1000), lambda: r_type(0x33, 1, 8, 0, 0, 0)):
            with self.assertRaises(AssertionError):
                bad()
        self.assertEqual(ADDI(1, 0, -1), 0xFFF00093)
        self.assertEqual(SW(1, 1, 0x800), SW(1, 1, -2048), "a 12-bit field accepts its unsigned spelling")
        self.assertEqual(len(program_loop(LOOP_MAX_N)), 18 + 8)
        self.assertEqual(len(program_full()), 17)
        self.assertEqual(sorted(PROGRAMS), ["full", "loop"])
        with self.assertRaises(AssertionError):
            program_loop(LOOP_MAX_N + 1)
        words = program_loop()
        self.assertEqual(words_to_hex(words), "".join(f"{word}\n" for word in to_hex_words(words_to_bytes(words))),
                         "the testbench image format is what tools/rv32_image.py --hex writes")


if __name__ == "__main__":
    unittest.main()
