"""Hand-computed edge tests for the RV32 emulator (tools/rv32emu.c).

The instruction encoder (tools/rv32_asm.py) is written from the RV32I instruction
formats independently of the emulator's decoder, and every expected value is computed
by hand or with Python integers, never by running the emulator. Each test
assembles a raw image, runs the compiled emulator on it, and inspects the
state dump, the trace, the console output, and the exit status.
"""

from collections import namedtuple
from pathlib import Path
import os
import re
import resource
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest

# The encoder lives in tools/rv32_asm.py so the RTL tests assemble the same words.
from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_devices import (FB_SIZE, KEYS, diag_checksum, event_word, frame_hash, power_on_palette, render_diag_frame,
                                word_hash)
from tools.rv32_diff_qemu import compare, qemu_pcs, trace_pcs
from tools.rv32_pong_native import EXPECTED as PONG_EXPECTED, INPUT as PONG_INPUT
from tools.rv32_run_emu import build_emulator, halt_line


ROOT = Path(__file__).resolve().parents[1]


State = namedtuple("State", "pc x mtvec mepc mcause mtval steps retired traps frames events halt done")
Result = namedtuple("Result", "status stdout stderr state trace halt checkpoints", defaults=([],))
DECIMAL_STATE = ("steps", "retired", "traps", "frames", "events")


def parse_state(text):
    fields = {}
    for line in text.splitlines():
        key, value = line.split()
        fields[key] = value if key == "halt" else int(value, 10 if key in DECIMAL_STATE else 16)
    x = [fields[f"x{i}"] for i in range(32)]
    return State(fields["pc"], x, fields["mtvec"], fields["mepc"], fields["mcause"], fields["mtval"],
                 fields["steps"], fields["retired"], fields["traps"], fields["frames"], fields["events"],
                 fields["halt"], fields.get("done"))


def effects(line):
    """What a trace line says after `step pc word`: the register and memory effects, or ''."""
    parts = line.split(" ", 3)
    return parts[3] if len(parts) == 4 else ""



# misa (issue #36): MXL 1 and the extension letters, derived here so the hex in the C and the RTL is
# checked independently.
MISA_VALUE = 1 << 30 | sum(1 << (ord(c) - ord("A")) for c in "AFIMSU")


def console_16550_program():
    """Issue #36: the console's 16550 subset, run with b"Z" waiting at the console. Returns the words
    (no FINISH, which uses x30 and x31) and the registers they leave: x1-x3, x5-x20. test_rv32_rtl.py runs the same program on both backends."""
    words = LI(5, CONSOLE) + [LBU(6, 5, 2)]                                       # IIR: nothing enabled
    words += LI(7, 0xFF) + [SB(7, 5, 1), LBU(8, 5, 1), SB(7, 5, 4), LBU(9, 5, 4),
                            SB(7, 5, 2), LBU(1, 5, 7), LBU(10, 5, 6)]            # IER, MCR, FCR (lands nowhere: SCR 0), MSR
    words += LI(7, 0x5A) + [SB(7, 5, 7), LBU(11, 5, 7), LBU(12, 5, 2)]            # SCR; IIR: a byte waits, RDI first
    words += LI(7, 0x02) + [SB(7, 5, 1), LBU(13, 5, 2)]                           # IIR: THRI only
    words += LI(7, 0x83) + [SB(7, 5, 3), LBU(14, 5, 3)]                           # DLAB on
    words += LI(7, 0x41) + [SB(7, 5, 0)] + LI(7, 0x42) + [SB(7, 5, 1)]            # DLL, DLM: nothing sent
    words += [LBU(15, 5, 0), LBU(16, 5, 1), LBU(17, 5, 5)]                        # read back; DR still set
    words += LI(7, 0x03) + [SB(7, 5, 3), LBU(18, 5, 1), LBU(19, 5, 0), LBU(20, 5, 5)]  # DLAB off: RBR takes Z
    words += LI(7, 0x01) + [SB(7, 5, 1), LBU(2, 5, 2)]                           # IIR: RDI, but nothing waits
    words += LI(7, 0x03) + [SB(7, 5, 1), LBU(3, 5, 2)]                           # IIR: RDI and THRI, nothing waits
    expected = {6: 0x01, 8: 0x0F, 9: 0x1F, 1: 0x00, 10: 0xB0, 11: 0x5A, 12: 0x04, 13: 0x02, 14: 0x83,
                15: 0x41, 16: 0x42, 17: 0x61, 18: 0x02, 19: ord("Z"), 20: 0x60, 2: 0x01, 3: 0x02}
    return words, expected


def console_prompt_program():
    """Issue #36: with --console-prompt "> " and the input "a\nb\n", nothing waits until the guest
    sends the prompt; then the first line does (and only it), and the second after the prompt again.
    Returns the words (no FINISH) and the registers they leave."""
    words = LI(5, CONSOLE) + [LBU(6, 5, 5)]                                      # LSR: nothing yet
    words += LI(7, ord(">")) + [SB(7, 5, 0)] + LI(7, ord(" ")) + [SB(7, 5, 0)]   # the prompt
    words += [LBU(8, 5, 5), LBU(9, 5, 0), LBU(10, 5, 0), LBU(11, 5, 5)]          # DR; "a", "\n"; DR clear
    words += LI(7, ord(">")) + [SB(7, 5, 0), LBU(12, 5, 5)]                      # half a prompt: still clear
    words += LI(7, ord(" ")) + [SB(7, 5, 0), LBU(13, 5, 0), LBU(14, 5, 0)]       # the rest: "b", "\n"
    return words, {6: 0x60, 8: 0x61, 9: ord("a"), 10: 0x0A, 11: 0x60, 12: 0x60, 13: ord("b"), 14: 0x0A}


def console_prompt_edges_program():
    """Issue #36, the gate's edges, with --console-prompt "aa" and the input "x\ny\nz" (no last
    newline): "aaa" holds two overlapping prompts and releases two lines; an "a" stored under DLAB
    goes to the divisor latch, not the tail; the next "a" completes a third prompt and releases "z";
    then more prompts than lines release nothing. Returns the words (no FINISH) and the registers."""
    send = lambda *chars: [w for c in chars for w in LI(7, ord(c)) + [SB(7, 5, 0)]]  # noqa: E731
    words = LI(5, CONSOLE) + send("a", "a", "a")
    words += [LBU(6, 5, 0), LBU(8, 5, 0), LBU(9, 5, 0), LBU(10, 5, 0), LBU(11, 5, 5)]  # x \n y \n; nothing more
    words += LI(7, 0x80) + [SB(7, 5, 3)] + send("a") + LI(7, 0x03) + [SB(7, 5, 3), LBU(12, 5, 5)]  # DLAB: not a prompt
    words += send("a") + [LBU(13, 5, 0), LBU(14, 5, 5)]                                # "z", then input exhausted
    words += send("a", "a") + [LBU(15, 5, 5)]                                          # prompts beyond the lines
    return words, {6: ord("x"), 8: 0x0A, 9: ord("y"), 10: 0x0A, 11: 0x60, 12: 0x60, 13: ord("z"), 14: 0x60, 15: 0x60}


def machine_information_program():
    """Issue #36: misa ignores writes of every kind, the four ID CSRs read 0, fence.i retires, and
    the done register reads 0. Uses x7 and x21-x28."""
    words = LI(7, 0xFFFFFFFF) + [CSRRW(21, 0x301, 7), CSRRC(0, 0x301, 7), CSRRWI(0, 0x301, 0x1F),
                                 CSRRS(22, 0x301, 0), CSRRS(23, 0xF11, 0), CSRRS(24, 0xF12, 0),
                                 CSRRS(25, 0xF13, 0), CSRRS(26, 0xF14, 0), i_type(0x0F, 0, 1, 0, 0)]
    words += LI(27, DONE) + [LW(28, 27, 0)]
    return words, {21: MISA_VALUE, 22: MISA_VALUE, 23: 0, 24: 0, 25: 0, 26: 0, 28: 0}

class EmulatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.TemporaryDirectory()
        cls.emulator = Path(cls.workdir.name) / "rv32emu"
        build_emulator(cls.emulator)

    @classmethod
    def tearDownClass(cls):
        cls.workdir.cleanup()

    def run_words(self, words, limit=10000, base=RAM, extra=(), checkpoints=False, input_script=None,
                  allow_lost_events=False):
        """Run a raw word image and return the exit status, outputs, state, trace lines, and, when
        asked for, the checkpoint lines; `input_script` is the text of an --input file, and
        `allow_lost_events` passes the flag that keeps a dropped or undelivered event from failing the run."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "image.bin").write_bytes(b"".join(word.to_bytes(4, "little") for word in words))
            command = [str(self.emulator), "--image", str(path / "image.bin"), "--base", f"{base:#x}",
                       "--dump-state", str(path / "state"), "--trace", str(path / "trace"),
                       "--max-instructions", str(limit), *extra]
            if checkpoints:
                command += ["--checkpoints", str(path / "checkpoints")]
            if input_script is not None:
                (path / "input.txt").write_text(input_script)
                command += ["--input", str(path / "input.txt")]
            if allow_lost_events:
                command.append("--allow-lost-events")
            completed = subprocess.run(command, capture_output=True, text=True)
            # A run refused before it starts (a bad argument or script) writes no state or trace.
            state = parse_state((path / "state").read_text()) if (path / "state").exists() else None
            trace = (path / "trace").read_text().splitlines() if (path / "trace").exists() else []
            lines = (path / "checkpoints").read_text().splitlines() if checkpoints else []
        return Result(completed.returncode, completed.stdout, completed.stderr, state, trace,
                      halt_line(completed.stderr), lines)

    def run_pass(self, words, **kwargs):
        result = self.run_words(list(words) + FINISH(), **kwargs)
        self.assertEqual((result.status, result.state.halt, result.state.done), (0, "done", 0x5555), result.stderr)
        return result

    def run_trapping(self, body, handler_extra=()):
        """Run `body` with a handler that records mcause in x10 and mtval in x11, then finishes."""
        handler_at = RAM + 0x200
        handler = [CSRRS(10, MCAUSE, 0), CSRRS(11, MTVAL, 0), CSRRS(12, MEPC, 0), *handler_extra] + FINISH()
        words = LI(5, handler_at) + [CSRRW(0, MTVEC, 5)] + list(body) + FINISH(0x00013333)
        words += [0] * ((handler_at - RAM) // 4 - len(words)) + handler
        result = self.run_words(words)
        self.assertEqual((result.status, result.state.halt), (0, "done"), result.stderr)
        self.assertEqual(result.state.traps, 1)
        return result

    # Arithmetic, logic, shifts, and immediates.

    def test_arithmetic_edges(self):
        words = LI(1, 0x7FFFFFFF) + [ADDI(2, 1, 1)] + LI(3, M) + [
            ADDI(4, 3, 1), SUB(5, 0, 1), SLT(6, 1, 3), SLTU(7, 1, 3), SLTI(8, 3, 0), SLTIU(9, 0, -1),
            XORI(10, 3, -1), ORI(11, 0, -1), ANDI(12, 3, 0x7FF), LUI(13, 0xFFFFF), AUIPC(14, 0),
            ADD(15, 1, 1), XOR(16, 1, 3), OR(17, 1, 3), AND(18, 1, 3), SLTI(19, 1, -2048), SLTIU(20, 3, -1)]
        auipc_pc = RAM + 4 * words.index(AUIPC(14, 0))
        x = self.run_pass(words).state.x
        self.assertEqual(x[2], 0x80000000, "signed overflow wraps")
        self.assertEqual(x[4], 0, "unsigned wrap to zero")
        self.assertEqual(x[5], 0x80000001, "0 - 0x7fffffff")
        self.assertEqual(x[6], 0, "0x7fffffff < -1 is false when signed")
        self.assertEqual(x[7], 1, "0x7fffffff < 0xffffffff is true when unsigned")
        self.assertEqual(x[8], 1, "-1 < 0")
        self.assertEqual(x[9], 1, "sltiu with -1 compares against 0xffffffff")
        self.assertEqual(x[10], 0)
        self.assertEqual(x[11], M)
        self.assertEqual(x[12], 0x7FF)
        self.assertEqual(x[13], 0xFFFFF000)
        self.assertEqual(x[14], auipc_pc)
        self.assertEqual(x[15], 0xFFFFFFFE)
        self.assertEqual(x[16], 0x80000000)
        self.assertEqual(x[17], M)
        self.assertEqual(x[18], 0x7FFFFFFF)
        self.assertEqual(x[19], 0, "0x7fffffff < -2048 is false")
        self.assertEqual(x[20], 0, "0xffffffff < 0xffffffff is false")

    def test_shifts(self):
        words = LI(1, 0x80000000) + LI(2, M) + LI(3, 31) + LI(4, 33) + [
            SRAI(5, 1, 31), SRLI(6, 1, 31), SLLI(7, 2, 31), SRA(8, 1, 3), SRL(9, 1, 3), SLL(10, 2, 3),
            SLL(11, 2, 4), SRL(12, 1, 4), SRA(13, 1, 4), SRAI(14, 2, 5), SLLI(15, 1, 1)]
        x = self.run_pass(words).state.x
        self.assertEqual(x[5], M, "srai by 31 of a negative number is all ones")
        self.assertEqual(x[6], 1)
        self.assertEqual(x[7], 0x80000000)
        self.assertEqual(x[8], M)
        self.assertEqual(x[9], 1)
        self.assertEqual(x[10], 0x80000000)
        self.assertEqual(x[11], 0xFFFFFFFE, "register shift amounts use only the low five bits: 33 -> 1")
        self.assertEqual(x[12], 0x40000000)
        self.assertEqual(x[13], 0xC0000000)
        self.assertEqual(x[14], M)
        self.assertEqual(x[15], 0, "shifting the top bit out")

    def test_x0_is_hardwired(self):
        words = [ADDI(0, 0, 5), LUI(0, 0x12345)] + LI(1, 7) + [ADD(0, 1, 1), ADD(2, 0, 1), FENCE()]
        result = self.run_pass(words)
        self.assertEqual(result.state.x[0], 0)
        self.assertEqual(result.state.x[2], 7)
        self.assertFalse(any("x0=" in line for line in result.trace), "x0 writes never appear in the trace")

    # Loads and stores.

    def test_loads_stores_and_byte_order(self):
        data = RAM + 0x1000
        words = LI(5, data) + LI(6, 0x11223344) + [SW(6, 5, 0), LB(7, 5, 0), LB(8, 5, 3), LBU(9, 5, 3), LW(10, 5, 0)]
        words += LI(11, 0x8001) + [SH(11, 5, 4), LH(12, 5, 4), LHU(13, 5, 4)]
        words += LI(14, 0x1234FFAB) + [SB(14, 5, 6), LW(15, 5, 4), LB(16, 5, 6), LH(17, 5, 6)]
        words += LI(18, 0x80) + [SB(18, 5, 8), LB(19, 5, 8), LBU(20, 5, 8), LW(21, 5, 0)]
        words += LI(22, data + 0x800) + [SW(6, 22, -0x800), SH(11, 22, -2046), SB(18, 22, -1),
                                        LW(23, 22, -0x800), LHU(24, 22, -2046), LBU(25, 22, -1)]
        result = self.run_pass(words)
        x = result.state.x
        self.assertEqual(x[7], 0x44, "little-endian: the low byte is at the low address")
        self.assertEqual(x[8], 0x11)
        self.assertEqual(x[9], 0x11)
        self.assertEqual(x[10], 0x11223344)
        self.assertEqual(x[12], 0xFFFF8001, "lh sign-extends")
        self.assertEqual(x[13], 0x00008001, "lhu zero-extends")
        self.assertEqual(x[15], 0x00AB8001, "sb stores only the low byte")
        self.assertEqual(x[16], 0xFFFFFFAB)
        self.assertEqual(x[17], 0x000000AB, "the byte above sb's target was untouched")
        self.assertEqual(x[19], 0xFFFFFF80)
        self.assertEqual(x[20], 0x80)
        self.assertEqual(x[21], 0x11223344)
        self.assertEqual((x[23], x[24], x[25]), (0x80013344, 0x8001, 0x80),
                         "negative offsets: the halfword at +2 overlaps the word's upper half")
        effects = [line.split(" ", 3)[3] for line in result.trace if "mem[" in line]
        self.assertEqual(effects[0], "mem[80001000]<-11223344/4")
        self.assertEqual(effects[1], "x7=00000044 mem[80001000]->00000044/1")
        self.assertEqual(effects[8], "mem[80001006]<-000000ab/1", "trace shows the narrowed value and width")

    # Branches and jumps.

    def test_branch_conditions_at_signed_boundary(self):
        # a = INT32_MIN, b = 1: every branch skips `addi xN, x0, 1` when taken.
        a, b = 1, 2
        words = [ADDI(10, 0, 0), ADDI(11, 0, 0)]  # a0 and a1 start with the boot convention's values
        words += LI(a, 0x80000000) + LI(b, 1)
        for n, branch in enumerate([BEQ, BNE, BLT, BGE, BLTU, BGEU], start=10):
            words += [branch(a, b, 8), ADDI(n, 0, 1)]
        for n, branch in enumerate([BEQ, BNE, BLT, BGE, BLTU, BGEU], start=16):
            words += [branch(a, a, 8), ADDI(n, 0, 1)]
        x = self.run_pass(words).state.x
        self.assertEqual(x[10:16], [1, 0, 0, 1, 1, 0], "a != b; a < b signed; a >= b unsigned")
        self.assertEqual(x[16:22], [0, 1, 1, 0, 1, 0], "a == a; a >= a both ways")

    def test_backward_branch_loop(self):
        words = LI(1, 3) + [ADDI(2, 2, 10), ADDI(1, 1, -1), BNE(1, 0, -8)]
        result = self.run_pass(words)
        self.assertEqual(result.state.x[2], 30)
        self.assertEqual(result.state.x[1], 0)

    def test_jal_and_jalr(self):
        words = [JAL(1, 8), ADDI(2, 0, 1), ADDI(3, 0, 2)]              # 0-2: skip the first addi
        words += LI(4, RAM + 4 * 7 + 1) + [JALR(5, 4, 0), ADDI(6, 0, 1)]  # 3-6: bit 0 of the target is dropped
        words += [ADDI(7, 0, 3)]                                        # 7: jalr lands here
        words += LI(8, RAM + 4 * 12) + [JALR(8, 8, 0)]                  # 8-10: call; rd == rs1 reads the old value
        words += [JAL(0, 12)]                                           # 11: the return lands here; skip the callee
        words += [ADDI(9, 0, 5), JALR(0, 8, 0)]                         # 12-13: callee, then return through x8
        result = self.run_pass(words)                                   # 14: finish
        x = result.state.x
        self.assertEqual(x[1], RAM + 4)
        self.assertEqual((x[2], x[3]), (0, 2))
        self.assertEqual(x[5], RAM + 4 * 6)
        self.assertEqual((x[6], x[7]), (0, 3))
        self.assertEqual(x[8], RAM + 4 * 11)
        self.assertEqual(x[9], 5)
        pcs = [int(line.split()[1], 16) - RAM for line in result.trace]
        self.assertEqual(pcs[:12], [0, 8, 12, 16, 20, 28, 32, 36, 40, 48, 52, 44])

    def test_misaligned_jump_traps_without_writing_rd(self):
        result = self.run_trapping([ADDI(1, 0, 9), JAL(1, 6)])
        self.assertEqual((result.state.mcause, result.state.x[11]), (0, RAM + 16 + 6))
        self.assertEqual(result.state.x[12], RAM + 16, "mepc is the jump itself")
        self.assertEqual(result.state.x[1], 9, "rd is not written when the jump traps")
        result = self.run_trapping(LI(1, RAM + 0x102) + [JALR(2, 1, 0)])
        self.assertEqual((result.state.mcause, result.state.x[11]), (0, RAM + 0x102))
        result = self.run_trapping(LI(1, RAM + 0x103) + [JALR(2, 1, 0)])
        self.assertEqual(result.state.x[11], RAM + 0x102, "bit 0 is cleared before the alignment check")
        result = self.run_trapping([BEQ(0, 0, 6)])
        self.assertEqual((result.state.mcause, result.state.x[11]), (0, RAM + 12 + 6))

    # Traps, CSRs, and devices.

    def test_ecall_handler_and_mret(self):
        body = [ADDI(1, 0, 1), ECALL(), ADDI(1, 1, 1)]
        resume = [ADDI(12, 12, 4), CSRRW(0, MEPC, 12), MRET()]
        handler_at = RAM + 0x200
        handler = [CSRRS(10, MCAUSE, 0), CSRRS(11, MTVAL, 0), CSRRS(12, MEPC, 0), *resume]
        words = LI(5, handler_at) + [CSRRW(0, MTVEC, 5)] + body + FINISH()
        words += [0] * ((handler_at - RAM) // 4 - len(words)) + handler
        result = self.run_words(words)
        self.assertEqual(result.status, 0, result.stderr)
        x = result.state.x
        self.assertEqual((x[10], x[11]), (11, 0), "ecall from machine mode, mtval zero")
        self.assertEqual(x[12], RAM + 4 * 4 + 4, "mepc was the ecall; handler advanced it")
        self.assertEqual(x[1], 2, "execution resumed after the ecall")
        self.assertEqual(result.state.traps, 1)
        self.assertIn(f"{RAM + 16:08x} 00000073 trap 11 00000000", result.trace[4])

    def test_ebreak_illegal_and_csr_errors(self):
        for body, cause, tval in [
                ([EBREAK()], 3, RAM + 12),
                ([0x00000000], 2, 0),
                ([r_type(0x33, 1, 0, 2, 3, 2)], 2, r_type(0x33, 1, 0, 2, 3, 2)),   # OP with an unused funct7
                ([i_type(0x0F, 0, 2, 0, 0)], 2, i_type(0x0F, 0, 2, 0, 0)),         # a reserved FENCE funct3
                ([CSRRS(1, 0x7C0, 0)], 2, CSRRS(1, 0x7C0, 0)),                     # unimplemented CSR
                ([CSRRS(1, 0xF10, 0)], 2, CSRRS(1, 0xF10, 0)),                     # beside the ID registers (issue #36)
                ([CSRRS(1, 0xF15, 0)], 2, CSRRS(1, 0xF15, 0)),
                ([i_type(0x13, 1, 1, 0, 0x420)], 2, i_type(0x13, 1, 1, 0, 0x420)),  # slli with funct7 set
                ([r_type(0x33, 1, 1, 2, 3, 0x20)], 2, r_type(0x33, 1, 1, 2, 3, 0x20)),  # sll with sub bit
                ([b_type(2, 0, 0, 8)], 2, b_type(2, 0, 0, 8)),                     # unused branch funct3
                ([i_type(0x03, 1, 3, 0, 0)], 2, i_type(0x03, 1, 3, 0, 0)),         # ld does not exist
                ([0x10500073 | 1 << 7], 2, 0x10500073 | 1 << 7),                   # wfi with rd set
                ([0x12000073 | 1 << 7], 2, 0x12000073 | 1 << 7)]:                  # sfence.vma with rd set
            with self.subTest(body=body):
                result = self.run_trapping(body)
                self.assertEqual((result.state.x[10], result.state.x[11]), (cause, tval))
                self.assertEqual(result.state.x[12], RAM + 12)

    def test_csr_read_write_and_masking(self):
        words = LI(1, 0x80000203) + [CSRRW(2, MTVEC, 1), CSRRS(3, MTVEC, 0), CSRRW(4, MEPC, 1), CSRRS(5, MEPC, 0),
                                     CSRRWI(6, MCAUSE, 9), CSRRS(7, MCAUSE, 0), CSRRC(8, MCAUSE, 1),
                                     CSRRS(9, MCAUSE, 0), CSRRS(10, MTVAL, 1), CSRRS(11, MTVAL, 0)]
        x = self.run_pass(words).state.x
        self.assertEqual(x[2], 0, "reset value")
        self.assertEqual(x[3], 0x80000200, "mtvec mode bits read as zero: direct mode only")
        self.assertEqual((x[4], x[5]), (0, 0x80000200), "mepc is word aligned")
        self.assertEqual((x[6], x[7]), (0, 9))
        self.assertEqual((x[8], x[9]), (9, 9 & ~0x80000203 & M))
        self.assertEqual((x[10], x[11]), (0, 0x80000203))

    def test_memory_faults(self):
        cases = [
            ([LW(1, 2, 2)], RAM, 4, RAM + 2),               # misaligned word load
            ([LH(1, 2, 1)], RAM, 4, RAM + 1),               # misaligned halfword load
            ([SH(1, 2, 1)], RAM, 6, RAM + 1),               # misaligned halfword store
            ([SW(1, 2, 2)], RAM, 6, RAM + 2),               # misaligned word store
            ([LW(1, 2, 0)], 0, 5, 0),                       # unmapped: address zero
            ([LBU(1, 2, 0)], RAM + 0x1000000, 5, RAM + 0x1000000),  # first byte past RAM
            ([LW(1, 2, 0)], RAM + 0xFFFFFE, 4, RAM + 0xFFFFFE),   # misaligned before bounds
            ([LH(1, 2, 0)], RAM + 0xFFFFFF, 4, RAM + 0xFFFFFF),
            ([SW(1, 2, 0)], UNMAPPED, 7, UNMAPPED),     # unmapped store
            ([SB(1, 2, 0)], 0x7FFFFFFF, 7, 0x7FFFFFFF),     # one byte below RAM
            ([SW(1, 2, 0)], 0xFFFFFFFC, 7, 0xFFFFFFFC),     # top of the address space
            ([LW(1, 2, 0)], 0xFFFFFFFC, 5, 0xFFFFFFFC),
            ([LHU(1, 2, 0)], CONSOLE, 5, CONSOLE),          # RBR (O2) is a byte
            ([LHU(1, 2, 4)], CONSOLE, 5, CONSOLE + 4),      # a halfword over MCR and LSR: registers are bytes
            ([LW(1, 2, 0)], CONSOLE, 5, CONSOLE),
            ([SB(1, 2, 5)], CONSOLE, 7, CONSOLE + 5),       # status is read-only
            ([SB(1, 2, 6)], CONSOLE, 7, CONSOLE + 6),       # and so is MSR (issue #36)
            ([LHU(1, 2, 2)], CONSOLE, 5, CONSOLE + 2),      # every register is a byte
            ([SH(1, 2, 0)], CONSOLE, 7, CONSOLE),           # TX is byte-only
            ([SW(1, 2, 0)], CONSOLE, 7, CONSOLE),
            ([LBU(1, 2, 0)], DONE, 5, DONE),                # the done register reads a word (issue #36)
            ([LHU(1, 2, 0)], DONE, 5, DONE),
            ([SB(1, 2, 0)], DONE, 7, DONE),                 # and word-only
            ([SH(1, 2, 0)], DONE, 7, DONE),
            ([SW(1, 2, 4)], DONE, 7, DONE + 4),
            ([LBU(1, 2, 0)], MTIME, 5, MTIME),              # mtime is read a word at a time
            ([SH(1, 2, 0)], MTIME, 7, MTIME),
            ([LW(1, 2, -4)], MTIME, 5, MTIME - 4),          # no other CLINT register
            ([SW(1, 2, 12)], MTIME, 7, MTIME + 12),
            ([LW(1, 2, 8)], MTIME, 5, MTIME + 8),           # past the last register, still inside the window
            ([LW(1, 2, 0)], CLINT + 4, 5, CLINT + 4),       # between msip and mtimecmp
            ([LW(1, 2, 0)], CLINT + 0x10000, 5, CLINT + 0x10000),  # past the window
            ([SB(1, 2, 0)], BOOTROM, 7, BOOTROM),           # the boot ROM is read-only
            ([SW(1, 2, 0)], BOOTROM + 0xFFC, 7, BOOTROM + 0xFFC),
            ([LW(1, 2, 0)], BOOTROM + 0x1000, 5, BOOTROM + 0x1000),  # past the ROM
            ([LB(1, 2, 0)], PLIC, 5, PLIC),                 # the PLIC (O1) takes words only
            ([SW(1, 2, 0)], PLIC_PENDING, 7, PLIC_PENDING), # and its pending word is read-only
            ([LW(1, 2, 0)], PLIC + 0x80, 5, PLIC + 0x80),   # between the priorities and the pending word
            ([LW(1, 2, 0)], PLIC + 0x600000, 5, PLIC + 0x600000),  # past the 6 MiB window
            ([LB(1, 2, 0)], VIRTIO, 5, VIRTIO),             # virtio-blk (O3) takes words only
            ([SW(1, 2, 0)], VIRTIO, 7, VIRTIO),             # MagicValue is read-only
            ([LW(1, 2, 0x50)], VIRTIO, 5, VIRTIO + 0x50),   # QueueNotify is write-only
            ([LW(1, 2, 0)], VIRTIO + 0x200, 5, VIRTIO + 0x200),  # past its 512 bytes
            ([LW(1, 2, 0)], 0x10002000, 5, 0x10002000),     # virt's second virtio slot: unmapped here
            ([LW(1, 2, 2)], MTIME, 4, MTIME + 2),           # misalignment is decided before the window
            ([LHU(1, 2, 2)], MTIME, 5, MTIME + 2),          # an aligned halfword inside the window is refused by it
            ([LW(1, 2, 0)], 0x11003400, 5, 0x11003400),     # past the palette's 1 KiB (issue #35): unmapped
            ([SW(1, 2, 0)], 0x11003400, 7, 0x11003400),
            ([LBU(1, 2, 5)], PALETTE, 5, PALETTE + 5),      # the palette takes words only
            ([SH(1, 2, 2)], PALETTE, 7, PALETTE + 2),
            ([LW(1, 2, 0)], DISPLAY, 5, DISPLAY),           # PRESENT is write-only
            ([SW(1, 2, 4)], DISPLAY, 7, DISPLAY + 4),       # FRAMES, WIDTH, HEIGHT are read-only
            ([SW(1, 2, 12)], DISPLAY, 7, DISPLAY + 12),
            ([LHU(1, 2, 8)], DISPLAY, 5, DISPLAY + 8),      # and words
            ([SB(1, 2, 0)], DISPLAY, 7, DISPLAY),
            ([SB(1, 2, 0)], FB + FB_SIZE, 7, FB + FB_SIZE), # the byte past the last pixel
            ([LW(1, 2, 0)], FB + FB_SIZE, 5, FB + FB_SIZE),
            ([LBU(1, 2, 0)], FB - 1, 5, FB - 1),
            ([SW(1, 2, 0)], INPUT, 7, INPUT),               # the input registers are read-only
            ([SW(1, 2, 8)], INPUT, 7, INPUT + 8),
            ([LBU(1, 2, 0)], INPUT, 5, INPUT),              # and words
            ([LHU(1, 2, 4)], INPUT, 5, INPUT + 4),
            ([LW(1, 2, 12)], INPUT, 5, INPUT + 12)]         # no fourth register
        for body, base, cause, tval in cases:
            with self.subTest(body=body, base=hex(base)):
                result = self.run_trapping(LI(2, base) + LI(1, 0x5555) + body)
                self.assertEqual((result.state.x[10], result.state.x[11]), (cause, tval))
        result = self.run_trapping(LI(2, RAM + 0xFFFFFC) + [LW(1, 2, 0), LH(1, 2, 2), LBU(1, 2, 3), SW(1, 2, 0),
                                                            SH(1, 2, 2), SB(1, 2, 3), LW(1, 2, 4)])
        self.assertEqual((result.state.x[10], result.state.x[11]), (5, RAM + 0x1000000), "only the last access faults")
        self.assertEqual(result.state.retired, 2 + 1 + 2 + 6 + 3 + 5, "the six in-bounds accesses retired")
        # The last two framebuffer bytes as a halfword, then the byte past them.
        result = self.run_trapping(LI(2, FB + FB_SIZE - 2) + LI(1, 0xBEEF) + [SH(1, 2, 0), LHU(1, 2, 0), LW(1, 2, 2)])
        self.assertEqual((result.state.x[1], result.state.x[10], result.state.x[11]), (0xBEEF, 5, FB + FB_SIZE))
        # A fetch from the first word past RAM is a fetch fault, like one from below it.
        result = self.run_trapping(LI(2, RAM + 0x1000000) + [JALR(0, 2, 0)])
        self.assertEqual((result.state.x[10], result.state.x[11]), (1, RAM + 0x1000000))

    def test_double_fault_halts_with_report(self):
        result = self.run_words([ECALL()] + FINISH())
        self.assertEqual((result.status, result.state.halt), (2, "double-fault"))
        self.assertEqual((result.state.mcause, result.state.mepc, result.state.mtval), (11, RAM, 0))
        self.assertIn("unhandled-trap mcause=11 mepc=80000000 mtval=00000000 then mcause=1 mtval=00000000 at pc=00000000",
                      result.stderr)
        self.assertEqual(result.trace, ["1 80000000 00000073 trap 11 00000000", "2 00000000 00000000 trap 1 00000000"])
        self.assertEqual(result.state.steps, 2)
        self.assertEqual(result.state.retired, 0)
        # A handler whose first instruction is illegal is also a double fault.
        handler_at = RAM + 0x100
        words = LI(5, handler_at) + [CSRRW(0, MTVEC, 5), ECALL()] + FINISH()
        words += [0] * ((handler_at - RAM) // 4 - len(words)) + [0]
        result = self.run_words(words)
        self.assertEqual(result.state.halt, "double-fault")
        self.assertIn("mcause=11 mepc=8000000c mtval=00000000 then mcause=2 mtval=00000000 at pc=80000100", result.stderr)
        # A nested trap after the handler has retired an instruction is not a double fault.
        handler = [CSRRS(10, MCAUSE, 0), CSRRW(0, MTVEC, 0), EBREAK()]
        words = LI(5, handler_at) + [CSRRW(0, MTVEC, 5), ECALL()] + FINISH()
        words += [0] * ((handler_at - RAM) // 4 - len(words)) + handler
        result = self.run_words(words)
        self.assertEqual((result.state.traps, result.state.mcause, result.state.mepc), (2, 3, handler_at + 8))
        self.assertEqual(result.state.halt, "double-fault", "the second trap vectors to 0 and cannot be delivered")

    def test_supervisor_state_in_the_dump_and_a_delegated_double_fault(self):
        """Issue #20: --dump-state shows MPP = S and the S CSRs; a double fault whose first trap was
        delegated reports scause, sepc and stval, where that trap went."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "image.bin").write_bytes(b"".join(word.to_bytes(4, "little")
                                                      for word in LI(5, 0x800) + [CSRRW(0, MSTATUS, 5)] + FINISH()))
            subprocess.run([str(self.emulator), "--image", str(path / "image.bin"), "--dump-state", str(path / "state")],
                           capture_output=True, check=True)
            state = dict(line.split() for line in (path / "state").read_text().splitlines())
        self.assertEqual(int(state["mstatus"], 16) & MSTATUS_MPP, 0x800, "MPP = S")
        self.assertTrue({"medeleg", "mideleg", "satp", "stvec", "sepc", "scause", "stval"} <= state.keys())
        target = RAM + 0x100  # an illegal word, run in S mode with illegal instructions delegated
        words = LI(5, 0xFFFFFFFF) + [CSRRW(0, PMPADDR0, 5)] + LI(5, 0x1F) + [CSRRW(0, PMPCFG0, 5)]  # S may run anywhere
        words += LI(5, 1 << 2) + [CSRRW(0, MEDELEG, 5)] + LI(5, target) + [CSRRW(0, MEPC, 5)]
        words += LI(5, 0x800) + [CSRRW(0, MSTATUS, 5), MRET()]
        words += [0] * ((target - RAM) // 4 - len(words)) + [0]
        result = self.run_words(words)
        self.assertEqual(result.state.halt, "double-fault")
        self.assertIn("unhandled-trap scause=2 sepc=80000100 stval=00000000 then cause=1 tval=00000000 at pc=00000000",
                      result.stderr)

    def test_console_receive(self):
        """O2: --console-input's bytes wait from reset; LSR.DR says one is waiting, RBR takes it, and
        RBR reads 0 once they are gone."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "in.txt"
            source.write_bytes(b"AB")
            words = LI(5, CONSOLE) + [LBU(6, 5, 5), LBU(7, 5, 0), LBU(8, 5, 5), LBU(9, 5, 0), LBU(10, 5, 5), LBU(11, 5, 0)]
            x = self.run_pass(words, extra=("--console-input", str(source))).state.x
            self.assertEqual(x[6:12], [0x61, ord("A"), 0x61, ord("B"), 0x60, 0])
            x = self.run_pass(words).state.x
            self.assertEqual(x[6:12], [0x60, 0, 0x60, 0, 0x60, 0], "no input: nothing waits")
        result = self.run_words(FINISH(), extra=("--console-input", "/nonexistent/in.txt"))
        self.assertEqual(result.status, 2)
        self.assertIn("cannot open console input", result.stderr)

    def test_interactive_console_output_is_not_held_back(self):
        """With --console-input - on a terminal, each byte the guest sends appears at once: a guest
        echoing a key must not wait for the next newline in stdio's line buffer."""
        import pty
        import select
        words = LI(5, CONSOLE) + LI(7, ord("a")) + [SB(7, 5, 0)]
        words += [LBU(6, 5, 5), ANDI(6, 6, 1), BEQ(6, 0, -8)]  # wait for a received byte
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.bin"
            image.write_bytes(b"".join(w.to_bytes(4, "little") for w in words + FINISH()))
            master, slave = pty.openpty()
            process = subprocess.Popen([str(self.emulator), "--image", str(image), "--console-input", "-",
                                        "--max-instructions", "100000000"], stdin=slave, stdout=slave,
                                       stderr=subprocess.DEVNULL)
            os.close(slave)
            try:
                ready, _, _ = select.select([master], [], [], 5)
                self.assertTrue(ready, "the guest's byte was held back")
                self.assertEqual(os.read(master, 1), b"a")
                os.write(master, b"x\n")  # the pty is canonical: a line reaches the emulator
                self.assertEqual(process.wait(timeout=10), 0)
            finally:
                if process.poll() is None:
                    process.kill()
                    process.wait()
                os.close(master)

    def test_console_and_done_register(self):
        words = LI(5, CONSOLE) + [LBU(6, 5, 5)] + LI(7, ord("H")) + [SB(7, 5, 0)] + LI(7, ord("i")) + [SB(7, 5, 0)]
        words += LI(7, 0x10A) + [SB(7, 5, 0)]
        result = self.run_pass(words)
        self.assertEqual(result.stdout, "Hi\n", "only the low byte of x7 is transmitted")
        self.assertEqual(result.state.x[6], 0x60, "console always ready: THRE and TEMT")
        self.assertEqual(result.halt, {"halt": "done", "steps": result.state.steps, "retired": result.state.steps,
                                       "traps": 0, "loaded": 4 * (len(words) + 5), "done": 0x5555, "outcome": "pass"})
        result = self.run_words(FINISH((7 << 16) | 0x3333))
        self.assertEqual((result.status, result.state.done, result.halt["outcome"]), (7, 0x00073333, "fail=7"))
        result = self.run_words(FINISH((255 << 16) | 0x3333))
        self.assertEqual((result.status, result.halt["outcome"]), (255, "fail=255"))
        result = self.run_words(FINISH(0x7777))
        self.assertEqual((result.status, result.halt["outcome"]), (2, "error=reserved-reset-word"))
        for word in (0x3333, 0x00013334, 0x01005555, 0x01003333 | (256 << 16)):
            with self.subTest(word=hex(word)):
                result = self.run_words(FINISH(word))
                self.assertEqual((result.status, result.halt["outcome"]), (2, "error=undefined-done-word"))
        self.assertEqual(result.trace[-1].split()[-1], f"mem[00100000]<-{word:08x}/4", "the done store retires")

    def test_console_16550_registers(self):
        """Issue #36: the 16550 subset Linux's 8250 driver drives (console_16550_program)."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "in.txt"
            source.write_bytes(b"Z")
            words, expected = console_16550_program()
            result = self.run_pass(words, extra=("--console-input", str(source)))
        self.assertEqual(result.stdout, "", "the divisor latch transmits nothing")
        self.assertEqual({r: result.state.x[r] for r in expected}, expected)
        self.assertNotIn("warning", result.stderr, "DLAB was cleared again, as a driver setting the divisor does")
        muted = LI(5, CONSOLE) + LI(7, 0x80) + [SB(7, 5, 3), SB(7, 5, 3)] + LI(7, ord("x")) + [SB(7, 5, 0), SB(7, 5, 0)]
        result = self.run_pass(muted)
        self.assertEqual(result.stdout, "")
        self.assertIn("warning: the console ended with LCR.DLAB set; 2 byte(s) since went to the divisor latch",
                      result.stderr, "a run left muted is reported, counting from when DLAB was set")

    def test_console_prompt_gates_input_lines(self):
        """Issue #36: --console-prompt releases one input line per prompt the guest sends; it needs
        a --console-input file and a prompt of 1 to 64 bytes."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "in.txt"
            source.write_bytes(b"a\nb\n")
            words, expected = console_prompt_program()
            result = self.run_pass(words, extra=("--console-input", str(source), "--console-prompt", "> "))
            self.assertEqual(result.stdout, "> > ")
            self.assertEqual({r: result.state.x[r] for r in expected}, expected)
            x = self.run_pass(words, extra=("--console-input", str(source))).state.x
            self.assertEqual((x[6], x[9], x[10]), (0x61, ord("a"), 0x0A), "without a prompt every byte waits from reset")
            for extra, message in (((("--console-prompt", "> ")), "gates a --console-input file"),
                                   (("--console-input", "-", "--console-prompt", "> "), "gates a --console-input file"),
                                   (("--console-input", str(source), "--console-prompt", "x" * 65), "1 to 64 bytes")):
                with self.subTest(extra=extra[-1][:8]):
                    result = self.run_words(FINISH(), extra=extra)
                    self.assertEqual(result.status, 2)
                    self.assertIn(message, result.stderr)

    def test_console_prompt_edges(self):
        """Issue #36: overlapping prompts, a last line without a newline, a prompt byte under DLAB and
        prompts beyond the input (console_prompt_edges_program); an empty line is a line; a prompt is
        1 to 64 bytes; and a run that ends with input unrevealed says so."""
        with tempfile.TemporaryDirectory() as directory:
            source = Path(directory) / "in.txt"
            source.write_bytes(b"x\ny\nz")
            words, expected = console_prompt_edges_program()
            result = self.run_pass(words, extra=("--console-input", str(source), "--console-prompt", "aa"))
            self.assertEqual({r: result.state.x[r] for r in expected}, expected)
            self.assertNotIn("console input:", result.stderr, "every byte was taken")
            source.write_bytes(b"\nq\n")
            reads = LI(5, CONSOLE) + LI(7, ord(">")) + [SB(7, 5, 0), LBU(6, 5, 0), LBU(8, 5, 5)]
            result = self.run_pass(reads, extra=("--console-input", str(source), "--console-prompt", ">"))
            self.assertEqual((result.state.x[6], result.state.x[8]), (0x0A, 0x60), "an empty line is one line")
            self.assertIn("console input: 1 of 3 byte(s) taken; the prompt was sent 1 time(s), 1 line(s) taken",
                          result.stderr, "input left unrevealed is reported")
            result = self.run_pass([], extra=("--console-input", str(source), "--console-prompt", "y" * 64))
            self.assertEqual(result.status, 0, "64 bytes is the longest prompt")
            result = self.run_words(FINISH(), extra=("--console-input", str(source), "--console-prompt", ""))
            self.assertEqual(result.status, 2)
            self.assertIn("1 to 64 bytes", result.stderr)

    def test_machine_information_csrs_fence_i_and_done_read(self):
        """Issue #36 (machine_information_program); and a write to a read-only ID register traps."""
        words, expected = machine_information_program()
        x = self.run_pass(words).state.x
        self.assertEqual({r: x[r] for r in expected}, expected)
        result = self.run_trapping([CSRRW(0, 0xF14, 1)])
        self.assertEqual((result.state.x[10], result.state.x[11]), (2, CSRRW(0, 0xF14, 1)))

    def test_timer_counts_executed_instructions(self):
        """Device time (docs/rv32.md): a load inside instruction N reads N - 1, trapped instructions
        count, and a write loads the count so a read n instructions later returns value + n."""
        result = self.run_pass(LI(1, MTIME) + [LW(2, 1, 0), LW(3, 1, 0)])
        self.assertEqual((result.state.x[2], result.state.x[3]), (2, 3))
        self.assertEqual(effects(result.trace[2]), f"x2=00000002 mem[{MTIME:08x}]->00000002/4")
        # The store is instruction 5; instruction 6 reads value + 1 and instruction 7 wraps to zero.
        result = self.run_pass(LI(1, MTIME) + LI(3, 0xFFFFFFFE) + [SW(3, 1, 0), LW(2, 1, 0), LW(4, 1, 0)])
        self.assertEqual((result.state.x[2], result.state.x[4]), (0xFFFFFFFF, 0))
        # A trapped instruction is executed: lui, addi, csrrw, then the ecall is instruction 4; the
        # handler's lui and addi are 5 and 6, so its lw is instruction 7 and reads 6.
        handler_at = RAM + 0x200
        words = LI(5, handler_at) + [CSRRW(0, MTVEC, 5), ECALL()] + FINISH(0x00013333)
        words += [0] * ((handler_at - RAM) // 4 - len(words)) + LI(1, MTIME) + [LW(2, 1, 0)] + FINISH()
        result = self.run_words(words)
        self.assertEqual((result.state.halt, result.state.traps, result.state.x[2]), ("done", 1, 6))

    def test_clint_registers_and_time_csr(self):
        """Track 1: mtime is 64-bit and writable half by half, `time` reads mtime (so it follows a
        write), mtimecmp resets to all ones and keeps its two halves apart, and msip keeps bit 0 of
        what is written (all ones reads 1, zero clears it)."""
        words = LI(1, MSIP) + LI(2, MTIME) + LI(3, MTIMECMP)
        words += [LW(4, 3, 0), LW(5, 3, 4)]                        # mtimecmp at reset
        words += LI(6, 7) + [SW(6, 2, 4), SW(0, 2, 0)]             # mtime = 7 << 32, low word 0
        words += [RDTIMEH(7), RDTIME(8), LW(9, 2, 4)]
        words += LI(6, 0x89ABCDEF) + LI(13, 0x01234567) + [SW(6, 3, 0), SW(13, 3, 4), LW(10, 3, 0), LW(11, 3, 4)]
        words += LI(6, 0xFFFFFFFF) + [SW(6, 1, 0), LW(12, 1, 0), SW(0, 1, 0), LW(14, 1, 0)]
        x = self.run_pass(words).state.x
        self.assertEqual((x[4], x[5]), (0xFFFFFFFF, 0xFFFFFFFF))
        self.assertEqual((x[7], x[9]), (7, 7), "timeh and mtime's high word follow the write")
        self.assertEqual(x[8], 2, "time reads the count two instructions after the low word was set to 0")
        self.assertEqual((x[10], x[11]), (0x89ABCDEF, 0x01234567), "each mtimecmp half keeps its own value")
        self.assertEqual((x[12], x[14]), (1, 0), "msip holds bit 0: all ones reads 1, zero clears it")

    def test_mtime_carries_into_the_high_word(self):
        """Review on PR #18: the low word carries into the high word (and `timeh`), a write to the
        high word after the low word has wrapped keeps the wrapped low word, and `cycle` is the
        core's own count, untouched by writes to mtime."""
        words = [RDCYCLE(20), RDCYCLEH(21)] + LI(2, MTIME) + LI(6, 0xFFFFFFFE)
        words += [SW(0, 2, 4), SW(6, 2, 0)]                          # store N: count N = 0x0_FFFFFFFE
        words += [ADDI(0, 0, 0)] * 3 + [LW(7, 2, 4), RDTIMEH(8), LW(9, 2, 0)]   # N+4, N+5, N+6
        words += LI(6, 0xFFFFFFFF) + [SW(6, 2, 0)] + LI(13, 5) + [SW(13, 2, 4)]  # low wraps before the high write
        words += [LW(14, 2, 4), RDTIMEH(15), LW(16, 2, 0), RDCYCLE(22), RDCYCLEH(23)]
        x = self.run_pass(words).state.x
        self.assertEqual((x[7], x[8], x[9]), (1, 1, 4), "0xFFFFFFFE + 4 carries; + 6 reads low word 4")
        # The low store makes its own count 0xFFFFFFFF; LI is two instructions, so the high store sees
        # 0x1_00000002 and makes it 0x5_00000002; the low word is read three instructions later.
        self.assertEqual((x[14], x[15], x[16]), (5, 5, 5), "the high write replaced the carried 1, kept the low word")
        self.assertEqual((x[20], x[21], x[23]), (0, 0, 0))
        self.assertEqual(x[22], words.index(RDCYCLE(22)), "cycle counts every instruction before it, whatever mtime was set to")

    def test_boot_convention_and_rom(self):
        """Track 1: a0 holds hart id 0 and a1 the boot ROM, whose first bytes are an FDT header
        (magic d00dfeed, big-endian) of the size tools/rv32_dtb.py builds; every width reads it."""
        from tools import rv32_dtb
        blob = rv32_dtb.build(rv32_dtb.MACHINE)
        words = [ADDI(20, 10, 0), ADDI(21, 11, 0), LW(22, 11, 0), LBU(23, 11, 0), LHU(24, 11, 6), LW(25, 11, 4)]
        x = self.run_pass(words).state.x
        self.assertEqual((x[20], x[21]), (0, BOOTROM))
        self.assertEqual((x[22], x[23]), (0xEDFE0DD0, 0xD0))
        self.assertEqual(x[24], int.from_bytes(blob[6:8], "little"))
        self.assertEqual(int.from_bytes(x[25].to_bytes(4, "little"), "big"), len(blob), "totalsize")

    def test_display_and_framebuffer(self):
        """Pixels are ordinary memory at every width; a present hashes them into a checkpoint line
        computed here independently; FRAMES counts presents; the state file reports them."""
        last = FB_SIZE - 4
        words = LI(1, DISPLAY) + [LW(2, 1, 8), LW(3, 1, 12)] + LI(5, FB) + LI(6, 0x11223344)
        words += [SW(6, 5, 0), SB(6, 5, 4), SH(6, 5, 6)] + LI(7, FB + last) + [SW(6, 7, 0), LW(8, 5, 4), LBU(9, 7, 3)]
        words += [SW(0, 1, 0), LW(11, 1, 4), SB(6, 5, 8), SW(6, 1, 0), LW(12, 1, 4)]
        result = self.run_pass(words, checkpoints=True)
        self.assertEqual((result.state.x[2], result.state.x[3], result.state.x[8], result.state.x[9]),
                         (320, 240, 0x33440044, 0x11))
        self.assertEqual((result.state.x[11], result.state.x[12], result.state.frames), (1, 2, 2))
        pixels = bytearray(FB_SIZE)
        pixels[0:4] = (0x11223344).to_bytes(4, "little")
        pixels[4] = 0x44
        pixels[6:8] = (0x3344).to_bytes(2, "little")
        pixels[last:last + 4] = (0x11223344).to_bytes(4, "little")
        first = frame_hash(pixels)
        pixels[8] = 0x44
        self.assertEqual(result.checkpoints, [f"frame 1 {first:08x}", f"frame 2 {frame_hash(pixels):08x}"])
        self.assertEqual(effects(result.trace[16]), f"mem[{DISPLAY:08x}]<-00000000/4", "the present store retires")
        self.assertEqual(result.halt["outcome"], "pass", "the halt line does not change")
        # Without --checkpoints nothing is written and the run is the same; a fresh frame is all zero.
        result = self.run_pass(LI(1, DISPLAY) + [SW(0, 1, 0)])
        self.assertEqual((result.state.frames, result.checkpoints), (1, []))
        result = self.run_pass(LI(1, DISPLAY) + [SW(0, 1, 0)], checkpoints=True)
        self.assertEqual(result.checkpoints, [f"frame 1 {frame_hash(bytes(FB_SIZE)):08x}"])

    def test_input_events_arrive_at_frames(self):
        """Frame 0's events are queued before the first instruction, later frames' when their
        present retires; EVENT pops, COUNT counts, KEYS follows arrivals (a press and release that
        arrive together leave the bit clear); a full queue drops and reports."""
        script = "frame 0 down LEFT\nframe 0 up left\nframe 1 down A\nframe 2 up 8\n"
        words = LI(1, INPUT) + LI(2, DISPLAY) + [LW(3, 1, 4), LW(4, 1, 8), LW(5, 1, 0), LW(6, 1, 0), LW(7, 1, 0), LW(8, 1, 4)]
        words += [SW(0, 2, 0), LW(9, 1, 4), LW(10, 1, 8), LW(11, 1, 0), SW(0, 2, 0), LW(12, 1, 0), LW(13, 1, 8), LW(14, 1, 0)]
        result = self.run_pass(words, input_script=script)
        x = result.state.x
        self.assertEqual(x[3:9], [2, 0, event_word(True, 1), event_word(False, 1), 0, 0])
        self.assertEqual(x[9:12], [1, 0x100, event_word(True, 8)])
        self.assertEqual(x[12:15], [event_word(False, 8), 0, 0])
        self.assertEqual((result.state.frames, result.state.events), (2, 0))
        self.assertEqual(effects(result.trace[6]), f"x5={event_word(True, 1):08x} mem[{INPUT:08x}]->{event_word(True, 1):08x}/4")
        # Sixteen events fill the queue; the seventeenth is dropped and reported; KEYS shows arrivals only.
        burst = "".join(f"frame 0 down {code}\n" for code in range(17))
        words = LI(1, INPUT) + [LW(3, 1, 4), LW(4, 1, 8)]
        result = self.run_pass(words, input_script=burst, allow_lost_events=True)
        self.assertEqual((x := result.state.x)[3], 16)
        self.assertEqual(x[4], 0xFFFF)
        self.assertEqual(result.state.events, 16)
        self.assertIn(f"rv32emu: input queue full: dropped frame 0 event {event_word(True, 16):08x}", result.stderr)
        # Without the flag the same run is rejected after its halt line: the guest passed, the host did not.
        result = self.run_words(words + FINISH(), input_script=burst)
        self.assertEqual((result.status, result.state.done, result.halt["outcome"]), (2, 0x5555, "pass"))
        self.assertIn("rv32emu: 1 input event(s) lost, run rejected", result.stderr)
        # Popping makes room; an event for a frame never presented stays with the host.
        result = self.run_pass(LI(1, INPUT) + [LW(3, 1, 0), LW(4, 1, 4)], input_script=burst + "frame 5 up 3\n",
                               allow_lost_events=True)
        self.assertEqual((result.state.x[3], result.state.x[4], result.state.events), (event_word(True, 0), 15, 15))
        self.assertIn("rv32emu: 1 scripted event(s) never delivered (first: frame 5)", result.stderr)
        result = self.run_words(LI(1, INPUT) + [LW(3, 1, 0)] + FINISH(), input_script="frame 5 up 3\n")
        self.assertEqual(result.status, 2)
        self.assertIn("rv32emu: 1 input event(s) lost, run rejected", result.stderr)
        # The ring wraps: twelve popped, ten more pushed at frame 1 past entry 15 and popped in order;
        # key 31 is the top bit of KEYS.
        script = "".join(f"frame 0 down {code}\n" for code in range(12))
        script += "".join(f"frame 1 down {code}\n" for code in range(22, 32))
        words = LI(1, INPUT) + [LW(2, 1, 0)] * 12 + LI(3, DISPLAY) + [SW(0, 3, 0)] + [LW(2, 1, 0)] * 10 + [LW(4, 1, 4), LW(5, 1, 8)]
        result = self.run_pass(words, input_script=script)
        pops = [effects(line) for line in result.trace if f"mem[{INPUT:08x}]->" in line]
        self.assertEqual(pops, [f"x2={event_word(True, code):08x} mem[{INPUT:08x}]->{event_word(True, code):08x}/4"
                                for code in list(range(12)) + list(range(22, 32))])
        self.assertEqual((result.state.x[4], result.state.x[5], result.state.events), (0, 0xFFC00FFF, 0))

    def test_record_writes_every_offered_event_as_a_replayable_script(self):
        """--record lists every event the host offered, scripted or not, at the frame it was offered,
        before the queue decides: a replay of the record reproduces the run, drops included."""
        script = "frame 0 down LEFT\nframe 0 up left\nframe 1 down A\nframe 2 up 8\nframe 2 down 30\n"
        words = LI(1, INPUT) + LI(2, DISPLAY) + [LW(3, 1, 0), LW(3, 1, 0), SW(0, 2, 0), LW(3, 1, 0), SW(0, 2, 0), LW(3, 1, 0), LW(3, 1, 0)]
        with tempfile.TemporaryDirectory() as directory:
            record = Path(directory) / "record.txt"
            result = self.run_pass(words, input_script=script, extra=["--record", str(record)], checkpoints=True)
            self.assertEqual(record.read_text(), "frame 0 down LEFT\nframe 0 up LEFT\nframe 1 down A\nframe 2 up A\nframe 2 down 30\n")
            replay = self.run_pass(words, input_script=record.read_text(), checkpoints=True)
            self.assertEqual((replay.trace, replay.checkpoints), (result.trace, result.checkpoints))
            # A burst the queue cannot hold is recorded whole, so the replay drops the same event.
            burst = "".join(f"frame 0 down {code}\n" for code in range(17))
            result = self.run_pass(LI(1, INPUT) + [LW(3, 1, 4)], input_script=burst, allow_lost_events=True,
                                   extra=["--record", str(record)])
            names = {code: name for name, code in KEYS.items()}
            self.assertEqual(record.read_text(), "".join(f"frame 0 down {names.get(code, code)}\n" for code in range(17)))
            self.assertIn("dropped frame 0 event", result.stderr)
            replay = self.run_words(LI(1, INPUT) + [LW(3, 1, 4)] + FINISH(), input_script=record.read_text())
            self.assertEqual((replay.status, replay.state.x[3]), (2, 16))
            self.assertIn("1 input event(s) lost", replay.stderr)
            # An unwritable record is refused before the run.
            completed = subprocess.run([str(self.emulator), "--image", str(self.image_path(words)), "--record", directory],
                                       capture_output=True, text=True)
            self.assertEqual(completed.returncode, 2)
            self.assertIn(f"rv32emu: cannot write {directory}", completed.stderr)

    def image_path(self, words):
        """A flat image of `words` in the class workdir, for tests that need a stable path."""
        path = Path(self.workdir.name) / "image.bin"
        path.write_bytes(b"".join(word.to_bytes(4, "little") for word in list(words) + FINISH()))
        return path

    def test_input_script_errors(self):
        for content in ("frame 1 down\n", "frame x down A\n", "frame 1 press A\n", "frame 2 down A\nframe 1 up A\n",
                        "frame 1 down NOPE\n", "frame 1 down 32\n", "key 1 down A\n", "frame 1 down A extra\n",
                        "frame 1 down A " + "x" * 300 + "\n",
                        "frame 0000000001 down A\n", "frame 0 down 0000000001\n"):  # a number is at most nine digits
            with self.subTest(script=content):
                result = self.run_words(FINISH(), input_script=content)
                self.assertEqual(result.status, 2)
                self.assertIsNone(result.halt, "the run never starts")
                self.assertIn("input script", result.stderr)
        result = self.run_pass([], input_script="# only a comment\n\n  frame 0 down Left  \n")
        self.assertEqual(result.state.events, 1)
        result = self.run_pass([], input_script="frame 0 down A\r\nframe 000000000 up 000000008\r\n")
        self.assertEqual(result.state.events, 2, "CRLF lines and nine-digit numbers are accepted")
        result = self.run_words(FINISH(), extra=("--input", "/nonexistent/input.txt"))
        self.assertEqual((result.status, result.halt), (2, None))
        self.assertIn("cannot open input script", result.stderr)
        # A directory opens but does not read: that must not become an empty script.
        result = self.run_words(FINISH(), extra=("--input", str(ROOT / "tools")))
        self.assertEqual((result.status, result.halt), (2, None))
        self.assertIn("cannot read input script", result.stderr)

    def test_pong_image_when_built(self):
        """The Pong session replays to the checkpoints the native build produced (pong.expected) and
        ends with the PASS word the Makefile pins; the frame count and the halt are as scripted."""
        image = ROOT / "build/rv32/pong.bin"
        if not image.exists():
            self.skipTest("build/rv32/pong.bin is not built (make check-rv32-image)")
        expected = [line for line in PONG_EXPECTED.read_text().splitlines() if line and not line.startswith("#")]
        pinned = re.search(r"^RV32_PONG_HEX := ([0-9a-f]{8})$", (ROOT / "Makefile").read_text(), re.M).group(1)
        data = image.read_bytes()
        words = [int.from_bytes(data[i:i + 4], "little") for i in range(0, len(data), 4)]
        result = self.run_words(words, limit=10_000_000, checkpoints=True, input_script=PONG_INPUT.read_text())
        self.assertEqual((result.status, result.halt["outcome"]), (0, "pass"), result.stderr)
        self.assertEqual(result.checkpoints, expected)
        self.assertEqual((result.stdout, result.state.frames, result.state.traps), (f"PASS {pinned}\n", 200, 0))
        self.assertFalse(any(f"mem[{MTIME:08x}]" in line for line in result.trace), "Pong never reads the timer")
        # A Q one frame later is popped one iteration later, so the guest presents once more first.
        later = PONG_INPUT.read_text().replace("frame 200 down Q", "frame 201 down Q")
        result = self.run_words(words, limit=10_000_000, checkpoints=True, input_script=later)
        self.assertEqual((result.status, len(result.checkpoints)), (0, 201))

    def test_palette(self):
        """Issue #35: the palette's power-on words are RGB332's (hashed here over all 256); a word
        write keeps its low 24 bits and the next frame is drawn through it."""
        words = LI(1, PALETTE) + LI(10, 5381)
        for i in range(256):
            words += [LW(11, 1, 4 * i), SLLI(12, 10, 5), ADD(10, 10, 12), XOR(10, 10, 11)]
        words += LI(4, 0xAB123456) + [SW(4, 1, 4 * 5), LW(5, 1, 4 * 5), LW(6, 1, 4 * 6)]
        words += LI(7, FB) + LI(8, 5) + [SB(8, 7, 1)] + LI(9, DISPLAY) + [SW(0, 9, 0)]
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_pass(words, extra=("--frames", directory))
            data = (Path(directory) / "frame-0001.ppm").read_bytes()
        x = result.state.x
        self.assertEqual(x[10], word_hash(power_on_palette()))
        self.assertEqual((x[5], x[6]), (0x123456, power_on_palette()[6]), "the top byte reads as zero")
        header = len(b"P6\n320 240\n255\n")
        self.assertEqual(data[header:header + 6], b"\x00\x00\x00\x12\x34\x56", "pixel 1 is value 5, drawn in the new colour")

    def test_each_frame_is_coloured_by_its_own_palette(self):
        """Issue #35: a frame's picture is taken at its present, palette included; a change after
        the first present colours only the second."""
        words = LI(7, FB) + LI(8, 9) + [SB(8, 7, 0)] + LI(9, DISPLAY) + [SW(0, 9, 0)]
        words += LI(1, PALETTE) + LI(4, 0x00ABCDEF) + [SW(4, 1, 4 * 9), SW(0, 9, 0)]
        with tempfile.TemporaryDirectory() as directory:
            self.run_pass(words, extra=("--frames", directory))
            first, second = ((Path(directory) / f"frame-000{n}.ppm").read_bytes() for n in (1, 2))
        header = len(b"P6\n320 240\n255\n")
        before = power_on_palette()[9]
        self.assertEqual(first[header:header + 3], before.to_bytes(3, "big"))
        self.assertEqual(second[header:header + 3], b"\xab\xcd\xef")
        self.assertEqual(first[header + 3:], second[header + 3:], "every other pixel is value 0 in both")

    def test_frames_are_written_as_ppm(self):
        """--frames DIR writes one binary PPM per present with the RGB332 mapping; an unwritable
        directory rejects the run."""
        words = LI(1, DISPLAY) + LI(5, FB) + LI(6, 0xE3) + [SB(6, 5, 321), SW(0, 1, 0)]
        with tempfile.TemporaryDirectory() as directory:
            result = self.run_pass(words, extra=("--frames", directory))
            files = sorted(Path(directory).iterdir())
            self.assertEqual([f.name for f in files], ["frame-0001.ppm"])
            data = files[0].read_bytes()
            header = b"P6\n320 240\n255\n"
            self.assertTrue(data.startswith(header))
            self.assertEqual(len(data), len(header) + 3 * FB_SIZE)
            self.assertEqual(data[len(header):len(header) + 3], b"\x00\x00\x00")
            self.assertEqual(data[len(header) + 3 * 321:len(header) + 3 * 322], b"\xff\x00\xff", "0xE3: red 7, green 0, blue 3")
        result = self.run_words(words + FINISH(), extra=("--frames", "/nonexistent/dir"))
        self.assertEqual((result.status, result.state.frames), (2, 1))
        self.assertIn("cannot write frame 1", result.stderr)
        self.assertIn("outputs incomplete", result.stderr)
        # A frame file that already exists is never overwritten, whatever it is: a stale frame, or a
        # link to another file of the run, which no path check can see.
        with tempfile.TemporaryDirectory() as directory:
            stale = Path(directory) / "frame-0001.ppm"
            stale.write_text("stale")
            result = self.run_words(words + FINISH(), extra=("--frames", directory))
            self.assertEqual((result.status, result.state.frames), (2, 1))
            self.assertIn("never overwritten", result.stderr)
            self.assertEqual(stale.read_text(), "stale")
            stale.unlink()
            (Path(directory) / "frames").mkdir()
            record = Path(directory) / "record"  # outside the frames directory, so the path check passes
            (Path(directory) / "frames" / "frame-0001.ppm").symlink_to(record)
            result = self.run_words(words + FINISH(), extra=("--frames", str(Path(directory) / "frames"), "--record", str(record)),
                                    input_script="frame 0 down A\n")
            self.assertEqual(result.status, 2)
            self.assertIn("never overwritten", result.stderr)
            self.assertEqual(record.read_text(), "frame 0 down A\n", "the recording survived")

    def test_instruction_limit_and_counts(self):
        result = self.run_words([JAL(0, 0)], limit=50)
        self.assertEqual((result.status, result.state.halt, result.state.steps), (2, "limit", 50))
        self.assertEqual(result.halt["outcome"], "error=instruction-limit pc=80000000")
        result = self.run_pass([ADDI(1, 0, 1), ADDI(2, 1, 1)])
        self.assertEqual((result.state.steps, result.state.retired, result.state.traps), (2 + 5, 7, 0))
        self.assertEqual(result.trace[:2], ["1 80000000 00100093 x1=00000001", "2 80000004 00108113 x2=00000002"])

    def test_diag_image_when_built(self):
        """The M5 diagnostic on the emulator: every section passes, the checkpoints are the Python
        render's hashes, and the guest's readback hash equals the frame-1 checkpoint."""
        bin_path = ROOT / "build" / "rv32" / "diag.bin"
        if not bin_path.exists():
            self.skipTest("run `make check-rv32-image` first")
        script = (ROOT / "programs" / "rv32" / "diag.input").read_text()
        words = [int.from_bytes(bin_path.read_bytes()[i:i + 4], "little") for i in range(0, len(bin_path.read_bytes()), 4)]
        result = self.run_words(words, limit=10000000, checkpoints=True, input_script=script)
        self.assertEqual((result.status, result.state.halt, result.state.done), (0, "done", 0x5555), result.stderr)
        frame1, frame2 = (f"frame {n} {frame_hash(render_diag_frame(n)):08x}" for n in (1, 2))
        self.assertEqual(result.checkpoints, [frame1, frame2])
        self.assertEqual(result.stdout.splitlines(),
                         ["diag: timer ok", "diag: faults 4", f"diag: display {frame1.split()[2]}", "diag: input 4",
                          f"PASS {diag_checksum():08x}"])
        self.assertEqual((result.state.traps, result.state.frames, result.state.events), (4, 2, 0))
        # The diagnostic detects a deviation: without the SPACE press, CHECK 16 fails and the done
        # word carries 16; with the two frame-1 presses swapped every CHECK passes but the checksum
        # over the popped events does not, which is the 99 of the final guard.
        for altered, code in ((script.replace("frame 1 down SPACE\n", ""), 16),
                              (script.replace("frame 1 down LEFT\nframe 1 down SPACE\n", "frame 1 down SPACE\nframe 1 down LEFT\n"), 99)):
            with self.subTest(code=code):
                result = self.run_words(words, limit=10000000, input_script=altered)
                self.assertEqual((result.status, result.state.done, result.halt["outcome"]), (code, (code << 16) | 0x3333, f"fail={code}"))
                self.assertEqual(result.stdout.splitlines()[-1], f"FAIL {code}")
                self.assertNotIn("run rejected", result.stderr, "a failed guest keeps its own code")
                if code == 16:
                    self.assertIn("never delivered", result.stderr, "a guest that stops early leaves later events unread")

    def test_empty_or_unreadable_image_is_refused(self):
        """An empty image, or a directory, would run as an illegal instruction at the reset PC and be
        reported as the guest's double fault; both are refused before the run instead."""
        result = self.run_words([])
        self.assertEqual((result.status, result.halt, result.state), (2, None, None))
        self.assertIn("is empty", result.stderr)
        completed = subprocess.run([str(self.emulator), "--image", str(ROOT / "tools")], capture_output=True, text=True)
        self.assertEqual(completed.returncode, 2)
        self.assertIn("cannot read", completed.stderr)
        self.assertNotIn("halt=", completed.stderr)

    def test_loading_and_start_pc(self):
        words = [ADDI(1, 0, 1)] + FINISH()
        result = self.run_words(words, base=RAM + 0x3000, extra=["--pc", f"{RAM + 0x3000:#x}"])
        self.assertEqual((result.status, result.state.x[1]), (0, 1))
        self.assertEqual(result.trace[0], f"1 {RAM + 0x3000:08x} 00100093 x1=00000001")
        result = self.run_words([ADDI(1, 0, 1)] + FINISH(), base=RAM + 0x3000)
        self.assertEqual(result.state.halt, "double-fault", "starting at the base with nothing there faults")
        self.assertEqual((result.state.mcause, result.state.mtval), (2, 0), "RAM outside the image reads as zero")

    def test_selfcheck_image_when_built(self):
        image = ROOT / "build" / "rv32" / "selfcheck.bin"
        if not image.exists():
            self.skipTest("run `make check-rv32-image` first")
        with tempfile.TemporaryDirectory() as directory:
            state_path = Path(directory) / "state"
            completed = subprocess.run([str(self.emulator), "--image", str(image), "--dump-state", str(state_path)],
                                       capture_output=True, text=True)
            state = parse_state(state_path.read_text())
        self.assertEqual((completed.returncode, completed.stdout), (0, "PASS 807d9fad\n"))
        self.assertEqual((state.halt, state.done, state.traps), ("done", 0x5555, 0))
        self.assertEqual(state.x[2], 0x80040000, "sp is back at _stack_top when main returns")

    # Host-side robustness: outputs, limits, and the drivers around the emulator.

    def test_outputs_may_not_overwrite_inputs_or_each_other(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            image = path / "image.bin"
            image.write_bytes(b"".join(word.to_bytes(4, "little") for word in FINISH()))
            original = image.read_bytes()
            (path / "s").write_text("frame 0 down A\n")  # a script an output must not truncate
            (path / "real").mkdir()
            (path / "link").symlink_to(path / "real")
            (path / "rl").symlink_to(path / "target")  # two dangling links to one missing file
            (path / "cl").symlink_to(path / "target")
            for extra, message in [(["--trace", str(image)], "trace file"),
                                   (["--dump-state", str(image)], "state file"),
                                   (["--trace", str(path / "t"), "--dump-state", str(path / "t")], "state file"),
                                   (["--trace", str(path / "t"), "--dump-state", str(path / "./t")], "state file"),
                                   (["--checkpoints", str(image)], "checkpoints file"),
                                   (["--input", str(path / "s"), "--trace", str(path / "s")], "trace file"),
                                   (["--trace", str(path / "t"), "--checkpoints", str(path / "t")], "checkpoints file"),
                                   (["--checkpoints", str(path / "c"), "--dump-state", str(path / "c")], "state file"),
                                   (["--record", str(image)], "record file"),
                                   (["--input", str(path / "s"), "--record", str(path / "s")], "record file"),
                                   (["--record", str(path / "r"), "--trace", str(path / "r")], "trace file"),
                                   (["--record", str(path / "r"), "--checkpoints", str(path / "r")], "checkpoints file"),
                                   (["--record", str(path / "r"), "--dump-state", str(path / "r")], "state file"),
                                   (["--frames", str(image)], "frames directory"),
                                   (["--input", str(path / "s"), "--frames", str(path / "s")], "frames directory"),
                                   # Two spellings of one file that does not exist yet (pathlib would fold `./` away,
                                   # so the strings are built by hand), and a symlinked directory.
                                   (["--record", f"{path}/out", "--checkpoints", f"{path}/./out"], "checkpoints file"),
                                   (["--trace", f"{path}//out", "--record", f"{path}/out"], "trace file"),
                                   (["--record", f"{path}/link/out", "--checkpoints", f"{path}/real/out"], "checkpoints file"),
                                   (["--input", f"{path}/./s", "--trace", str(path / "s")], "trace file"),
                                   # Inside the frames directory a present could overwrite the file, whatever its name.
                                   (["--frames", str(path / "real"), "--record", str(path / "real/frame-0001.ppm")], "record file"),
                                   (["--frames", str(path / "real"), "--checkpoints", f"{path}/link/c"], "checkpoints file"),
                                   (["--frames", str(path / "real"), "--input", str(path / "real/s")], "input script"),
                                   # Outputs are real files: a symbolic link, dangling or not, is refused before it is followed.
                                   (["--record", str(path / "rl"), "--checkpoints", str(path / "cl")], "record file"),
                                   (["--trace", str(path / "rl")], "trace file"),
                                   (["--dump-state", str(path / "rl")], "state file")]:
                with self.subTest(extra=extra):
                    completed = subprocess.run([str(self.emulator), "--image", str(image), *extra],
                                               capture_output=True, text=True)
                    self.assertEqual(completed.returncode, 2)
                    self.assertIn(f"{message} ", completed.stderr)
                    self.assertRegex(completed.stderr, "would overwrite|where a frame could overwrite|is a symbolic link")
                    self.assertEqual(image.read_bytes(), original, "the image is never touched")
                    self.assertEqual(sorted(p.name for p in path.iterdir()), ["cl", "image.bin", "link", "real", "rl", "s"], "nothing was created")

    def test_trace_write_failure_rejects_the_run(self):
        words = [ADDI(1, 1, 1)] * 400 + FINISH()

        def limit_file_size():
            resource.setrlimit(resource.RLIMIT_FSIZE, (4096, 4096))
            signal.signal(signal.SIGXFSZ, signal.SIG_IGN)

        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "image.bin").write_bytes(b"".join(word.to_bytes(4, "little") for word in words))
            completed = subprocess.run([str(self.emulator), "--image", str(path / "image.bin"),
                                        "--trace", str(path / "trace")], capture_output=True, text=True,
                                       preexec_fn=limit_file_size)
            lines = (path / "trace").read_text().splitlines()
        self.assertLess(len(lines), len(words), "the trace really was truncated")
        self.assertEqual(completed.returncode, 2)
        self.assertIn("error writing", completed.stderr)
        self.assertIn("outputs incomplete", completed.stderr)
        self.assertIn("halt=done", completed.stderr, "the guest run itself is still reported")

    def test_instruction_limit_is_validated(self):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.bin"
            image.write_bytes(JAL(0, 0).to_bytes(4, "little"))
            for text in ("-1", "+5", " -1", "\t-1", " 5", "10junk", "", "18446744073709551616", "0x", "1 "):
                with self.subTest(limit=text):
                    completed = subprocess.run([str(self.emulator), "--image", str(image), "--max-instructions", text],
                                               capture_output=True, text=True, timeout=10)
                    self.assertEqual(completed.returncode, 2)
                    self.assertIn("bad instruction limit", completed.stderr)
            completed = subprocess.run([str(self.emulator), "--image", str(image), "--max-instructions", "0x10"],
                                       capture_output=True, text=True, timeout=10)
            self.assertIn("halt=limit steps=16 ", completed.stderr)
            for extra in (["--base", " 0x80000000"], ["--pc", "-0x80000000"]):
                with self.subTest(extra=extra):
                    completed = subprocess.run([str(self.emulator), "--image", str(image), *extra],
                                               capture_output=True, text=True, timeout=10)
                    self.assertEqual(completed.returncode, 2)
                    self.assertIn("rv32emu: bad", completed.stderr)

    def test_disk_size_is_the_files(self):
        """Issue #35: --disk takes whole 512-byte sectors up to VIRTIO_DISK_MAX, and the capacity the
        device reports is the file's; without a disk it is VIRTIO_DISK_SIZE of zeros."""
        words = LI(1, VIRTIO + 0x100) + [LW(10, 1, 0), LW(11, 1, 4)]
        self.assertEqual(self.run_pass(words).state.x[10], VIRTIO_DISK_SIZE // 512)
        with tempfile.TemporaryDirectory() as directory:
            disk = Path(directory) / "disk"
            for size in (512, VIRTIO_DISK_SIZE, 0x600000, VIRTIO_DISK_MAX):
                with self.subTest(size=size):
                    disk.write_bytes(bytes(size))
                    x = self.run_pass(words, extra=("--disk", str(disk))).state.x
                    self.assertEqual((x[10], x[11]), (size // 512, 0))
            for size in (0, 1000, VIRTIO_DISK_MAX + 512):
                with self.subTest(refused=size):
                    disk.write_bytes(bytes(size))
                    result = self.run_words(words + FINISH(), extra=("--disk", str(disk)))
                    self.assertEqual(result.status, 2)
                    self.assertIn("whole 512-byte sectors", result.stderr)

    def test_run_driver_rejects_negative_limit_and_times_out(self):
        driver = ROOT / "tools" / "rv32_run_emu.py"
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "image.bin"
            image.write_bytes(JAL(0, 0).to_bytes(4, "little"))
            common = [sys.executable, str(driver), str(image), "--emulator", str(self.emulator)]
            completed = subprocess.run(common + ["--max-instructions", "-1"], capture_output=True, text=True)
            self.assertEqual(completed.returncode, 2)
            self.assertIn("is negative", completed.stderr)
            completed = subprocess.run(common + ["--timeout", "0.5", "--max-instructions", "4000000000"],
                                       capture_output=True, text=True, timeout=30)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("did not finish within 0.5 s", completed.stderr)


class DiffDriverTest(unittest.TestCase):
    """The QEMU differential must never pass on a stale log or a failed QEMU run."""

    def run_diff(self, qemu, log):
        script = ROOT / "tools" / "rv32_diff_qemu.py"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "trace").write_text("1 80000000 00100093 x1=00000001\n")
            (path / "image.elf").write_bytes(b"")
            return subprocess.run([sys.executable, str(script), str(path / "image.elf"), str(path / "trace"),
                                   "--qemu", str(qemu), "--log", str(log)], capture_output=True, text=True, timeout=30)

    def test_failed_qemu_and_stale_log_are_rejected(self):
        good_log = "Trace 0: 0x1 [00000100/0000000080000000/01c1401b/ff020201]\n"
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            stale = path / "qemu.log"
            stale.write_text(good_log)
            completed = self.run_diff(shutil.which("false"), stale)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("QEMU exited with status 1", completed.stderr)
            self.assertFalse(stale.exists(), "the old log is removed before QEMU starts")
            fake = path / "fake-qemu.sh"
            fake.write_text("#!/bin/sh\nexit 0\n")
            fake.chmod(0o755)
            completed = self.run_diff(fake, stale)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("wrote no execution log", completed.stderr)
            fake.write_text("#!/bin/sh\nwhile [ $# -gt 1 ]; do [ \"$1\" = -D ] && printf %s \"$2\" > \"$2\"; shift; done; exit 0\n")
            completed = self.run_diff(fake, stale)
            self.assertEqual(completed.returncode, 1)
            self.assertIn("no instructions inside RAM", completed.stderr)
            fake.write_text("#!/bin/sh\nwhile [ $# -gt 1 ]; do [ \"$1\" = -D ] && printf '%s' '" + good_log + "' > \"$2\"; shift; done; exit 0\n")
            completed = self.run_diff(fake, stale)
            self.assertEqual(completed.returncode, 0, completed.stderr)
            self.assertIn("1 instructions: emulator and QEMU executed the same PC sequence", completed.stdout)


class DriverParsingTest(unittest.TestCase):
    def test_halt_line(self):
        self.assertIsNone(halt_line("rv32emu: cannot open x\n"))
        self.assertEqual(halt_line("noise\nrv32emu: halt=done steps=7 retired=7 traps=0 loaded=28 done=00073333 fail=7\n"),
                         {"halt": "done", "steps": 7, "retired": 7, "traps": 0, "loaded": 28, "done": 0x73333,
                          "outcome": "fail=7"})
        self.assertEqual(halt_line("rv32emu: halt=limit steps=5 retired=5 traps=0 loaded=4 error=instruction-limit pc=80000000")
                         ["outcome"], "error=instruction-limit pc=80000000")

    def test_qemu_log_and_trace_parsing(self):
        log = ("Trace 0: 0x107bbc180 [00000100/0000000000001000/01c1401b/ff020201] \n"
               "Trace 0: 0x107c01c00 [00000100/0000000080000000/01c1401b/ff020201] _start\n"
               "Stopped execution of TB chain before 0x107c01c00 [0000000080000058] rv32_exit\n"
               "Trace 0: 0x107c01c00 [00000100/0000000080000004/01c1401b/ff020201]\n")
        self.assertEqual(qemu_pcs(log), [RAM, RAM + 4])
        self.assertEqual(trace_pcs("1 80000000 00100093 x1=00000001\n2 80000004 00000073 trap 11 00000000\n"), [RAM, RAM + 4])
        self.assertIsNone(compare([RAM, RAM + 4], [RAM, RAM + 4]))
        self.assertIsNone(compare([RAM, RAM + 4], [RAM, RAM + 4, RAM + 8, RAM + 8]), "guard spin repeats are tolerated")
        self.assertIn("instruction 2", compare([RAM, RAM + 4], [RAM, RAM + 8]))
        self.assertIn("stopped after 1", compare([RAM, RAM + 4], [RAM]))
        self.assertIn("extra", compare([RAM, RAM + 4], [RAM, RAM + 4, RAM + 12]))


if __name__ == "__main__":
    unittest.main()
