"""F2 image/ABI checks and benchmark support independent of RTL execution."""
import ctypes
import os
from pathlib import Path
import random
import subprocess
import tempfile
import unittest

from tools.rv32_f_asm import arithmetic, fp, flw, fsw, fli
from tools.rv32_asm import CSRRW, CSRRWI, FINISH, words_to_bytes
from tools.rv32_image import check_image, check_listing, parse_elf, LISTING_LINE, listing_word, F_OPCODES
from tools.rv32_run_emu import floating_objects

ROOT = Path(__file__).resolve().parents[1]


def listing(word, mnemonic='fadd.s', byte_form=False):
    encoded = word.to_bytes(4, 'little').hex(' ') if byte_form else f'{word:08x}'
    return f'80000000: {encoded}  {mnemonic} f0, f1, f2\n'


class FloatingToolsTest(unittest.TestCase):
    def test_image_isa_is_explicit_and_rejects_reserved_encodings(self):
        valid = [arithmetic(op, 7) for op in range(18)] + [flw(0, 1), fsw(0, 1), fp(0x70, 1, 0), fp(0x78, 0, 1), CSRRW(1, 3, 0)]
        for word in valid:
            for byte_form in (False, True):
                text = listing(word, byte_form=byte_form)
                self.assertTrue(check_listing(text))
                self.assertEqual(check_listing(text, allow_f=True), [])
        invalid = [arithmetic(0, 5), arithmetic(3, 0) | (1 << 25), fp(1, 0, 1, 2), fp(0x70, 1, 2, 3), flw(0, 1) ^ (1 << 12)]
        for word in invalid:
            self.assertTrue(check_listing(listing(word), allow_f=True))
        self.assertTrue(check_listing('', allow_f=True))
        self.assertTrue(check_listing('80000000: 00200073  <unknown>\n', allow_f=True))

    def test_compiled_images_abi_and_real_instructions(self):
        expectations = {'floatcheck': ('fmul.s', 'fadd.s', 'fdiv.s', 'fsub.s', 'fmv.w.x', 'fmv.x.w'),
                        'floatconvert': ('fcvt.s.w', 'fcvt.s.wu', 'fcvt.w.s', 'fcvt.wu.s', 'frflags'),
                        'floatsoft': ()}
        for name, mnemonics in expectations.items():
            with self.subTest(name=name):
                path = ROOT / 'build/rv32' / name
                elf = parse_elf(path.with_suffix('.elf').read_bytes())
                text = path.with_suffix('.lst').read_text()
                self.assertEqual(elf.flags, 0, 'ILP32, no RVC/hard-float ABI')
                self.assertEqual(check_image(elf, text, allow_f=name != 'floatsoft'), [])
                decoded = [m for line in text.splitlines() if (m := LISTING_LINE.match(line))]
                emitted = {m.group(3) for m in decoded}
                self.assertTrue(set(mnemonics) <= emitted, emitted)
                if name == 'floatsoft':
                    self.assertFalse(any((listing_word(m.group(2)) or 0) & 127 in F_OPCODES for m in decoded))
                    for helper in ('__addsf3', '__mulsf3', '__divsf3', '__subsf3'): self.assertIn(helper, elf.symbols)
                else:
                    attrs = path.with_suffix('.readelf').read_text()
                    self.assertIn('zicsr', attrs)
                    self.assertRegex(attrs, r'rv32i[^\n]*f2')

    def test_runtime_inputs_cannot_be_constant_folded(self):
        for name, symbol, offset, bits in [('floatcheck', 'float_inputs', 8, 0x41000000),
                                          ('floatsoft', 'float_inputs', 8, 0x41000000),
                                          ('floatconvert', 'signed_input', 0, 0xfffffffd)]:
            base = ROOT / 'build/rv32' / name
            elf = parse_elf(base.with_suffix('.elf').read_bytes())
            image = bytearray(base.with_suffix('.bin').read_bytes())
            at = elf.symbols[symbol] - 0x80000000 + offset
            image[at:at+4] = bits.to_bytes(4, 'little')
            with tempfile.TemporaryDirectory() as directory:
                path = Path(directory)/'changed.bin'; path.write_bytes(image)
                run = subprocess.run([str(ROOT/'build/rv32/rv32emu'), '--image', str(path)], capture_output=True, text=True)
                self.assertEqual(run.returncode, 1, run.stderr)
                self.assertIn('FAIL ', run.stdout)

    def test_state_dump_preserves_float_bits_and_all_registers(self):
        words = fli(0, 0x7fc12345) + fli(31, 0x80000000) + [CSRRWI(0, 3, 31)] + FINISH()
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            (path/'image.bin').write_bytes(words_to_bytes(words))
            run = subprocess.run([str(ROOT/'build/rv32/rv32emu'), '--image', str(path/'image.bin'),
                                  '--dump-state', str(path/'state')], capture_output=True, text=True)
            self.assertEqual(run.returncode, 0, run.stderr)
            state = dict(line.split() for line in (path/'state').read_text().splitlines())
            self.assertEqual(state['f0'], '7fc12345')
            self.assertEqual(state['f31'], '80000000')
            self.assertEqual(state['fcsr'], '1f')
            self.assertEqual(state['x0'], '00000000')
            for reg in range(1,31): self.assertEqual(state[f'f{reg}'], '00000000')

    def test_software_multiply_helper_extremes_and_seeded(self):
        # Benchmark glue must not silently break high products or carries.
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)/'softabi.dylib'
            subprocess.run([os.environ.get('HOST_CC', 'cc'), '-std=c11', '-O2', '-shared',
                '-I'+str(ROOT/'third_party/softfloat/include'), str(ROOT/'programs/rv32/rt/softfloat_abi.c'),
                *floating_objects()[1:], '-o', str(path)], check=True)
            mul = getattr(ctypes.CDLL(str(path)), '__muldi3')
            mul.argtypes = [ctypes.c_uint64, ctypes.c_uint64]; mul.restype = ctypes.c_uint64
            rng = random.Random(20260922)
            values = [0, 1, 0xffffffff, 0x100000000, 0x8000000000000000, 0xffffffffffffffff]
            pairs = [(a,b) for a in values for b in values] + [(rng.getrandbits(64), rng.getrandbits(64)) for _ in range(1000)]
            for a,b in pairs: self.assertEqual(mul(a,b), a*b & 0xffffffffffffffff)


if __name__ == '__main__': unittest.main()
