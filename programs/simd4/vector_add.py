"""A small kernel builder: C[i] = (A[i] + B[i]) modulo 65536.

Data word bases: A=0x00, B=0x40, C=0x80. All lanes are active.
Run through `make test-simd4` or `make sim-simd4` from the repository root.
"""

from tools.simd4_model import image, word


def program(lanes=4, length=32):
    if lanes not in (1, 2, 4) or not 1 <= length <= 64 or length % lanes:
        raise ValueError("length must be 1..64 and a multiple of the supported lane count")
    return image([
        word(2, rd=0),                       # 0: LANE r0        ; starting index
        word(7, imm=length // lanes),        # 1: SETLOOP groups
        word(5, rd=1, ra=0, imm=0x00),       # 2: LOAD r1,r0,A   ; loop starts here
        word(5, rd=2, ra=0, imm=0x40),       # 3: LOAD r2,r0,B
        word(3, rd=3, ra=1, rb=2),           # 4: ADD r3,r1,r2   ; all lanes together
        word(6, rd=3, ra=0, imm=0x80),       # 5: STORE r3,r0,C
        word(4, rd=0, ra=0, imm=lanes),      # 6: ADDI r0,r0,LANES
        word(8, imm=2),                     # 7: LOOP 2         ; one shared branch
        word(0),                            # 8: HLT
    ])
