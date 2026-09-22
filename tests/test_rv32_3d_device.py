"""G2 emulator device against the Python oracle, through the CPU's access path."""
import ctypes as C
import os
from pathlib import Path
import random
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import rv32_g3d_model as M  # noqa: E402
from tools.rv32_g3d_model import assemble, render  # noqa: E402
from tools.rv32_g3d_scene import SHADERS, constants, cube  # noqa: E402
from tools.rv32_g3d_scene import passthrough, random_program, vertex  # noqa: E402

BUILD = ROOT / 'build/g3d'
RAM_SIZE = 0x400000
ZBASE = 0x80040000


def build_device(extra=()):
    BUILD.mkdir(parents=True, exist_ok=True)
    out = BUILD / ('libg3ddev-san.dylib' if extra else 'libg3ddev.dylib')
    subprocess.run([os.environ.get('HOST_CC', 'cc'), '-shared', '-fPIC', '-O2', '-std=c11', '-Wall', '-Wextra',
                    '-Werror', *extra, '-o', str(out), str(ROOT / 'tools/rv32_g3d.c'),
                    str(ROOT / 'tests/rv32_g3d_native.c')], check=True)
    lib = C.CDLL(str(out))
    lib.native_g3d_size.restype = C.c_size_t
    lib.g3d_access.restype = C.c_bool
    lib.g3d_access.argtypes = [C.c_void_p, C.c_uint32, C.c_int, C.c_bool, C.POINTER(C.c_uint32), C.c_bool]
    lib.g3d_tick.argtypes = [C.c_void_p, C.c_void_p, C.c_uint32, C.c_void_p, C.c_bool]
    lib.g3d_z_locked.restype = C.c_bool
    lib.g3d_z_locked.argtypes = [C.c_void_p, C.c_uint32, C.c_int]
    return lib


class Device:
    def __init__(self, lib):
        self.lib = lib
        self.state = C.create_string_buffer(lib.native_g3d_size())
        self.ram = C.create_string_buffer(RAM_SIZE)
        self.fb = C.create_string_buffer(76800)

    def write(self, off, value, other_busy=False):
        v = C.c_uint32(value & 0xffffffff)
        return self.lib.g3d_access(self.state, off, 4, True, C.byref(v), other_busy)

    def read(self, off):
        v = C.c_uint32(0)
        ok = self.lib.g3d_access(self.state, off, 4, False, C.byref(v), False)
        return v.value if ok else None

    def tick(self, hold=False):
        self.lib.g3d_tick(self.state, self.ram, RAM_SIZE, self.fb, hold)

    def load(self, program, consts, inputs, triangles, vcount=None, tcount=None, limit=4096, zbase=ZBASE):
        for i, w in enumerate(program):
            assert self.write(M.G3D_PROGRAM + 4 * i, w)
        for i, w in enumerate(consts):
            assert self.write(M.G3D_CONST + 4 * i, w)
        for v, row in enumerate(inputs):
            for s, w in enumerate(row):
                assert self.write(M.G3D_VERTEX + 32 * v + 4 * s, w)
        for t, tri in enumerate(triangles):
            assert self.write(M.G3D_TRIANGLE + 4 * t, tri if isinstance(tri, int) else tri[0] | tri[1] << 8 | tri[2] << 16)
        for off, value in ((M.G3D_VCOUNT, len(inputs) if vcount is None else vcount),
                           (M.G3D_TCOUNT, len(triangles) if tcount is None else tcount),
                           (M.G3D_ZBASE, zbase), (M.G3D_LIMIT, limit)):
            assert self.write(off, value)

    def run(self, command=M.G3D_START, hold=lambda tick: False, budget=20_000_000):
        assert self.write(M.G3D_COMMAND, command)
        self.tick()   # the command's own tick
        for tick in range(budget):
            if self.read(M.G3D_STATUS) != M.G3D_BUSY:
                return self.read(M.G3D_STATUS)
            self.tick(hold(tick))
        raise AssertionError('device did not finish')

    def counters(self):
        return {name: self.read(getattr(M, 'G3D_' + name.upper())) for name in
                ('instructions', 'transfers', 'divides', 'pixels', 'zfail', 'culled', 'cycles', 'stalls')}

    def zbuf(self, zbase=ZBASE):
        base = zbase - 0x80000000
        raw = self.ram.raw[base:base + M.Z_BYTES]
        return [raw[2 * i] | raw[2 * i + 1] << 8 for i in range(76800)]


def hold_pattern(seed):
    if seed is None:
        return lambda tick: False
    rng = random.Random(seed)
    return lambda tick: rng.random() < 0.3


class EmulatorDevice(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.lib = build_device()

    def fresh(self, zbase=ZBASE):
        d = Device(self.lib)
        if M.valid_zbase(zbase):
            d.ram[zbase - 0x80000000:zbase - 0x80000000 + M.Z_BYTES] = b'\xff' * M.Z_BYTES
        return d

    def agree(self, program, consts, inputs, triangles, limit=4096, seed=None, vcount=None, zbase=ZBASE):
        d = self.fresh(zbase)
        d.load(program, consts, inputs, triangles, vcount=vcount, limit=limit, zbase=zbase)
        status = d.run(hold=hold_pattern(seed))
        tris = [t if not isinstance(t, int) else (t & 255, t >> 8 & 255, t >> 16 & 255) for t in triangles]
        py = render(program, consts, inputs, len(inputs) if vcount is None else vcount, tris, limit, zbase=zbase)
        fault = py['fault']
        self.assertEqual(status, M.G3D_FAULT if fault else M.G3D_DONE)
        self.assertEqual(d.read(M.G3D_ERROR), fault.reason if fault else 0)
        if fault and fault.reason not in (M.E_PARAM, M.E_INDEX):
            self.assertEqual(d.read(M.G3D_FAULT_PC), fault.pc | fault.batch << 8)
        got = d.counters()
        if seed is None:
            self.assertEqual(got['stalls'], 0)
        for name in ('instructions', 'transfers', 'divides', 'pixels', 'zfail', 'culled'):
            self.assertEqual(got[name], py[name], name)
        self.assertEqual(got['cycles'], py['cycles'] + got['stalls'])
        self.assertEqual(d.fb.raw, bytes(py['fb']))
        if M.valid_zbase(zbase):
            self.assertEqual(d.zbuf(zbase), py['zbuf'])
        return d, py

    def test_cube_every_shader_with_and_without_holds(self):
        inputs, triangles = cube()
        for name, text in SHADERS.items():
            for angle, seed in ((0.4, None), (1.7, 5), (3.3, None)):
                d, py = self.agree(assemble(text), constants(angle, frame=int(angle * 30)), inputs, triangles, seed=seed)
                self.assertIsNone(py['fault'])
                if seed is not None:
                    self.assertGreater(d.read(M.G3D_STALLS), 0)

    def test_random_programs_and_faults(self):
        rng = random.Random(99)
        for trial in range(120):
            text = '\n'.join(random_program(rng) + ['END'])
            try:
                words = assemble(text)
            except ValueError:
                continue
            vcount = rng.randrange(1, 33)
            self.agree(words, [rng.getrandbits(32) for _ in range(32)],
                       [[rng.getrandbits(32) for _ in range(8)] for _ in range(vcount)], [],
                       limit=rng.choice([60, 400, 4096]))

    def test_corpus_directed_and_raster_jobs(self):
        # The directed fault jobs (every ERROR, including ILLEGAL, MISMATCH and PC=128) and the
        # raster edge cases, through the CPU access path with holds on alternate jobs.
        from tools.rv32_g3d_corpus import directed_faults, raster_cases
        from tools.rv32_g3d_model import unpack_triangle
        seen = set()
        for i, scene in enumerate(directed_faults() + raster_cases()):
            name, program, consts, inputs, triangles, vcount, limit, zbase = scene
            if vcount > M.VMAX or len(triangles) > M.TMAX or isinstance(triangles[:1] and triangles[0], int):
                continue   # beyond the windows or raw words: the sanitizer corpus runs these
            _, py = self.agree(program, consts, inputs, triangles, limit=limit, seed=i if i % 2 else None,
                               vcount=vcount, zbase=zbase)
            seen.add(py['fault'].reason if py['fault'] else 0)
        self.assertEqual(seen, {0, M.E_PARAM, M.E_ILLEGAL, M.E_OVERFLOW, M.E_MISMATCH, M.E_LIMIT, M.E_PC})

    def test_raster_cases(self):
        verts = [vertex(-.9, -.8, z=.2, r=250, g=10), vertex(.8, -.7, z=.9, b=200), vertex(.1, .9, z=.5, g=90),
                 vertex(-4, -1), vertex(4, -1), vertex(0, 4), vertex(0, 0, w=-1), vertex(.3, .3, z=.1)]
        tris = [(0, 1, 2), (0, 2, 1), (3, 4, 5), (0, 1, 6), (7, 0, 1), (1, 1, 2)]
        for seed in (None, 11):
            self.agree(passthrough(), [0] * 32, verts, tris, seed=seed)

    def test_parameter_and_index_faults(self):
        verts = [vertex(-.5, -.5), vertex(.5, -.5), vertex(0, .5)]
        for kw in ({'limit': 0}, {'limit': 0x10000}, {'vcount': 0}, {'vcount': 33}, {'zbase': ZBASE + 2},
                   {'zbase': 0x7ffffffc}, {'zbase': 0x80400000 - M.Z_BYTES + 4}):
            d, py = self.agree(passthrough(), [0] * 32, verts, [(0, 1, 2)], **kw)
            self.assertEqual(py['fault'].reason, M.E_PARAM)
        self.agree(passthrough(), [0] * 32, verts, [(0, 1, 2), (0, 1, 3)])
        self.agree(passthrough(), [0] * 32, verts, [(0, 1, 2)], zbase=0x80400000 - M.Z_BYTES)

    def test_clear_z(self):
        d = Device(self.lib)
        self.assertTrue(d.write(M.G3D_ZBASE, ZBASE))
        self.assertEqual(d.run(M.G3D_CLEAR_Z, hold=hold_pattern(3)), M.G3D_DONE)
        self.assertEqual(d.zbuf(), [0xffff] * 76800)
        c = d.counters()
        self.assertEqual((c['transfers'], c['cycles'] - c['stalls']), (M.Z_BYTES // 4, M.clear_cycles()))
        self.assertEqual(d.ram.raw[ZBASE - 0x80000000 - 1], 0)
        self.assertEqual(d.ram.raw[ZBASE - 0x80000000 + M.Z_BYTES], 0)
        d = Device(self.lib)
        self.assertTrue(d.write(M.G3D_ZBASE, ZBASE + 1))
        self.assertEqual(d.run(M.G3D_CLEAR_Z), M.G3D_FAULT)
        self.assertEqual((d.read(M.G3D_ERROR), d.read(M.G3D_TRANSFERS)), (M.E_PARAM, 0))

    def test_ownership_and_commands(self):
        d = self.fresh()
        d.load(passthrough(), [0] * 32, [vertex(-.5, -.5), vertex(.5, -.5), vertex(0, .5)], [(0, 1, 2)])
        self.assertFalse(d.write(M.G3D_COMMAND, M.G3D_START, other_busy=True))   # G1 owns the engine port
        self.assertEqual(d.read(M.G3D_STATUS), M.G3D_IDLE)
        self.assertTrue(d.write(M.G3D_COMMAND, M.G3D_START))
        d.tick()
        busy = [(M.G3D_PROGRAM, True), (M.G3D_CONST + 4, True), (M.G3D_VERTEX, True), (M.G3D_TRIANGLE, True),
                (M.G3D_VCOUNT, True), (M.G3D_COMMAND, True)]
        for off, _ in busy:
            self.assertFalse(d.write(off, M.G3D_START if off == M.G3D_COMMAND else 1), hex(off))
        self.assertFalse(d.write(M.G3D_COMMAND, M.G3D_CLEAR_Z))
        self.assertIsNone(d.read(M.G3D_PROGRAM))       # windows are engine-owned while busy
        self.assertEqual(d.read(M.G3D_VCOUNT), 3)       # parameters stay readable
        self.assertTrue(self.lib.g3d_z_locked(d.state, ZBASE, 1))
        self.assertTrue(self.lib.g3d_z_locked(d.state, ZBASE + M.Z_BYTES - 4, 4))
        self.assertFalse(self.lib.g3d_z_locked(d.state, ZBASE + M.Z_BYTES, 4))
        self.assertFalse(self.lib.g3d_z_locked(d.state, ZBASE - 4, 4))
        for bad in (0x30, 0x3c, 0x50, 0x100, 0x480, 0xa00, 0x1400, 0x1900, 0x1ffc):
            self.assertIsNone(d.read(bad), hex(bad))
        self.assertFalse(d.write(M.G3D_STATUS, 0))
        self.assertFalse(d.write(M.G3D_COMMAND, 7))
        v = C.c_uint32(0)
        self.assertFalse(self.lib.g3d_access(d.state, M.G3D_STATUS, 2, False, C.byref(v), False))
        self.assertFalse(self.lib.g3d_access(d.state, M.G3D_STATUS + 2, 4, False, C.byref(v), False))
        # RESET is always legal, clears parameters and counters, and keeps the windows.
        self.assertTrue(d.write(M.G3D_COMMAND, M.G3D_RESET))
        d.tick()
        self.assertEqual([d.read(o) for o in (M.G3D_STATUS, M.G3D_VCOUNT, M.G3D_CYCLES)], [M.G3D_IDLE, 0, 0])
        self.assertEqual(d.read(M.G3D_PROGRAM), passthrough()[0])
        self.assertFalse(self.lib.g3d_z_locked(d.state, ZBASE, 4))

    def test_reset_mid_frame_keeps_accepted_writes(self):
        inputs, triangles = cube()
        d = self.fresh()
        d.load(assemble(SHADERS['diffuse']), constants(1.0), inputs, triangles)
        d.write(M.G3D_COMMAND, M.G3D_START)
        for _ in range(9000):
            d.tick()
        written = d.read(M.G3D_PIXELS)
        self.assertGreater(written, 0)
        d.write(M.G3D_COMMAND, M.G3D_RESET)
        d.tick()
        fb = d.fb.raw
        for _ in range(100):
            d.tick()
        self.assertEqual(d.fb.raw, fb)
        self.assertEqual(sum(1 for p in fb if p), written)


if __name__ == '__main__':
    unittest.main()
