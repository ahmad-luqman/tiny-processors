"""RV32F test encodings, independent of both execution decoders."""
from tools.rv32_asm import i_type, r_type, s_type, LI


def fp(funct7, rd, rs1, rs2=0, funct3=0):
    return r_type(0x53, rd, funct3, rs1, rs2, funct7)


def flw(rd, base, offset=0):
    return i_type(0x07, rd, 2, base, offset)


def fsw(rs2, base, offset=0):
    return s_type(0x27, 2, base, rs2, offset)


def fli(rd, bits):
    return LI(6, bits) + [fp(0x78, rd, 6)]


def arithmetic(op, rm, rd=4, rs1=1, rs2=2, rs3=3):
    """Encode an F1 vector's operation as an instruction, not an RTL control word."""
    if 3 <= op <= 6:
        assert 0 <= rs3 < 32
        return r_type((0x43, 0x47, 0x4b, 0x4f)[op-3], rd, rm, rs1, rs2, rs3 << 2)
    if op in (0, 1, 2, 7, 8):
        return fp({0: 0x00, 1: 0x04, 2: 0x08, 7: 0x0c, 8: 0x2c}[op], rd, rs1, 0 if op == 8 else rs2, rm)
    if 9 <= op <= 12:
        return fp(0x68 if op <= 10 else 0x60, rd, rs1, (op-9) % 2, rm)
    if 13 <= op <= 15:
        return fp(0x50, rd, rs1, rs2, {13: 2, 14: 1, 15: 0}[op])
    if op in (16, 17):
        return fp(0x14, rd, rs1, rs2, op-16)
    raise ValueError(op)
