"""Check the RV32I multiply/divide helpers on the host against Python integers.

The helpers are plain C, so the same source compiles for the Mac. Each case is
run through builds at -O0 and -O2: a result that depends on undefined behavior
usually differs between the two.
"""

import ctypes
import os
from pathlib import Path
import random
import subprocess
import unittest


ROOT = Path(__file__).resolve().parents[1]
SOURCE = ROOT / "programs/rv32/rt/muldiv.c"
HOST_DIR = ROOT / "build/rv32/host"
MASK = 0xFFFFFFFF
INT_MIN = -0x80000000
EDGES = [0, 1, 2, 3, 7, 10, 0xFF, 0xFFFF, 0x10000, 0x40000000, 0x7FFFFFFF,
         0x80000000, 0x80000001, 0xAAAAAAAA, 0x55555555, 0xFFFFFFFE, 0xFFFFFFFF]


def signed(value):
    value &= MASK
    return value - (1 << 32) if value & 0x80000000 else value


def ref_mul(a, b):
    return (a * b) & MASK


def ref_divu(n, d):
    return MASK if d == 0 else n // d


def ref_remu(n, d):
    return n if d == 0 else n % d


def ref_div(n, d):
    n, d = signed(n), signed(d)
    if d == 0:
        return -1
    if n == INT_MIN and d == -1:
        return INT_MIN
    quotient = abs(n) // abs(d)
    return -quotient if (n < 0) != (d < 0) else quotient


def ref_rem(n, d):
    n, d = signed(n), signed(d)
    if d == 0:
        return n
    if n == INT_MIN and d == -1:
        return 0
    remainder = abs(n) % abs(d)
    return -remainder if n < 0 else remainder


def build(optimization):
    HOST_DIR.mkdir(parents=True, exist_ok=True)
    library = HOST_DIR / f"librv32rt-{optimization}.dylib"
    command = [os.environ.get("HOST_CC", "cc"), "-shared", f"-{optimization}", "-std=c11",
               "-fno-builtin", "-Wall", "-Wextra", "-Werror", "-o", str(library), str(SOURCE)]
    subprocess.run(command, check=True)
    lib = ctypes.CDLL(str(library))
    for name, argtype, restype in (("rv32_mul", ctypes.c_uint32, ctypes.c_uint32),
                                   ("rv32_divu", ctypes.c_uint32, ctypes.c_uint32),
                                   ("rv32_remu", ctypes.c_uint32, ctypes.c_uint32),
                                   ("rv32_div", ctypes.c_int32, ctypes.c_int32),
                                   ("rv32_rem", ctypes.c_int32, ctypes.c_int32)):
        function = getattr(lib, name)
        function.argtypes = [argtype, argtype]
        function.restype = restype
    return lib


class MulDivTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.libraries = {level: build(level) for level in ("O0", "O2")}

    def check(self, name, reference, a, b):
        for level, lib in self.libraries.items():
            with self.subTest(op=name, level=level, a=hex(a), b=hex(b)):
                if name in ("rv32_div", "rv32_rem"):
                    observed = getattr(lib, name)(signed(a), signed(b))
                else:
                    observed = getattr(lib, name)(a, b)
                self.assertEqual(observed, reference(a, b))

    def check_all(self, a, b):
        self.check("rv32_mul", ref_mul, a, b)
        self.check("rv32_divu", ref_divu, a, b)
        self.check("rv32_remu", ref_remu, a, b)
        self.check("rv32_div", ref_div, a, b)
        self.check("rv32_rem", ref_rem, a, b)

    def test_edge_grid(self):
        for a in EDGES:
            for b in EDGES:
                self.check_all(a, b)

    def test_division_by_zero_follows_m_extension(self):
        for n in (0, 1, 42, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF):
            self.check("rv32_divu", ref_divu, n, 0)
            self.check("rv32_remu", ref_remu, n, 0)
            self.check("rv32_div", ref_div, n, 0)
            self.check("rv32_rem", ref_rem, n, 0)
        self.assertEqual(ref_divu(42, 0), MASK)
        self.assertEqual(ref_div(42, 0), -1)
        self.assertEqual(ref_rem(42, 0), 42)

    def test_signed_overflow_case(self):
        self.check("rv32_div", ref_div, 0x80000000, 0xFFFFFFFF)
        self.check("rv32_rem", ref_rem, 0x80000000, 0xFFFFFFFF)
        self.assertEqual(ref_div(0x80000000, 0xFFFFFFFF), INT_MIN)
        self.assertEqual(ref_rem(0x80000000, 0xFFFFFFFF), 0)

    def test_truncation_toward_zero(self):
        cases = {(-7, 2): (-3, -1), (7, -2): (-3, 1), (-7, -2): (3, -1), (7, 2): (3, 1),
                 (-1, 3): (0, -1), (1, -3): (0, 1), (INT_MIN, 2): (-0x40000000, 0),
                 (INT_MIN, 3): (-715827882, -2), (0x7FFFFFFF, -1): (-0x7FFFFFFF, 0)}
        for (n, d), (quotient, remainder) in cases.items():
            self.assertEqual((ref_div(n & MASK, d & MASK), ref_rem(n & MASK, d & MASK)),
                             (quotient, remainder))
            self.check("rv32_div", ref_div, n & MASK, d & MASK)
            self.check("rv32_rem", ref_rem, n & MASK, d & MASK)

    def test_large_divisors_need_the_33rd_bit(self):
        for n, d in ((0xFFFFFFFF, 0x80000000), (0xFFFFFFFF, 0xFFFFFFFF), (0x80000000, 0x80000001),
                     (0xFFFFFFFE, 0x7FFFFFFF), (0x80000000, 0x80000000), (0x80000001, 0x80000000)):
            self.check("rv32_divu", ref_divu, n, d)
            self.check("rv32_remu", ref_remu, n, d)

    def test_seeded_random_pairs(self):
        generator = random.Random(0x5232)
        for _ in range(2000):
            a = generator.getrandbits(generator.choice((4, 8, 16, 31, 32)))
            b = generator.getrandbits(generator.choice((4, 8, 16, 31, 32)))
            self.check_all(a, b)


if __name__ == "__main__":
    unittest.main()
