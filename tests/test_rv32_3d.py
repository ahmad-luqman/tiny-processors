"""G2: the Python oracle, the guest C reference and their contract.

The Python model (tools/rv32_g3d_model.py) and programs/rv32/g3d_ref.c are
written independently; every test here that renders compares the two
bit for bit, framebuffer, Z buffer and counters.
"""
import ctypes as C
import os
from pathlib import Path
import random
import re
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import rv32_g3d_model as M  # noqa: E402
from tools.rv32_g3d_model import (Fault, assemble, encode, render, render_float, run_shader,  # noqa: E402
                                  s32, u32, ONE)
from tools.rv32_g3d_scene import SHADERS, constants, cube, passthrough, random_program, vertex  # noqa: E402

BUILD = ROOT / 'build/g3d'
U32 = C.c_uint32


class Job(C.Structure):
    _fields_ = [('program', C.POINTER(U32)), ('consts', C.POINTER(U32)), ('inputs', C.POINTER(U32)),
                ('triangles', C.POINTER(U32)), ('vcount', U32), ('tcount', U32), ('limit', U32),
                ('program_words', U32), ('zbase', U32)]


class Counts(C.Structure):
    _fields_ = [(n, U32) for n in 'error fault_pc instructions transfers divides pixels zfail culled cycles'.split()]


def build_reference(extra=()):
    BUILD.mkdir(parents=True, exist_ok=True)
    tag = '-san' if extra else ''
    out = BUILD / f'libg3dref{tag}.dylib'
    subprocess.run([os.environ.get('HOST_CC', 'cc'), '-shared', '-fPIC', '-O2', '-std=c11', '-Wall', '-Wextra',
                    '-Werror', '-fno-builtin', *extra, '-I' + str(ROOT / 'programs/rv32'), '-o', str(out),
                    str(ROOT / 'programs/rv32/g3d_ref.c')], check=True)
    lib = C.CDLL(str(out))
    lib.g3d_reference.restype = U32
    lib.g3d_div_sat.restype = C.c_int32
    lib.g3d_div_sat.argtypes = [C.c_int64, C.c_int32]
    return lib


def _array(values, n=None):
    values = list(values)
    if n is not None:
        values += [0] * (n - len(values))
    return (U32 * max(1, len(values)))(*values)


def c_render(lib, program, consts, inputs, triangles, limit=4096, fb=None, zbuf=None, vcount=None, zbase=0x80040000):
    fb = (C.c_uint8 * 76800)(*(fb or [0] * 76800))
    zbuf = (C.c_uint16 * 76800)(*(zbuf or [M.Z_MAX] * 76800))
    counts = Counts()
    words = [t if isinstance(t, int) else t[0] | t[1] << 8 | t[2] << 16 for t in triangles]
    # The program goes in at its own length: words past it must read as END.
    job = Job(_array(program), _array(consts, M.CONSTS),
              _array([w for row in inputs for w in row]), _array(words),
              len(inputs) if vcount is None else vcount, len(words), limit, len(program), zbase)
    lib.g3d_reference(fb, zbuf, C.byref(job), C.byref(counts))
    return bytes(fb), list(zbuf), counts


def tri_tuple(t):
    return t if not isinstance(t, int) else (t & 255, t >> 8 & 255, t >> 16 & 255)


class Reference(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = build_reference()

    def agree(self, program, consts, inputs, triangles, limit=4096, vcount=None, **kw):
        """Render on both references and require identical results; return the Python one."""
        vcount = len(inputs) if vcount is None else vcount
        py = render(program, consts, inputs, vcount, [tri_tuple(t) for t in triangles], limit, **kw)
        fb, zbuf, c = c_render(self.lib, program, consts, inputs, triangles, limit, vcount=vcount,
                               fb=list(kw['fb']) if 'fb' in kw else None, zbuf=kw.get('zbuf'),
                               zbase=kw.get('zbase', 0x80040000))
        fault = py['fault']
        self.assertEqual(c.error, fault.reason if fault else 0)
        if fault and fault.reason not in (M.E_PARAM, M.E_INDEX):
            self.assertEqual(c.fault_pc, fault.pc | fault.batch << 8)
        for name in ('instructions', 'transfers', 'divides', 'pixels', 'zfail', 'culled', 'cycles'):
            self.assertEqual(getattr(c, name), py[name], name)
        self.assertEqual(fb, bytes(py['fb']))
        self.assertEqual(zbuf, py['zbuf'])
        return py


class Contract(Reference):
    def test_header_constants_match_model(self):
        text = (ROOT / 'programs/rv32/g3d.h').read_text()
        defines = {m[1]: int(m[2].rstrip('u'), 0) for m in re.finditer(r'#define (G3D_\w+) (0x[0-9a-f]+u?|\d+u?)\b', text)}
        for name in ('BASE', 'SIZE', 'COMMAND', 'STATUS', 'ERROR', 'FAULT_PC', 'CYCLES', 'STALLS', 'INSTRUCTIONS',
                     'TRANSFERS', 'DIVIDES', 'PIXELS', 'ZFAIL', 'CULLED', 'VCOUNT', 'TCOUNT', 'ZBASE', 'LIMIT',
                     'CONST', 'PROGRAM', 'VERTEX', 'TRIANGLE', 'START', 'RESET', 'CLEAR_Z', 'IDLE', 'BUSY',
                     'DONE', 'FAULT'):
            self.assertEqual(defines['G3D_' + name], getattr(M, 'G3D_' + name), name)
        for name in ('LANES', 'REGS', 'PROGRAM_WORDS', 'CONSTS', 'SLOTS', 'VMAX', 'TMAX', 'DEPTH', 'GUARD', 'SUB'):
            self.assertEqual(defines['G3D_' + name], getattr(M, name), name)
        self.assertEqual(defines['G3D_Z_MAX'], M.Z_MAX)
        self.assertEqual(defines['G3D_DIVIDE_TICKS'], M.DIVIDE_TICKS)
        self.assertIn('#define G3D_ONE 0x10000', text)
        self.assertEqual(M.ONE, 0x10000)
        self.assertIn('#define G3D_W_NEAR (G3D_ONE >> 4)', text)
        self.assertEqual(M.W_NEAR, M.ONE >> 4)
        self.assertIn('#define G3D_CLEAR_CYCLES (1u+320u*240u*2u/4u+1u)', text)
        self.assertEqual(M.clear_cycles(), 1 + 320 * 240 * 2 // 4 + 1)
        ops = re.search(r'enum \{ (G3D_OP_END.*?) \};', text, re.S)[1]
        names = [n.strip()[len('G3D_OP_'):] for n in ops.split(',')]
        self.assertEqual(names[:-1], list(M.OPS))
        self.assertEqual(names[-1], 'COUNT')
        errors = re.search(r'enum \{ (G3D_E_NONE.*?) \};', text, re.S)[1]
        self.assertEqual([n.strip() for n in errors.split(',')],
                         ['G3D_E_' + n for n in 'NONE PARAM INTERNAL ILLEGAL OVERFLOW MISMATCH LIMIT PC INDEX'.split()])

    def test_register_constants_agree_across_languages(self):
        from tools import rv32_asm as asm
        names = [n for n in dir(asm) if n.startswith('G3D_')]
        self.assertEqual(len(names), 29)
        for name in names:
            self.assertEqual(getattr(asm, name), getattr(M, name), name)
        emulator = (ROOT / 'tools/rv32emu_core.c').read_text()
        self.assertIn('{"g3d", G3D_BASE, G3D_SIZE, g3d_mmio_load, g3d_mmio_store}', emulator)

    def test_divider_matches_truncating_saturating_division(self):
        rng = random.Random(3)
        cases = [(0, 1), (-1, 2), (1, 2), (-7, 2), (7, 2), ((1 << 31) - 1, 1), (1 << 31, 1), (-(1 << 31), 1),
                 (-(1 << 31) - 1, 1), ((1 << 62), 3), (-(1 << 62), 3), (5 << 31, 5), ((5 << 31) - 1, 5)]
        cases += [(rng.randrange(-(1 << 50), 1 << 50), rng.randrange(1, 1 << 31)) for _ in range(3000)]
        cases += [(rng.randrange(-(1 << 40), 1 << 40), rng.randrange(1, 1 << 10)) for _ in range(3000)]
        for n, d in cases:
            self.assertEqual(self.lib.g3d_div_sat(n, d), M.div_sat(n, d), (n, d))


class Assembler(unittest.TestCase):
    def test_structure_is_checked(self):
        for bad in ('ELSE\nEND', 'IF\nEND', 'ENDIF\nEND', 'LOOP\nEND', 'ENDLOOP\nEND', 'BREAK\nEND',
                    'IF\nELSE\nELSE\nENDIF\nEND', 'IF\nENDLOOP\nEND', 'ADD r0, r1, r2', 'LDI r0, 4194304',
                    'LDC r0, 32\nEND', 'IN r0, 8\nEND', 'ADDI r0, r0, 8192\nEND', 'ADD r16, r0, r0\nEND',
                    'NOP\nEND', 'ADD r0, r1, r2, r3\nEND', 'ADD r0, r1\nEND', 'IF r0\nENDIF\nEND',
                    'MAD r0, r1, r2\nEND', 'END r0', 'LDI r0\nEND', 'OUT 0, r1, r2\nEND', 'IF\n' * 9 + 'ENDIF\n' * 9 + 'END', 'LDI r0, 0\n' * 128 + 'END'):
            with self.assertRaises(ValueError, msg=bad):
                assemble(bad)
        assemble('IF\n' * 8 + 'ENDIF\n' * 8 + 'LDI r0, 0\n' * 110 + 'END')

    def test_targets_are_absolute_pcs(self):
        w = assemble('IF\nLDI r0, 1\nELSE\nLDI r0, 2\nENDIF\nLOOP\nBREAK\nENDLOOP\nEND')
        self.assertEqual(w[0] & 0x7f, 2)     # IF -> ELSE
        self.assertEqual(w[2] & 0x7f, 4)     # ELSE -> ENDIF
        self.assertEqual(w[7] & 0x7f, 6)     # ENDLOOP -> first body instruction
        self.assertEqual(assemble('IF\nENDIF\nEND')[0] & 0x7f, 1)
        self.assertEqual(assemble('LDI r3, -1.5f\nEND')[0], encode('LDI', 3) | (-3 * ONE // 2) & 0x3fffff)


def lanes(text, vcount=4, inputs=None, consts=None, limit=4096):
    """Run `text` (outputs in slot 0..) and return outputs per vertex."""
    inputs = inputs or [[v] + [0] * 7 for v in range(vcount)]
    return run_shader(assemble(text), consts or [0] * 32, inputs, vcount, limit)[0]


class Shader(Reference):
    def test_arithmetic(self):
        k = [u32(v) for v in (3 * ONE // 2, -ONE // 4, 0x7fffffff, -0x80000000, 5, ONE)] + [0] * 26
        out = lanes('''
            LDC r1, 0
            LDC r2, 1
            MUL r3, r1, r2      ; 1.5 * -0.25 = -0.375
            OUT 0, r3
            LDC r4, 2
            LDC r5, 3
            ADD r6, r4, r4      ; wraps
            OUT 1, r6
            ABS r6, r5          ; ABS(INT_MIN) = INT_MIN
            OUT 2, r6
            SUB r6, r5, r4      ; wraps
            OUT 3, r6
            LDC r6, 4
            MAD r7, r1, r2, r6  ; 5 raw + -0.375
            OUT 4, r7
            SRA r7, r2, 4
            OUT 5, r7
            SHL r7, r1, 31
            OUT 6, r7
            ADDI r7, r6, -8192
            OUT 7, r7
            END''', consts=k)[0]
        self.assertEqual([s32(v) for v in out],
                         [-3 * ONE // 8, -2, -0x80000000, 1, 5 - 3 * ONE // 8, -ONE // 64, 0, 5 - 8192])
        out = lanes('LDC r1, 1\nLDC r2, 1\nMUL r3, r1, r2\nOUT 0, r3\nLDC r1, 2\nMUL r3, r1, r1\nOUT 1, r3\n'
                    'MIN r3, r1, r2\nOUT 2, r3\nMAX r3, r1, r2\nOUT 3, r3\nXOR r3, r1, r2\nOUT 4, r3\n'
                    'AND r3, r1, r2\nOUT 5, r3\nOR r3, r1, r2\nOUT 6, r3\nMOV r3, r2\nOUT 7, r3\nEND', consts=k)[0]
        self.assertEqual(out, [ONE // 16, u32(((0x7fffffff ** 2) >> 16)), u32(-ONE // 4), 0x7fffffff,
                               u32(0x7fffffff ^ u32(-ONE // 4)), 0x7fffffff & u32(-ONE // 4),
                               0x7fffffff | u32(-ONE // 4), u32(-ONE // 4)])

    def test_special_registers_and_partial_batch(self):
        out = lanes('SPC r0, lane\nOUT 0, r0\nSPC r0, vid\nOUT 1, r0\nSPC r0, vcount\nOUT 2, r0\nEND', vcount=6)
        self.assertEqual([o[:3] for o in out], [[v % 4, v, 6] for v in range(6)])

    def test_if_else_masks_lanes(self):
        out = lanes('''
            IN r0, 0
            LDI r1, 2
            SLT r0, r1          ; lanes 0,1
            IF
                LDI r2, 10
                LDI r3, 1
                SEQ r0, r3      ; lane 1 only
                IF
                    LDI r2, 11
                ENDIF
            ELSE
                LDI r2, 20
            ENDIF
            OUT 0, r2
            END''')
        self.assertEqual([o[0] for o in out], [10, 11, 20, 20])

    def test_uniform_if_skips_body(self):
        # All lanes false: IF jumps to ENDIF, so only IF, ENDIF and the rest execute.
        _, n = run_shader(assemble('LDI r0, 1\nSLT r0, r0\nIF\nLDI r1, 1\nLDI r1, 2\nENDIF\nEND'), [0] * 32,
                          [[0] * 8] * 4, 4, 100)
        self.assertEqual(n, 5)
        _, n = run_shader(assemble('LDI r0, 1\nSEQ r0, r0\nIF\nLDI r1, 1\nELSE\nLDI r1, 2\nLDI r1, 2\nENDIF\nEND'),
                          [0] * 32, [[0] * 8] * 4, 4, 100)
        self.assertEqual(n, 7)   # ELSE finds no lane and jumps to ENDIF

    def test_divergent_loop_iterates_per_lane(self):
        out = lanes('''
            IN r0, 0            ; lane v iterates v+1 times
            LDI r1, 0
            LDI r2, 1
            LOOP
                ADD r1, r1, r2
                SLT r0, r1      ; P = v < count
                IF
                    BREAK
                ENDIF
            ENDLOOP
            OUT 0, r1
            END''', vcount=4)
        self.assertEqual([o[0] for o in out], [1, 2, 3, 4])

    def test_break_inside_nested_if_does_not_revive(self):
        out = lanes('''
            IN r0, 0
            LDI r5, 0
            LDI r6, 1
            LOOP
                LDI r1, 2
                SLT r0, r1          ; lanes 0,1 take the IF
                IF
                    LDI r1, 1
                    SLT r0, r1      ; lane 0 breaks from two IFs deep
                    IF
                        BREAK
                    ENDIF
                    ADD r5, r5, r6  ; lane 1 only
                    OUT 1, r5
                ELSE
                    ADD r5, r5, r6  ; lanes 2,3; a revived lane 0 would count here
                ENDIF
                LDI r1, 3
                SLT r5, r1          ; keep looping while count < 3
                IF
                ELSE
                    BREAK
                ENDIF
            ENDLOOP
            OUT 0, r5
            END''')
        self.assertEqual([o[0] for o in out], [0, 3, 3, 3])

    def test_nested_loops_and_depth_eight(self):
        body = 'LDI r0, 1\nSEQ r0, r0\n' + 'IF\n' * 7 + 'LOOP\nBREAK\nENDLOOP\n' + 'ENDIF\n' * 7 + 'LDI r1, 7\nOUT 0, r1\nEND'
        self.assertEqual(lanes(body)[0][0], 7)

    def run_fault(self, text_or_words, reason, pc, batch=0, vcount=4, limit=4096):
        words = assemble(text_or_words) if isinstance(text_or_words, str) else text_or_words
        with self.assertRaises(Fault) as caught:
            run_shader(words, [0] * 32, [[0] * 8] * vcount, vcount, limit)
        self.assertEqual((caught.exception.reason, caught.exception.pc, caught.exception.batch), (reason, pc, batch))
        self.agree(words, [0] * 32, [[0] * 8] * vcount, [], limit=limit)

    def test_faults(self):
        seq = assemble('LDI r0, 1\nSEQ r0, r0\nEND')[:2]
        self.run_fault(seq + [encode('IF')] * 9, M.E_OVERFLOW, 10)
        self.run_fault(seq + [encode('LOOP')] * 9, M.E_OVERFLOW, 10)
        self.run_fault([encode('ELSE')], M.E_MISMATCH, 0)
        self.run_fault([encode('ENDIF')], M.E_MISMATCH, 0)
        self.run_fault([encode('LOOP'), encode('ENDIF')], M.E_MISMATCH, 1)
        self.run_fault([encode('IF') | 1, encode('ENDLOOP')], M.E_MISMATCH, 1)
        self.run_fault([encode('BREAK')], M.E_MISMATCH, 0)
        self.run_fault([encode('IF') | 1, encode('BREAK')], M.E_MISMATCH, 1)
        self.run_fault([encode('LOOP'), encode('END')], M.E_MISMATCH, 1)
        self.run_fault([28 << 26], M.E_ILLEGAL, 0)
        self.run_fault([63 << 26], M.E_ILLEGAL, 0)
        self.run_fault([encode('LDI')] * 128, M.E_PC, 128)
        # An IF with no true lane jumps to its target; a program running off word 127 faults.
        false = assemble('LDI r0, 1\nSLT r0, r0\nEND')[:2]
        self.run_fault(false + [encode('IF') | 127] + [encode('LDI')] * 125, M.E_PC, 128)
        self.run_fault(seq + [encode('IF') | 127], M.E_MISMATCH, 3)   # all true: falls into END padding
        self.run_fault('LOOP\nENDLOOP\nEND', M.E_LIMIT, 1, limit=100)
        self.run_fault('LDI r0, 0\nEND', M.E_LIMIT, 1, limit=1)
        # The limit is per batch: 3 instructions per batch fit a limit of 3 with two batches.
        self.agree(assemble('LDI r0, 0\nLDI r0, 0\nEND'), [0] * 32, [[0] * 8] * 8, [], limit=3)
        # A second batch faults with its own number in FAULT_PC.
        self.run_fault('SPC r0, vid\nLDI r1, 4\nSLT r0, r1\nIF\nELSE\nLOOP\nENDLOOP\nENDIF\nEND', M.E_LIMIT, 6,
                       batch=1, vcount=8, limit=50)


class Differential(Reference):
    def test_random_structured_programs(self):
        rng = random.Random(2026)
        faults = set()
        for trial in range(400):
            text = '\n'.join(random_program(rng) + ['END'])
            try:
                words = assemble(text)
            except ValueError:
                continue
            vcount = rng.randrange(1, 33)
            inputs = [[rng.getrandbits(32) for _ in range(8)] for _ in range(vcount)]
            consts = [rng.getrandbits(32) for _ in range(32)]
            py = self.agree(words, consts, inputs, [], limit=rng.choice([60, 400, 4096]))
            faults.add(py['fault'].reason if py['fault'] else 0)
        self.assertIn(0, faults)
        self.assertIn(M.E_LIMIT, faults)

    def test_random_raw_words(self):
        # Unstructured words: every fault class must agree, including mid-body control mismatches.
        rng = random.Random(7)
        control = [encode(n) for n in ('IF', 'ELSE', 'ENDIF', 'LOOP', 'ENDLOOP', 'BREAK', 'END')]
        seen = set()
        for _ in range(1500):
            words = [rng.choice(control) | rng.randrange(128) if rng.random() < 0.3 else
                     rng.getrandbits(32) if rng.random() < 0.1 else
                     encode(rng.choice(M.OPS[1:22]), *(rng.randrange(16) for _ in range(4)), imm=rng.getrandbits(14))
                     for _ in range(rng.randrange(1, 40))]
            vcount = rng.randrange(1, 12)
            py = self.agree(words, [rng.getrandbits(32) for _ in range(32)],
                            [[rng.getrandbits(32) for _ in range(8)] for _ in range(vcount)], [], limit=300)
            seen.add(py['fault'].reason if py['fault'] else 0)
        # Short raw programs end in END padding, so the limit and PC faults come from directed tests.
        for reason in (0, M.E_ILLEGAL, M.E_OVERFLOW, M.E_MISMATCH):
            self.assertIn(reason, seen)


class Raster(Reference):
    def draw(self, verts, triangles, **kw):
        return self.agree(passthrough(), [0] * 32, verts, triangles, **kw)

    def test_cube_all_shaders(self):
        inputs, triangles = cube()
        for name, text in SHADERS.items():
            for angle in (0.0, 0.3, 1.1, 2.5, 4.0):
                py = self.agree(assemble(text), constants(angle, frame=int(angle * 40)), inputs, triangles)
                self.assertIsNone(py['fault'])
                self.assertEqual(py['culled'], 6 if angle else 8, (name, angle))   # 2 faces face the camera at angle 0
                self.assertGreater(py['pixels'], 5000)

    def test_back_faces_and_degenerate_are_culled(self):
        # Counter-clockwise in clip space (y up) is front-facing.
        front = [vertex(-.5, -.5), vertex(.5, -.5), vertex(0, .5)]
        py = self.draw(front, [(0, 1, 2), (0, 2, 1), (0, 0, 1)])
        self.assertEqual(py['culled'], 2)
        self.assertGreater(py['pixels'], 0)

    def test_near_guard_and_depth_range_cull_whole_triangles(self):
        ok = [vertex(-.5, -.5), vertex(.5, -.5), vertex(0, .5)]
        for bad in (vertex(0, .5, w=1 / 32), vertex(0, .5, w=-1), vertex(4.01, .5), vertex(0, -4.01),
                    vertex(0, .5, z=-.01), vertex(0, .5, z=1.01)):
            py = self.draw(ok[:2] + [bad], [(0, 1, 2)])
            self.assertEqual((py['culled'], py['pixels'], py['divides']), (1, 0, 6))
        # Exactly at the guard band and the near plane is kept.
        py = self.draw([vertex(-4, -1, w=1), vertex(4, -1, w=1), vertex(0, 4, w=1)], [(0, 1, 2)])
        self.assertEqual(py['culled'], 0)
        self.assertEqual(py['pixels'], 320 * 240)   # guard-band triangle covers the screen, scissored
        near = [vertex(x, y, z=1 / 32, w=1 / 16) for x, y in ((-.01, -.01), (.01, -.01), (0, .01))]
        py = self.draw(near, [(0, 1, 2)])
        self.assertEqual(py['culled'], 0)

    def test_shared_edges_write_each_pixel_once(self):
        # A quad split along a diagonal, and a fan around a centre: no pixel is covered twice,
        # so no depth test fails even though every triangle has the same depth.
        quad = [vertex(-.7, -.6), vertex(.8, -.5), vertex(.6, .7), vertex(-.5, .8)]
        py = self.draw(quad, [(0, 1, 2), (0, 2, 3)])
        self.assertEqual(py['zfail'], 0)
        fan = [vertex(0.03, -0.01)] + [vertex(0.9 * c, 0.9 * s) for c, s in
                                       ((1, 0), (.6, .8), (-.3, .95), (-1, .1), (-.4, -.9), (.7, -.7))]
        py = self.draw(fan, [(0, i, i % 6 + 1) for i in range(1, 7)])
        self.assertEqual(py['zfail'], 0)
        self.assertGreater(py['pixels'], 10000)

    def test_depth_order_is_independent_of_draw_order(self):
        near = [vertex(-.8, -.8, z=.2, r=255, g=0, b=0), vertex(.8, -.8, z=.2, r=255, g=0, b=0),
                vertex(0, .8, z=.2, r=255, g=0, b=0)]
        far = [vertex(-.6, -.9, z=.8, g=255, r=0, b=0), vertex(.9, -.2, z=.8, g=255, r=0, b=0),
               vertex(-.2, .9, z=.8, g=255, r=0, b=0)]
        a = self.draw(near + far, [(0, 1, 2), (3, 4, 5)])
        b = self.draw(near + far, [(3, 4, 5), (0, 1, 2)])
        self.assertEqual(a['fb'], b['fb'])
        self.assertGreater(a['zfail'], 0)
        self.assertEqual(a['zfail'] + a['pixels'], b['zfail'] + b['pixels'])
        # Equal depth: strictly-less means the first triangle keeps the pixel.
        c = self.draw(near + [vertex(-.8, -.8, z=.2, r=0, g=0, b=255), vertex(.8, -.8, z=.2, r=0, g=0, b=255),
                              vertex(0, .8, z=.2, r=0, g=0, b=255)], [(0, 1, 2), (3, 4, 5)])
        self.assertEqual(c['zfail'], c['pixels'])
        self.assertEqual(set(c['fb']) - {0}, {0xe0})

    def test_interpolation_constant_linear_and_bounded(self):
        # Constant attributes have zero gradients, so every covered pixel is exact.
        flat = [vertex(x, y, z=.3, r=200, g=100, b=40) for x, y in ((-.9, -.8), (.7, -.9), (.1, .9))]
        py = self.draw(flat, [(0, 1, 2)])
        written = [i for i, z in enumerate(py['zbuf']) if z != M.Z_MAX]
        self.assertEqual(len(written), py['pixels'])
        self.assertEqual({py['zbuf'][i] for i in written}, {M.trunc_div(M.u32(round(.3 * ONE)) * M.Z_MAX, ONE)})
        # A horizontal depth ramp is monotonic along every row and stays in range.
        ramp = [vertex(-1, -1, z=0), vertex(1, -1, z=1), vertex(-1, 1, z=0), vertex(1, 1, z=1)]
        py = self.draw(ramp, [(0, 1, 3), (0, 3, 2)])
        self.assertEqual(py['pixels'], 320 * 240)
        for y in range(0, 240, 17):
            row = py['zbuf'][y * 320:(y + 1) * 320]
            self.assertEqual(row, sorted(row))
            self.assertLess(row[0], 200)
            self.assertGreater(row[-1], M.Z_MAX - 200)
        # A sliver: huge gradients saturate and clamp instead of wrapping.
        py = self.draw([vertex(-1, 0, z=0, r=0), vertex(1, 0.001, z=1, r=255), vertex(1, 0.0005, z=0, r=0)],
                       [(0, 1, 2), (0, 2, 1)])
        self.assertIsNone(py['fault'])

    def test_partial_batch_and_existing_buffers(self):
        verts = [vertex(-.5, -.5), vertex(.5, -.5), vertex(0, .5), vertex(.2, .2, z=.1), vertex(.9, .2, z=.1)]
        fb = bytes(range(256)) * 300
        zbuf = [(i * 7) & 0xffff for i in range(76800)]
        py = self.draw(verts, [(0, 1, 2), (3, 4, 2)], fb=fb, zbuf=zbuf)
        self.assertGreater(py['zfail'], 0)

    def test_zbase_is_validated_by_both_references(self):
        verts = [vertex(-.5, -.5), vertex(.5, -.5), vertex(0, .5)]
        for zbase, ok in ((0x80040000, True), (0x80400000 - M.Z_BYTES, True), (0x80000000, True),
                          (0x80040002, False), (0x7ffffffc, False), (0x80400000 - M.Z_BYTES + 4, False), (0, False)):
            py = self.draw(verts, [(0, 1, 2)], zbase=zbase)
            self.assertEqual(py['fault'] is None, ok, hex(zbase))

    def test_words_past_the_program_read_as_end(self):
        # The oracle pads with zeros (END); the C reference is told the length, and the
        # device's window is zero-filled by g3d_load_program. A program without END runs into them.
        py = self.agree([encode('LDI', 1)] * 3, [0] * 32, [[0] * 8] * 4, [])
        self.assertIsNone(py['fault'])
        self.assertEqual(py['instructions'], 4)

    def test_parameter_and_index_faults_touch_nothing(self):
        verts = [vertex(-.5, -.5), vertex(.5, -.5), vertex(0, .5)]
        for tris, kw, reason in (([(0, 1, 3)], {}, M.E_INDEX), ([0x00020100 | 0xff000000], {}, None),
                                 ([], {'limit': 0}, M.E_PARAM), ([], {'limit': 0x10000}, M.E_PARAM),
                                 ([(0, 1, 2)] * 65, {}, M.E_PARAM), ([], {'vcount': 0}, M.E_PARAM),
                                 ([], {'vcount': 33}, M.E_PARAM)):
            vs = verts if kw.get('vcount', 3) <= 3 else verts + [vertex(0, 0)] * 30
            py = self.draw(vs, tris, **kw)
            self.assertEqual(py['fault'].reason if py['fault'] else None, reason)
            if reason:
                self.assertEqual((py['pixels'], py['transfers'], py['instructions']), (0, 0, 0))


class Precision(unittest.TestCase):
    def test_fixed_point_against_float_reference(self):
        """Documented tolerance (docs/rv32-3d.md "Precision"): the fixed-function formats
        change at most 3% of the cube's pixels relative to a double-precision pipeline."""
        inputs, triangles = cube()
        worst = 0
        for name, text in SHADERS.items():
            program = assemble(text)
            for angle in (0.3, 1.1, 2.5, 4.0):
                consts = constants(angle, frame=17)
                fixed = render(program, consts, inputs, 24, triangles)
                outputs, _ = run_shader(program, consts, inputs, 24, 4096)
                ref = render_float(outputs, triangles)
                covered = sum(1 for p in ref if p) or 1
                differ = sum(a != b for a, b in zip(fixed['fb'], ref))
                worst = max(worst, differ / covered)
        self.assertLess(worst, 0.03)


if __name__ == '__main__':
    unittest.main()
