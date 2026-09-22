"""A2 protocol replay, independent kernel oracle, and whole-machine checks."""
import ctypes as C
import os
from pathlib import Path
import random
import re
import subprocess
import unittest

from tools import simd4_model as model
from tools.rv32_simd4_kernels import kernels
from tools.rv32_rtl import (run_rtl, run_emulator, check_passed, compare_backends,
                            uses_accelerator, write_image, diff_traces)
from tools.rv32_asm import LI, LW, LH, LB, SW, SH, SB, JALR, FINISH

BASE, PROGRAM, DATA = 0x20004000, 0x20005000, 0x20006000
OUT = Path('build/rv32/simd4-tests')


class Device:
    def __init__(self, lib):
        self.lib = lib
        lib.native_init()
        self.rows = []
        self.step(reset=True)

    def get(self, index):
        return self.lib.native_get(index)

    def read(self, offset):
        value = C.c_uint32()
        assert self.lib.native_access(BASE + offset, 4, 0, C.byref(value))
        return value.value

    def snapshot(self):
        return (sum(self.get(i) << (16*i) for i in range(16)) |
                sum(self.get(16+i) << (256+32*i) for i in range(4)) |
                (self.get(20) << 384) | (self.get(21) << 392))

    def step(self, address=None, value=None, *, width=4, hold=False, reset=False, expected=None, error=False):
        write = value is not None
        data = C.c_uint32(value or 0)
        failed = False
        if reset:
            self.lib.native_reset()
        elif address is not None:
            failed = not self.lib.native_access(address, width, write, C.byref(data))
            assert failed == error, (hex(address), value, error)
            if expected is not None:
                assert not write and not failed and data.value == expected, (hex(address), data.value, expected)
        self.lib.native_tick(hold)
        # Reproduce the CPU's shifted strobes/data, including rejected subword accesses.
        shift = ((address or 0) & 3) * 8
        strb = (((1 << width)-1) << ((address or 0) & 3)) & 15
        wdata = ((value or 0) << shift) & 0xffffffff
        self.rows.append([int(reset), int(address is not None), int(write), address or 0,
                          strb, wdata, int(hold), int(failed), data.value,
                          self.read(4), self.read(8), *[self.read(i) for i in (12,16,20,24)], self.snapshot()])
        return data.value

    def load(self, program, data):
        for i, (p, d) in enumerate(zip(program, data)):
            self.step(PROGRAM+4*i, p)
            self.step(DATA+4*i, d)

    def launch(self, entry=0):
        self.step(BASE+8, entry)
        self.step(BASE, 1)
        assert self.read(4) == 1 and self.read(12) == 0

    def until(self, predicate, limit=1000, **kwargs):
        for _ in range(limit):
            if predicate(): return
            self.step(**kwargs)
        raise AssertionError('condition never reached')

    def complete(self, seed=0):
        rng = random.Random(seed)
        transfers, snapshots = [], []
        for _ in range(10000):
            if not self.read(4) & 1: return transfers, snapshots
            hold = seed != 0 and rng.randrange(3) == 0
            before = self.read(24)
            if self.get(22) == 3 and not hold:
                insn, lane = self.get(23), self.get(24)
                ra, rd = (insn >> 20) & 3, (insn >> 22) & 3
                address = (self.get(lane*4+ra) + (insn & 65535)) & 255
                write = insn >> 24 == model.STORE
                data = self.get(lane*4+rd) if write else self.lib.native_data(address)
                transfers.append((int(write) << 24) | (address << 16) | data)
            self.step(hold=hold)
            if self.read(24) != before: snapshots.append(self.snapshot())
        raise AssertionError('device never completed')


class SimdIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)
        cls.backend = os.environ.get('A2_SIM', 'icarus')
        cls.prefix = OUT / cls.backend
        cls.prefix.mkdir(exist_ok=True)
        native = cls.prefix / 'native.so'
        subprocess.run([os.environ.get('HOST_CC', 'cc'), '-shared', '-fPIC', '-std=c11', '-O2',
                        '-Wall', '-Wextra', '-Werror', '-Itools', 'tests/rv32_simd4_native.c',
                        'tools/rv32_simd4.c', '-o', str(native)], check=True)
        cls.lib = C.CDLL(str(native.resolve()))
        cls.lib.native_access.argtypes = [C.c_uint32, C.c_int, C.c_int, C.POINTER(C.c_uint32)]
        cls.lib.native_get.restype = cls.lib.native_data.restype = C.c_uint32
        cls.protocol = Path('build/rv32/simd4-protocol.vvp') if cls.backend == 'icarus' else Path('build/verilator-rv32-simd4/protocol')
        cls.soc = 'build/rv32/rv32_tb.vvp' if cls.backend == 'icarus' else 'build/verilator-rv32/rv32_sim'

    def replay(self, device, name):
        fixture = self.prefix / (name+'.txt')
        fixture.write_text(''.join(' '.join(f'{n:x}' for n in row)+'\n' for row in device.rows))
        cmd = ['vvp', str(self.protocol)] if self.backend == 'icarus' else [str(self.protocol), '+verilator+quiet']
        cmd += [f'+fixture={fixture}', f'+rows={len(device.rows)}']
        if name == 'reset': cmd += [f'+wave={self.prefix / "protocol.vcd"}']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=60)
        (self.prefix / (name+'.log')).write_text(result.stdout+result.stderr)
        self.assertEqual(result.returncode, 0, result.stdout+result.stderr)
        self.assertRegex(result.stdout, rf'(?m)^PASS protocol {len(device.rows)}$')
        self.assertNotRegex(result.stdout+result.stderr, r'(?i)warning|error|fatal')
        return cmd, fixture

    def test_access_contract(self):
        d = Device(self.lib)
        for address in (PROGRAM, PROGRAM+1020, DATA, DATA+1020):
            d.step(address, 0xfedcba98)
            d.step(address, expected=0xba98 if address >= DATA else 0xfedcba98)
            for width in (1, 2):
                for offset in range(0, 4, width):
                    d.step(address+offset, width=width, error=True)
                    d.step(address+offset, 0, width=width, error=True)
            d.step(address, expected=0xba98 if address >= DATA else 0xfedcba98)
        for address in (BASE-4, BASE+28, BASE+32, PROGRAM-4, PROGRAM+1024, DATA-4, DATA+1024):
            d.step(address, error=True)
            d.step(address, 1, error=True)
        d.step(BASE, error=True)
        for command in (0,3,0xffffffff): d.step(BASE, command, error=True)
        d.step(BASE+8, 255)
        d.step(BASE+8, 256, error=True)
        d.step(BASE+8, expected=255)
        for offset in (4,12,16,20,24): d.step(BASE+offset, 9, error=True)
        for offset in range(0, 28, 4):
            for width in (1,2):
                d.step(BASE+offset, width=width, error=True)
                d.step(BASE+offset, 2, width=width, error=True)
        d.step(PROGRAM+1020, 0) # entry 255: fetch wraps to zero, HLT
        d.step(BASE, 1)
        d.complete()
        d.step(BASE+4, expected=2)
        self.assertEqual(d.get(20), 0)
        d.step(BASE, 2)
        d.step(DATA, expected=0xba98)
        self.replay(d, 'access')

    def test_kernels_and_independent_oracle(self):
        rng = random.Random(20260922)
        images = list(kernels().items())
        # Exercise every arithmetic opcode and all illegal instruction bytes,
        # with nonzero accumulators before each fault.
        prefix = [model.word(model.LDI, rd=0, imm=0xffff), model.word(model.LDI, rd=1, imm=0x8000),
                  model.word(model.MAC, ra=0, rb=1)]
        images += [(f'illegal-{op:02x}', model.image(prefix+[model.word(op)])) for op in range(14,256)]
        images += [('loop-zero', model.image(prefix+[model.word(model.LOOP)])),
                   ('setloop-zero', model.image(prefix+[model.word(model.SETLOOP)]))]
        words = prefix + [model.word(model.MACU, ra=1, rb=1)] * 5
        for shift in range(32): words.append(model.word(model.RDA, rd=shift%4, imm=shift))
        words += [model.word(model.MUL, rd=0, ra=1, rb=1), model.word(model.CLRA), model.word(model.HLT)]
        images += [('arithmetic', model.image(words))]
        d = Device(self.lib)
        for name, program in images:
            with self.subTest(kernel=name):
                data = [rng.choice([1, 0x7fff, 0x8000, 0xffff, rng.randrange(65536)]) for _ in range(256)]
                oracle = model.execute(program, data)
                d.load(program, data)
                d.launch()
                transfers, snapshots = d.complete(seed=7)
                self.assertEqual(transfers, oracle.transfers)
                self.assertEqual(snapshots, [r & ((1<<408)-1) for r in oracle.retirements])
                self.assertEqual([self.lib.native_data(i) for i in range(256)], oracle.memory)
                self.assertEqual(d.snapshot(), oracle.final_state)
                self.assertEqual(d.read(4), 6 if oracle.fault else 2)
                self.assertEqual(d.read(12), oracle.base_cycles+d.read(16))
                for i, value in enumerate(oracle.memory): d.step(DATA+4*i, expected=value)
        self.replay(d, 'kernels')

    def test_busy_and_reset(self):
        d = Device(self.lib)
        program = model.image([model.word(model.LANE, rd=0), model.word(model.LDI, rd=1, imm=0xabcd),
                               model.word(model.STORE, rd=1, ra=0, imm=128), model.word(model.HLT)])
        d.load(program, [0x4321]*256)
        d.launch()
        d.until(lambda: d.get(22) == 3)
        for address in (PROGRAM, PROGRAM+1020, DATA, DATA+1020):
            d.step(address, hold=True, error=True)
            d.step(address, 1, hold=True, error=True)
        d.step(BASE, 1, hold=True, error=True)
        d.step(BASE+8, 1, hold=True, error=True)
        d.step(BASE+8, hold=True, expected=0)
        d.step(BASE+4, hold=True, expected=1)
        self.assertEqual(d.read(20), 0)
        # Cancel an unaccepted store, using software reset on an otherwise ready edge.
        d.step(BASE, 2)
        d.step(DATA+512, expected=0x4321)
        d.launch()
        d.until(lambda: d.get(22) == 3)
        d.step() # accept lane zero only
        self.assertEqual(d.read(20), 1)
        d.step(hold=True)
        d.step(reset=True) # global reset on an otherwise ready edge
        for i in range(4): d.step(DATA+512+4*i, expected=0xabcd if i == 0 else 0x4321)
        d.launch()
        d.complete()
        for i in range(4): d.step(DATA+512+4*i, expected=0xabcd)
        # Partial load must not survive reset in the lane register bank.
        program[2] = model.word(model.LOAD, rd=1, ra=0, imm=128)
        d.load(program, [0x7654]*256)
        d.launch()
        d.until(lambda: d.get(22) == 3)
        d.step()
        self.assertEqual(d.get(1), 0x7654)
        d.step(hold=True)
        d.step(BASE, 2)
        self.assertEqual(d.snapshot(), 0)
        d.launch()
        d.complete()
        self.replay(d, 'reset')

    def test_nonempty_fixture_guard(self):
        d = Device(self.lib)
        cmd, fixture = self.replay(d, 'guard')
        fixture.write_text('')
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Incomplete fixture', result.stdout+result.stderr)

    def test_result_comparison_detection(self):
        self.assertTrue(uses_accelerator(['1 80000000 00000000 x1=1 mem[20004004]->00000001/4']))
        self.assertFalse(uses_accelerator(['1 80000000 00000000 trap 5 20004004']))
        self.assertFalse(uses_accelerator(['1 80000000 00000000 mem[20005000]<-00000000/4']))

    def test_soc_fault_edges(self):
        # Each image deliberately double-faults (mtvec remains zero); compare
        # the first trap literally and every retirement on both backends.
        cases = []
        for base in (BASE, PROGRAM, DATA):
            cases += [(base, LW(2,1,0), 5)] if base == BASE else []
            cases += [(base, op(2,1,0), cause) for op,cause in ((LH,5),(LB,5),(SH,7),(SB,7))]
            cases += [(base+2, LW(2,1,0), 4), (base+2, SW(2,1,0), 6), (base,JALR(0,1,0),1)]
        for address in (BASE+28, BASE+32, PROGRAM-4, PROGRAM+1024, DATA-4, DATA+1024):
            cases += [(address,LW(2,1,0),5),(address,SW(2,1,0),7)]
        for i,(address,op,cause) in enumerate(cases):
            with self.subTest(address=hex(address), cause=cause):
                words = LI(1,address)+[op]+FINISH()
                hexpath, binary = write_image(words, self.prefix, 'fault')
                emu = run_emulator('build/rv32/rv32emu', binary, self.prefix/'fault.emu.trace')
                rtl = run_rtl(self.soc, hexpath, self.prefix/'fault.rtl.trace', seed=7)
                self.assertIsNone(diff_traces(rtl.trace, emu.trace))
                self.assertEqual(rtl.noise, '')
                self.assertEqual(emu.halt['halt'], 'double-fault')
                self.assertIn(f'trap {cause} {address:08x}', emu.trace[3 if cause == 1 else 2])

    def test_firmware(self):
        binary = Path('build/rv32/simdcheck.bin')
        emu = run_emulator('build/rv32/rv32emu', binary, self.prefix/'firmware.emu.trace')
        check_passed(emu)
        self.assertEqual(emu.console, 'vector OK\nmatrix signed/unsigned OK\nrecovery OK\nPASS A2\n')
        for stall, seed, simd_stall, simd_seed in ((0,None,0,None),(3,None,2,None),(None,7,None,19)):
            rtl = run_rtl(self.soc, 'build/rv32/simdcheck.hex', self.prefix/'firmware.rtl.trace', stall=stall, seed=seed,
                          simd_stall=simd_stall, simd_seed=simd_seed)
            check_passed(rtl)
            self.assertIsNone(compare_backends(rtl, emu, 'results'))


if __name__ == '__main__':
    unittest.main()
