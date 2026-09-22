"""CPU-visible G2 ownership, exclusion and fault contracts, on both backends."""
import os
from pathlib import Path
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import rv32_asm as asm  # noqa: E402
from tools.rv32_asm import LI, LW, SW, SB, SH, ADDI, BNE, CSRRS, CSRRW, MRET, FINISH, RAM, FB, DISPLAY  # noqa: E402
from tools.rv32_rtl import write_image, run_rtl, run_emulator, check_passed, compare_backends, uses_accelerator  # noqa: E402
from tools.rv32_g3d_header import passthrough, vertex  # noqa: E402

BASE = asm.G3D_BASE
ZBASE = 0x80040000


def handler_at(words, index):
    """Pad to `index` and append a trap handler that skips the faulting instruction."""
    return words + [0] * (index - len(words)) + [CSRRS(6, 0x341, 0), ADDI(6, 6, 4), CSRRW(0, 0x341, 6), MRET()]


def store_words(base_reg, offset, values):
    """Store words at base + offset; offsets beyond a 12-bit immediate go through x11."""
    out = []
    if offset + 4 * len(values) > 2047:
        out += LI(11, (BASE if base_reg == 1 else asm.GPU_BASE if base_reg == 8 else BASE) + offset)
        base_reg, offset = 11, 0
    for i, v in enumerate(values):
        out += LI(4, v & 0xffffffff) + [SW(4, base_reg, offset + 4 * i)]
    return out


def screen_job():
    """A guard-band triangle covering all 76,800 pixels: busy for hundreds of thousands of ticks."""
    verts = [vertex(-4, -1, z=.9), vertex(4, -1, z=.9), vertex(0, 4, z=.9)]
    words = LI(1, BASE)
    words += store_words(1, asm.G3D_PROGRAM, passthrough())
    words += store_words(1, asm.G3D_VERTEX, [w for v in verts for w in v])
    words += store_words(1, asm.G3D_TRIANGLE, [0x00020100])
    words += store_words(1, asm.G3D_VCOUNT, [3, 1, ZBASE, 4096])
    return words


class ShaderSoC(unittest.TestCase):
    def setUp(self):
        self.out = Path('build/g3d/soc-' + os.environ.get('G2_SIM', 'icarus'))
        self.out.mkdir(parents=True, exist_ok=True)
        self.sim = 'build/verilator-rv32/rv32_sim' if os.environ.get('G2_SIM') == 'verilator' else 'build/rv32/rv32_tb.vvp'

    def run_pair(self, words, name, **kw):
        hp, bp = write_image(words, self.out, name)
        emu = run_emulator('build/rv32/rv32emu', bp, self.out / (name + '.emu.trace'))
        rtl = run_rtl(self.sim, hp, self.out / (name + '.rtl.trace'), **kw)
        check_passed(emu)
        check_passed(rtl)
        self.assertIsNone(compare_backends(rtl, emu, 'results'))
        return emu, rtl

    @staticmethod
    def traps(run):
        return [line.split()[-2:] for line in run.trace if ' trap ' in line]

    def test_busy_ownership(self):
        words = screen_job() + LI(2, FB) + LI(3, ZBASE) + LI(7, DISPLAY) + LI(8, asm.GPU_BASE) + LI(10, ZBASE + 153598)
        words += LI(12, BASE + asm.G3D_PROGRAM)
        words += LI(4, RAM + 4096) + [CSRRW(0, 0x305, 4)]
        words += LI(4, 1) + [SW(4, 1, asm.G3D_COMMAND)]
        # Forbidden while busy: framebuffer, PRESENT, G1 START, the windows, parameters,
        # a second launch, and any store into the depth buffer.
        words += [LW(5, 2, 0), SW(4, 2, 0), SW(4, 7, 0), SW(4, 8, 0), SW(4, 12, 0),
                  LW(5, 1, asm.G3D_CONST), SW(4, 1, asm.G3D_VCOUNT), SW(4, 1, asm.G3D_COMMAND),
                  SW(4, 3, 0), SH(4, 10, 0), SB(4, 3, 77)]
        words += LI(4, 3) + [SW(4, 1, asm.G3D_COMMAND)]
        # Allowed while busy: registers, parameters, depth reads, RAM just outside the buffer.
        words += [LW(5, 1, asm.G3D_STATUS), LW(5, 1, asm.G3D_VCOUNT), LW(5, 3, 0), SB(4, 3, -1)]
        # Spin about 2,000 instructions so the engine is rasterizing (and stalling) before STALLS is read.
        words += LI(6, ZBASE + 153600) + [SB(4, 6, 0), ADDI(13, 0, 1000), ADDI(13, 13, -1), BNE(13, 0, -4),
                                          LW(5, 1, asm.G3D_STALLS)]
        # RESET is always legal; afterwards everything is the CPU's again.
        words += LI(4, 2) + [SW(4, 1, asm.G3D_COMMAND), LW(5, 1, asm.G3D_STATUS), LW(5, 1, asm.G3D_VCOUNT),
                             LW(5, 2, 0), SW(4, 3, 0), LW(5, 3, 0), LW(5, 12, 0)]
        words += FINISH()
        words = handler_at(words, 1024)
        for suffix, timing in (('random', dict(seed=19, gpu_seed=31)), ('fixed', dict(stall=1, gpu_stall=2))):
            for backend, run in zip(('emulator', 'rtl'), self.run_pair(words, 'ownership-' + suffix, **timing)):
                expected = [(5, FB), (7, FB), (7, DISPLAY), (7, asm.GPU_BASE), (7, BASE + asm.G3D_PROGRAM),
                            (5, BASE + asm.G3D_CONST), (7, BASE + asm.G3D_VCOUNT), (7, BASE), (7, ZBASE),
                            (7, ZBASE + 153598), (7, ZBASE + 77), (7, BASE)]
                self.assertEqual(self.traps(run), [[str(c), f'{a:08x}'] for c, a in expected], backend)
                for address, value, width in ((BASE + asm.G3D_STATUS, 1, 4), (BASE + asm.G3D_VCOUNT, 3, 4),
                                              (ZBASE - 1, 3, 1), (ZBASE + 153600, 3, 1),
                                              (BASE + asm.G3D_STATUS, 0, 4), (BASE + asm.G3D_VCOUNT, 0, 4),
                                              (ZBASE, 2, 4), (BASE + asm.G3D_PROGRAM, passthrough()[0], 4)):
                    self.assertTrue(any(f'mem[{address:08x}]->{value:08x}/{width}' in l or
                                        f'mem[{address:08x}]<-{value:08x}/{width}' in l for l in run.trace),
                                    (backend, hex(address), value))
                stalls = [int(l.split('->')[1].split('/')[0], 16) for l in run.trace
                          if f'mem[{BASE + asm.G3D_STALLS:08x}]->' in l]
                self.assertEqual(len(stalls), 1)
                if backend == 'rtl':
                    self.assertGreater(stalls[0], 0)

    def test_engines_exclude_each_other(self):
        # G1 fills the whole screen; while it runs, G2 START and CLEAR_Z fault and G2 stays idle.
        words = LI(1, asm.GPU_BASE) + store_words(1, asm.GPU_PARAMS, [1, 0x1c, 0, 0, 0, 0, 0, 0, 320, 240])
        words += LI(9, BASE) + store_words(9, asm.G3D_ZBASE, [ZBASE])
        words += LI(4, RAM + 4096) + [CSRRW(0, 0x305, 4)]
        words += LI(4, 1) + [SW(4, 1, asm.GPU_COMMAND), SW(4, 9, asm.G3D_COMMAND)]
        words += LI(4, 3) + [SW(4, 9, asm.G3D_COMMAND), LW(5, 9, asm.G3D_STATUS)]
        # Once G1 is DONE, G2 may clear the depth buffer; poll each engine until DONE.
        words += [ADDI(6, 0, 2), LW(5, 1, asm.GPU_STATUS), BNE(5, 6, -4)]
        words += LI(4, 3) + [SW(4, 9, asm.G3D_COMMAND)]
        words += [LW(5, 9, asm.G3D_STATUS), BNE(5, 6, -4)]
        words += [LW(5, 9, asm.G3D_TRANSFERS)] + FINISH()
        words = handler_at(words, 1024)
        for run in self.run_pair(words, 'exclusion', stall=1, gpu_stall=1):
            self.assertEqual(self.traps(run), [['7', f'{BASE:08x}'], ['7', f'{BASE:08x}']])
            self.assertTrue(any(f'mem[{BASE + asm.G3D_STATUS:08x}]->00000000/4' in l for l in run.trace))
            self.assertTrue(any(f'mem[{BASE + asm.G3D_TRANSFERS:08x}]->{38400:08x}/4' in l for l in run.trace))

    def test_g1_start_faults_while_g2_runs(self):
        words = screen_job() + LI(8, asm.GPU_BASE) + store_words(8, asm.GPU_PARAMS, [1, 0x03, 0, 0, 0, 0, 0, 0, 4, 4])
        words += LI(4, RAM + 4096) + [CSRRW(0, 0x305, 4)]
        words += LI(4, 1) + [SW(4, 1, asm.G3D_COMMAND), SW(4, 8, asm.GPU_COMMAND), LW(5, 8, asm.GPU_STATUS)]
        words += LI(4, 2) + [SW(4, 1, asm.G3D_COMMAND)] + LI(4, 1) + [SW(4, 8, asm.GPU_COMMAND)] + FINISH()
        words = handler_at(words, 1024)
        for run in self.run_pair(words, 'g1-refused'):
            self.assertEqual(self.traps(run), [['7', f'{asm.GPU_BASE:08x}']])
            self.assertTrue(any(f'mem[{asm.GPU_BASE + asm.GPU_STATUS:08x}]->00000000/4' in l for l in run.trace))

    def test_register_faults(self):
        cases = [(asm.G3D_COMMAND, LW), (0x30, LW), (0x3c, LW), (0x50, LW), (0x100, LW), (0x480, LW), (0xa00, LW),
                 (0x1400, LW), (0x1900, LW), (0x1ffc, LW), (asm.G3D_STATUS, SW), (asm.G3D_STATUS, SB),
                 (asm.G3D_VCOUNT, SH), (asm.G3D_PROGRAM, SB)]
        for n, (off, access) in enumerate(cases):
            words = LI(1, BASE + off) + LI(4, RAM + 1024) + [CSRRW(0, 0x305, 4)] + LI(4, 7) + [access(5 if access == LW else 4, 1, 0)]
            words = handler_at(words + FINISH(), 256)
            for run in self.run_pair(words, f'fault{n}'):
                self.assertEqual(self.traps(run), [[str(5 if access == LW else 7), f'{BASE + off:08x}']], hex(off))

    def test_runner_contract(self):
        self.assertTrue(uses_accelerator([f'1 80000000 00000000 mem[{BASE + 4:08x}]->00000001/4']))
        self.assertFalse(uses_accelerator([f'1 80000000 00000000 mem[{BASE + asm.G3D_PROGRAM:08x}]<-00000001/4']))
        self.assertFalse(uses_accelerator([f'1 80000000 00000000 trap 5 {BASE + 4:08x}']))


if __name__ == '__main__':
    unittest.main()
