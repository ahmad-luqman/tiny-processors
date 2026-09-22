"""A bounded square matrix kernel: C = (A x B) >>> shift, N x N signed 16-bit words.

Data word bases: A=0x00, B=0x40, C=0x80, all row-major. Lane j of column group g
owns column c = g * lanes + j and computes C[i][c] for every row i with the
32-bit accumulator: acc = sum over k of A[i][k] * B[k][c], then RDA truncates
(acc >>> shift) to 16 bits before the store.

The core has one shared loop counter, so the builder unrolls the k loop inside
each row iteration and emits one straight-line region per column group, each
with its own SETLOOP over rows. Every lane loads its A[i][k] separately: the
shared port transfers each A word once per lane, which the benchmark measures.
Run through `make test-simd4` or `make bench-simd4` from the repository root.
"""

from tools.simd4_model import (ADD, ADDI, CLRA, HLT, LANE, LDI, LOAD, LOOP, MAC, RDA, SETLOOP, STORE,
                               image, signed, word)

A_BASE, B_BASE, C_BASE = 0x00, 0x40, 0x80


def word_count(lanes, n):
    """Program words the builder emits: per column group 4 setup words plus 3n+6 per unrolled row, then HLT."""
    return (n // lanes) * (4 + 3 * n + 6) + 1


def program(lanes=4, n=4, shift=0):
    if lanes not in (1, 2, 4) or n not in (2, 4, 8) or n % lanes or not 0 <= shift <= 31:
        raise ValueError("n must be 2, 4 or 8 and a multiple of the supported lane count; shift 0..31")
    if word_count(lanes, n) > 256:
        raise ValueError(f"{n}x{n} on {lanes} lane(s) needs {word_count(lanes, n)} program words; the image holds 256")
    words = []
    for group in range(n // lanes):
        words += [
            word(LANE, rd=0),                          # r0 = lane
            word(ADDI, rd=0, ra=0, imm=group * lanes),  # r0 = c, this lane's column
            word(LDI, rd=1, imm=0),                    # r1 = i * N, the A row base
            word(SETLOOP, imm=n),                      # one shared counter over rows
        ]
        row_start = len(words)
        words.append(word(CLRA))                       # acc = 0
        for k in range(n):
            words += [
                word(LOAD, rd=2, ra=1, imm=A_BASE + k),            # r2 = A[i][k], same word in every lane
                word(LOAD, rd=3, ra=0, imm=B_BASE + k * n),       # r3 = B[k][c]
                word(MAC, ra=2, rb=3),                            # acc += r2 * r3 (signed)
            ]
        words += [
            word(RDA, rd=2, imm=shift),                # r2 = low 16 of (acc >>> shift)
            word(ADD, rd=3, ra=1, rb=0),               # r3 = i * N + c
            word(STORE, rd=2, ra=3, imm=C_BASE),       # C[i][c] = r2
            word(ADDI, rd=1, ra=1, imm=n),             # next row of A
            word(LOOP, imm=row_start),                 # all lanes branch together
        ]
    words.append(word(HLT))
    assert len(words) == word_count(lanes, n)
    return image(words)


def reference(a, b, n, shift=0):
    """Direct Python product of two row-major N x N lists of 16-bit words, as the kernel stores it."""
    out = []
    for i in range(n):
        for c in range(n):
            acc = sum(signed(a[i * n + k]) * signed(b[k * n + c]) for k in range(n))
            # Wrap to the 32-bit accumulator before the arithmetic shift: an 8x8 sum of
            # corner products can pass 2^31, and shifts of 16 or more reach the sign bit.
            out.append((signed(acc % 2**32, 32) >> shift) % 65536)
    return out


def expected_counts(lanes, n):
    """Retired instructions and transfers predicted from the kernel shape, checked by the runner."""
    groups = n // lanes
    # Per group: 4 setup words, then per row CLRA + 3n load/load/MAC + RDA/ADD/STORE/ADDI/LOOP; plus HLT.
    instructions = groups * (4 + n * (3 * n + 6)) + 1
    # Per output cell: n A loads + n B loads + 1 store, and every lane transfers each of its own.
    transfers = n * n * (2 * n + 1)
    return instructions, transfers
