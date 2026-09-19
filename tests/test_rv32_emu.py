"""Hand-computed edge tests for the RV32 emulator (tools/rv32emu.c).

The instruction encoder below is written from the RV32I instruction formats
independently of the emulator's decoder, and every expected value is computed
by hand or with Python integers, never by running the emulator. Each test
assembles a raw image, runs the compiled emulator on it, and inspects the
state dump, the trace, the console output, and the exit status.
"""

from collections import namedtuple
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest

from tools.rv32_run_emu import halt_line


ROOT = Path(__file__).resolve().parents[1]
RAM = 0x80000000
CONSOLE = 0x10000000
DONE = 0x00100000
M = 0xFFFFFFFF

# Instruction formats (RV32I unprivileged specification, chapter 2).
def r_type(opcode, rd, funct3, rs1, rs2, funct7):
    return (funct7 << 25) | (rs2 << 20) | (rs1 << 15) | (funct3 << 12) | (rd << 7) | opcode


def i_type(opcode, rd, funct3, rs1, imm):
    return ((imm & 0xFFF) << 20) | (rs1 << 15) | (funct3 << 12) | (rd << 7) | opcode


def s_type(opcode, funct3, rs1, rs2, imm):
    imm &= 0xFFF
    return ((imm >> 5) << 25) | (rs2 << 20) | (rs1 << 15) | (funct3 << 12) | ((imm & 0x1F) << 7) | opcode


def b_type(funct3, rs1, rs2, offset):
    offset &= 0x1FFF
    return ((offset >> 12) << 31) | (((offset >> 5) & 0x3F) << 25) | (rs2 << 20) | (rs1 << 15) \
        | (funct3 << 12) | (((offset >> 1) & 0xF) << 8) | (((offset >> 11) & 1) << 7) | 0x63


def u_type(opcode, rd, imm20):
    return ((imm20 & 0xFFFFF) << 12) | (rd << 7) | opcode


def j_type(rd, offset):
    offset &= 0x1FFFFF
    return ((offset >> 20) << 31) | (((offset >> 1) & 0x3FF) << 21) | (((offset >> 11) & 1) << 20) \
        | (((offset >> 12) & 0xFF) << 12) | (rd << 7) | 0x6F


# Mnemonics; registers are numbers so nothing is hidden behind ABI names.
def LUI(rd, imm20): return u_type(0x37, rd, imm20)
def AUIPC(rd, imm20): return u_type(0x17, rd, imm20)
def JAL(rd, offset): return j_type(rd, offset)
def JALR(rd, rs1, imm): return i_type(0x67, rd, 0, rs1, imm)
def BEQ(rs1, rs2, off): return b_type(0, rs1, rs2, off)
def BNE(rs1, rs2, off): return b_type(1, rs1, rs2, off)
def BLT(rs1, rs2, off): return b_type(4, rs1, rs2, off)
def BGE(rs1, rs2, off): return b_type(5, rs1, rs2, off)
def BLTU(rs1, rs2, off): return b_type(6, rs1, rs2, off)
def BGEU(rs1, rs2, off): return b_type(7, rs1, rs2, off)
def LB(rd, rs1, imm): return i_type(0x03, rd, 0, rs1, imm)
def LH(rd, rs1, imm): return i_type(0x03, rd, 1, rs1, imm)
def LW(rd, rs1, imm): return i_type(0x03, rd, 2, rs1, imm)
def LBU(rd, rs1, imm): return i_type(0x03, rd, 4, rs1, imm)
def LHU(rd, rs1, imm): return i_type(0x03, rd, 5, rs1, imm)
def SB(rs2, rs1, imm): return s_type(0x23, 0, rs1, rs2, imm)
def SH(rs2, rs1, imm): return s_type(0x23, 1, rs1, rs2, imm)
def SW(rs2, rs1, imm): return s_type(0x23, 2, rs1, rs2, imm)
def ADDI(rd, rs1, imm): return i_type(0x13, rd, 0, rs1, imm)
def SLTI(rd, rs1, imm): return i_type(0x13, rd, 2, rs1, imm)
def SLTIU(rd, rs1, imm): return i_type(0x13, rd, 3, rs1, imm)
def XORI(rd, rs1, imm): return i_type(0x13, rd, 4, rs1, imm)
def ORI(rd, rs1, imm): return i_type(0x13, rd, 6, rs1, imm)
def ANDI(rd, rs1, imm): return i_type(0x13, rd, 7, rs1, imm)
def SLLI(rd, rs1, sh): return i_type(0x13, rd, 1, rs1, sh)
def SRLI(rd, rs1, sh): return i_type(0x13, rd, 5, rs1, sh)
def SRAI(rd, rs1, sh): return i_type(0x13, rd, 5, rs1, 0x400 | sh)
def ADD(rd, a, b): return r_type(0x33, rd, 0, a, b, 0)
def SUB(rd, a, b): return r_type(0x33, rd, 0, a, b, 0x20)
def SLL(rd, a, b): return r_type(0x33, rd, 1, a, b, 0)
def SLT(rd, a, b): return r_type(0x33, rd, 2, a, b, 0)
def SLTU(rd, a, b): return r_type(0x33, rd, 3, a, b, 0)
def XOR(rd, a, b): return r_type(0x33, rd, 4, a, b, 0)
def SRL(rd, a, b): return r_type(0x33, rd, 5, a, b, 0)
def SRA(rd, a, b): return r_type(0x33, rd, 5, a, b, 0x20)
def OR(rd, a, b): return r_type(0x33, rd, 6, a, b, 0)
def AND(rd, a, b): return r_type(0x33, rd, 7, a, b, 0)
def FENCE(): return i_type(0x0F, 0, 0, 0, 0x0FF)
def ECALL(): return 0x00000073
def EBREAK(): return 0x00100073
def MRET(): return 0x30200073
def CSRRW(rd, csr, rs1): return i_type(0x73, rd, 1, rs1, csr)
def CSRRS(rd, csr, rs1): return i_type(0x73, rd, 2, rs1, csr)
def CSRRC(rd, csr, rs1): return i_type(0x73, rd, 3, rs1, csr)
def CSRRWI(rd, csr, uimm): return i_type(0x73, rd, 5, uimm, csr)
MTVEC, MEPC, MCAUSE, MTVAL, MSTATUS = 0x305, 0x341, 0x342, 0x343, 0x300


def LI(rd, value):
    """Always two instructions (lui + addi) so program offsets stay predictable."""
    value &= M
    low = value & 0xFFF
    if low >= 0x800:
        low -= 0x1000
    high = ((value - low) & M) >> 12
    return [LUI(rd, high), ADDI(rd, rd, low)]


def FINISH(word=0x5555):
    """Write `word` to the done register through x30/x31; five instructions."""
    return LI(30, DONE) + LI(31, word) + [SW(31, 30, 0)]


State = namedtuple("State", "pc x mtvec mepc mcause mtval steps retired traps halt done")
Result = namedtuple("Result", "status stdout stderr state trace halt")


def parse_state(text):
    fields = {}
    for line in text.splitlines():
        key, value = line.split()
        fields[key] = value if key == "halt" else int(value, 16 if key not in ("steps", "retired", "traps") else 10)
    x = [fields[f"x{i}"] for i in range(32)]
    return State(fields["pc"], x, fields["mtvec"], fields["mepc"], fields["mcause"], fields["mtval"],
                 fields["steps"], fields["retired"], fields["traps"], fields["halt"], fields.get("done"))


class EmulatorTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.workdir = tempfile.TemporaryDirectory()
        cls.emulator = Path(cls.workdir.name) / "rv32emu"
        compiler = os.environ.get("HOST_CC", "cc")
        subprocess.run([compiler, "-std=c11", "-O2", "-Wall", "-Wextra", "-Werror", "-o", str(cls.emulator),
                        str(ROOT / "tools" / "rv32emu.c")], check=True)

    @classmethod
    def tearDownClass(cls):
        cls.workdir.cleanup()

    def run_words(self, words, limit=10000, base=RAM, extra=()):
        """Run a raw word image and return the exit status, outputs, state, and trace lines."""
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path / "image.bin").write_bytes(b"".join(word.to_bytes(4, "little") for word in words))
            command = [str(self.emulator), "--image", str(path / "image.bin"), "--base", f"{base:#x}",
                       "--dump-state", str(path / "state"), "--trace", str(path / "trace"),
                       "--max-instructions", str(limit), *extra]
            completed = subprocess.run(command, capture_output=True, text=True)
            state = parse_state((path / "state").read_text())
            trace = (path / "trace").read_text().splitlines()
        return Result(completed.returncode, completed.stdout, completed.stderr, state, trace,
                      halt_line(completed.stderr))

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
        effects = [line.split(" ", 3)[3] for line in result.trace if "mem[" in line]
        self.assertEqual(effects[0], "mem[80001000]<-11223344/4")
        self.assertEqual(effects[1], "x7=00000044 mem[80001000]->00000044/1")
        self.assertEqual(effects[8], "mem[80001006]<-000000ab/1", "trace shows the narrowed value and width")

    # Branches and jumps.

    def test_branch_conditions_at_signed_boundary(self):
        # a = INT32_MIN, b = 1: every branch skips `addi xN, x0, 1` when taken.
        a, b = 1, 2
        words = LI(a, 0x80000000) + LI(b, 1)
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
                ([r_type(0x33, 1, 0, 2, 3, 1)], 2, r_type(0x33, 1, 0, 2, 3, 1)),   # mul: M extension
                ([i_type(0x0F, 0, 1, 0, 0)], 2, i_type(0x0F, 0, 1, 0, 0)),         # fence.i
                ([CSRRS(1, MSTATUS, 0)], 2, CSRRS(1, MSTATUS, 0)),                 # unimplemented CSR
                ([i_type(0x13, 1, 1, 0, 0x420)], 2, i_type(0x13, 1, 1, 0, 0x420)),  # slli with funct7 set
                ([r_type(0x33, 1, 1, 2, 3, 0x20)], 2, r_type(0x33, 1, 1, 2, 3, 0x20)),  # sll with sub bit
                ([b_type(2, 0, 0, 8)], 2, b_type(2, 0, 0, 8)),                     # unused branch funct3
                ([i_type(0x03, 1, 3, 0, 0)], 2, i_type(0x03, 1, 3, 0, 0)),         # ld does not exist
                ([0x10200073], 2, 0x10200073)]:                                    # sret
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
            ([LBU(1, 2, 0)], RAM + 0x400000, 5, RAM + 0x400000),  # first byte past RAM
            ([LW(1, 2, 0)], RAM + 0x3FFFFE, 4, RAM + 0x3FFFFE),   # misaligned before bounds
            ([LH(1, 2, 0)], RAM + 0x3FFFFF, 4, RAM + 0x3FFFFF),
            ([SW(1, 2, 0)], 0x20000000, 7, 0x20000000),     # unmapped store
            ([SB(1, 2, 0)], 0x7FFFFFFF, 7, 0x7FFFFFFF),     # one byte below RAM
            ([SW(1, 2, 0)], 0xFFFFFFFC, 7, 0xFFFFFFFC),     # top of the address space
            ([LW(1, 2, 0)], 0xFFFFFFFC, 5, 0xFFFFFFFC),
            ([LBU(1, 2, 0)], CONSOLE, 5, CONSOLE),          # console TX is write-only
            ([LBU(1, 2, 1)], CONSOLE, 5, CONSOLE + 1),      # other console offsets
            ([LHU(1, 2, 4)], CONSOLE, 5, CONSOLE + 4),      # console status is byte-only
            ([LW(1, 2, 0)], CONSOLE, 5, CONSOLE),
            ([SB(1, 2, 5)], CONSOLE, 7, CONSOLE + 5),       # status is read-only
            ([SH(1, 2, 0)], CONSOLE, 7, CONSOLE),           # TX is byte-only
            ([SW(1, 2, 0)], CONSOLE, 7, CONSOLE),
            ([LW(1, 2, 0)], DONE, 5, DONE),                 # done register is write-only
            ([SB(1, 2, 0)], DONE, 7, DONE),                 # and word-only
            ([SH(1, 2, 0)], DONE, 7, DONE),
            ([SW(1, 2, 4)], DONE, 7, DONE + 4)]
        for body, base, cause, tval in cases:
            with self.subTest(body=body, base=hex(base)):
                result = self.run_trapping(LI(2, base) + LI(1, 0x5555) + body)
                self.assertEqual((result.state.x[10], result.state.x[11]), (cause, tval))
        result = self.run_trapping(LI(2, RAM + 0x3FFFFC) + [LW(1, 2, 0), LH(1, 2, 2), LBU(1, 2, 3), SW(1, 2, 0),
                                                            SH(1, 2, 2), SB(1, 2, 3), LW(1, 2, 4)])
        self.assertEqual((result.state.x[10], result.state.x[11]), (5, RAM + 0x400000), "only the last access faults")
        self.assertEqual(result.state.retired, 2 + 1 + 2 + 6 + 3 + 5, "the six in-bounds accesses retired")

    def test_double_fault_halts_with_report(self):
        result = self.run_words([ECALL()] + FINISH())
        self.assertEqual((result.status, result.state.halt), (2, "double-fault"))
        self.assertEqual((result.state.mcause, result.state.mepc, result.state.mtval), (11, RAM, 0))
        self.assertIn("unhandled-trap mcause=11 mepc=80000000 mtval=00000000 then mcause=1 mtval=00000000 at pc=00000000",
                      result.stderr)
        self.assertEqual(result.trace, [f"1 80000000 00000073 trap 11 00000000", "2 00000000 00000000 trap 1 00000000"])
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

    def test_console_and_done_register(self):
        words = LI(5, CONSOLE) + [LBU(6, 5, 5)] + LI(7, ord("H")) + [SB(7, 5, 0)] + LI(7, ord("i")) + [SB(7, 5, 0)]
        words += LI(7, 0x10A) + [SB(7, 5, 0)]
        result = self.run_pass(words)
        self.assertEqual(result.stdout, "Hi\n", "only the low byte of x7 is transmitted")
        self.assertEqual(result.state.x[6], 0x20, "console always ready")
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

    def test_instruction_limit_and_counts(self):
        result = self.run_words([JAL(0, 0)], limit=50)
        self.assertEqual((result.status, result.state.halt, result.state.steps), (2, "limit", 50))
        self.assertEqual(result.halt["outcome"], "error=instruction-limit pc=80000000")
        result = self.run_pass([ADDI(1, 0, 1), ADDI(2, 1, 1)])
        self.assertEqual((result.state.steps, result.state.retired, result.state.traps), (2 + 5, 7, 0))
        self.assertEqual(result.trace[:2], ["1 80000000 00100093 x1=00000001", "2 80000004 00108113 x2=00000002"])

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


if __name__ == "__main__":
    unittest.main()
