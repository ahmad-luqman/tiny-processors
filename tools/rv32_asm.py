"""RV32I instruction encoder shared by the emulator and RTL tests.

The format encoders are written from the RV32I instruction format diagrams
(unprivileged specification, chapter 2) independently of the emulator's and
the RTL's decoders. Registers are numbers so nothing hides behind ABI names;
`LI` and `FINISH` always emit a fixed number of words so program offsets stay
predictable. `program_loop` is the canonical M3 loop that both backends run.
"""

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


LOOP_DATA = RAM + 0x100  # the loop's data word lies past the image, so it starts as zero


def program_loop(n=10):
    """The M3 loop: sum 1..n through memory, then jump, byte store, and both branch outcomes.

    Every instruction of the M3 subset appears, with a negative branch offset, a
    skipped instruction after `jal`, a byte store into a word the image never
    wrote, and one taken and one not-taken `beq`. Ends with the pass word.
    """
    assert 1 <= n <= 0x7FF
    return [
        AUIPC(5, 0),            # 0  x5 = this PC (RAM base)
        ADDI(5, 5, 0x100),      # 1  x5 = data word address
        ADDI(6, 0, n),          # 2  x6 = n
        ADDI(7, 0, 0),          # 3  x7 = i = 0
        SW(7, 5, 0),            # 4  sum = 0
        LW(8, 5, 0),            # 5  loop: x8 = sum
        ADDI(7, 7, 1),          # 6  i += 1
        ADD(8, 8, 7),           # 7  sum += i
        SW(8, 5, 0),            # 8  store sum
        SUB(9, 6, 7),           # 9  x9 = n - i
        BNE(9, 0, -20),         # 10 back to loop while i != n
        JAL(1, 8),              # 11 skip the next instruction, x1 = return address
        ADDI(8, 0, -1),         # 12 skipped
        LUI(10, 0x12345),       # 13 x10 = 0x12345000
        ADDI(10, 10, 0x6A8),    # 14 x10 = 0x123456a8
        SB(10, 5, 5),           # 15 byte 0xa8 into lane 1 of the next data word
        LW(11, 5, 4),           # 16 x11 = 0x0000a800: the other lanes were never written
        BEQ(8, 11, 8),          # 17 not taken
        ADDI(12, 0, n * (n + 1) // 2),  # 18 x12 = expected sum
        BEQ(8, 12, 8),          # 19 taken when the loop was right
        ADDI(8, 0, 0),          # 20 skipped when the loop was right
    ] + FINISH()


def words_to_hex(words):
    """One 8-digit lowercase word per line: the $readmemh format tools/rv32_image.py writes."""
    return "".join(f"{word & M:08x}\n" for word in words)


def words_to_bytes(words):
    """The flat little-endian image the emulator loads."""
    return b"".join((word & M).to_bytes(4, "little") for word in words)
