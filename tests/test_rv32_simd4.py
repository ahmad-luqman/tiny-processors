"""A2 protocol replay, independent kernel oracle, and whole-machine checks."""
import ctypes as C
import os
import json
from pathlib import Path
import random
import re
import subprocess
import sys
from unittest.mock import patch
import unittest

from tools import simd4_model as model
from tools.rv32_simd4_kernels import kernels
from tools.rv32_rtl import (run_rtl, run_emulator, check_passed, compare_backends,
                            uses_accelerator, write_image, diff_traces, Run)
from tools.rv32_asm import LI, LW, LH, LB, SW, SH, SB, JALR, FINISH, CSRRS, CSRRW, ADDI, MRET, RAM, ECALL
from tools.rv32_asm import (SIMD_BASE as BASE, SIMD_PROGRAM as PROGRAM, SIMD_DATA as DATA,
    SIMD_COMMAND as COMMAND, SIMD_STATUS as STATUS, SIMD_ENTRY as ENTRY,
    SIMD_CYCLES as CYCLES, SIMD_STALLS as STALLS, SIMD_TRANSFERS as TRANSFERS,
    SIMD_INSTRUCTIONS as INSTRUCTIONS, SIMD_BUSY as BUSY, SIMD_DONE as DONE,
    SIMD_FAULT as FAULT, SIMD_START as START, SIMD_RESET as RESET)

OUT = Path('build/rv32/simd4-tests')
# Inspection bridge indices in rv32_simd4_native.c; registers 0..15, accumulators 16..19.
PC, LOOP_COUNT, PHASE, INSTRUCTION, LANE, COMMAND_TICK = range(20, 26)
IDLE, FETCH, EXECUTE, MEMORY = range(4)
FIELD_WIDTHS = (1, 1, 1, 32, 4, 32, 1, 1, 32, 32, 32, 32, 32, 32, 32, 408, 26, 1)


def fixture_text(rows):
    """Refuse malformed/over-wide fields before the simulator can truncate them."""
    lines = []
    for row in rows:
        if len(row) != len(FIELD_WIDTHS):
            raise ValueError('fixture field count')
        if any(not isinstance(n, int) or not 0 <= n < (1 << bits)
               for n, bits in zip(row, FIELD_WIDTHS, strict=True)):
            raise ValueError('fixture field width')
        lines.append(' '.join(f'{n:x}' for n in row)+'\n')
    return ''.join(lines)


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
        if not self.lib.native_access(BASE + offset, 4, 0, C.byref(value)):
            raise AssertionError(f"native register read rejected: {offset:#x}")
        return value.value

    def snapshot(self):
        return model.snapshot([[self.get(lane*4+r) for r in range(4)] for lane in range(4)],
                              [self.get(16+lane) for lane in range(4)], self.get(PC), self.get(LOOP_COUNT))

    def transfer(self, hold):
        if self.get(PHASE) != MEMORY or hold or self.get(COMMAND_TICK): return None
        insn, lane = self.get(INSTRUCTION), self.get(LANE)
        ra, rd = (insn >> 20) & 3, (insn >> 22) & 3
        address = (self.get(lane*4+ra) + (insn & 65535)) & 255
        write = insn >> 24 == model.STORE
        data = self.get(lane*4+rd) if write else self.lib.native_data(address)
        return (int(write) << 24) | (address << 16) | data

    def step(self, address=None, value=None, *, width=4, hold=False, reset=False, expected=None, error=False):
        write = value is not None
        data = C.c_uint32(value or 0)
        failed = False
        if reset:
            self.lib.native_reset()
        elif address is not None:
            failed = not self.lib.native_access(address, width, write, C.byref(data))
            if failed != error:
                raise AssertionError((hex(address), value, error, failed))
            if expected is not None:
                if write or failed or data.value != expected:
                    raise AssertionError((hex(address), data.value, expected))
        transfer = self.transfer(hold)
        self.lib.native_tick(hold)
        # Reproduce the CPU's shifted strobes/data, including rejected subword accesses.
        shift = ((address or 0) & 3) * 8
        strb = (((1 << width)-1) << ((address or 0) & 3)) & 15
        wdata = ((value or 0) << shift) & 0xffffffff
        self.rows.append([int(reset), int(address is not None), int(write), address or 0,
                          strb, wdata, int(hold), int(failed), data.value,
                          self.read(STATUS), self.read(ENTRY), *[self.read(i) for i in (CYCLES,STALLS,TRANSFERS,INSTRUCTIONS)], self.snapshot(), 0 if transfer is None else (1<<25) | transfer, int(address is not None and not reset)])
        return data.value

    def load(self, program, data):
        for i, (p, d) in enumerate(zip(program, data, strict=True)):
            self.step(PROGRAM+4*i, p)
            self.step(DATA+4*i, d)

    def launch(self, entry=0):
        self.step(BASE+ENTRY, entry)
        self.step(BASE+COMMAND, START)
        if self.read(STATUS) != BUSY or self.read(CYCLES) != 0:
            raise AssertionError("launch did not clear status/cycles")

    def until(self, predicate, limit=1000, **kwargs):
        for _ in range(limit):
            if predicate(): return
            self.step(**kwargs)
        raise AssertionError('condition never reached')

    def complete(self, seed=0):
        rng = random.Random(seed)
        transfers, snapshots, stalls = [], [], 0
        for _ in range(10000):
            if not self.read(STATUS) & BUSY: return transfers, snapshots, stalls
            hold = seed != 0 and rng.randrange(3) == 0
            stalls += int(hold and self.get(PHASE) == MEMORY and not self.get(COMMAND_TICK))
            before = self.read(INSTRUCTIONS)
            transfer = self.transfer(hold)
            if transfer is not None: transfers.append(transfer)
            self.step(hold=hold)
            if self.read(INSTRUCTIONS) != before: snapshots.append(self.snapshot())
        raise AssertionError('device never completed')


class SimdIntegration(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        OUT.mkdir(parents=True, exist_ok=True)
        cls.backend = os.environ.get('A2_SIM', 'icarus')
        if cls.backend not in ('icarus', 'verilator'):
            raise ValueError(f'unknown A2_SIM: {cls.backend!r}')
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
        fixture.write_text(fixture_text(device.rows))
        cmd = ['vvp', str(self.protocol)] if self.backend == 'icarus' else [str(self.protocol), '+verilator+quiet']
        cmd += [f'+fixture={fixture}', f'+rows={len(device.rows)}']
        if name == 'reset': cmd += [f'+wave={self.prefix / "protocol.vcd"}']
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=120)
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
        d.step(BASE+ENTRY, 255)
        d.step(BASE+ENTRY, 256, error=True)
        d.step(BASE+ENTRY, expected=255)
        for offset in (4,12,16,20,24): d.step(BASE+offset, 9, error=True)
        for offset in range(0, 28, 4):
            for width in (1,2):
                d.step(BASE+offset, width=width, error=True)
                d.step(BASE+offset, 2, width=width, error=True)
        d.step(PROGRAM+1020, 0) # entry 255: fetch wraps to zero, HLT
        d.step(BASE+COMMAND, START)
        d.complete()
        d.step(BASE+STATUS, expected=2)
        self.assertEqual(d.get(PC), 0)
        d.step(BASE+COMMAND, RESET)
        self.assertEqual(d.read(ENTRY),0)
        d.step(DATA, expected=0xba98)
        d.step(PROGRAM, 0)
        d.launch()
        d.step() # FETCH -> EXECUTE of HLT
        d.step(DATA, error=True) # completion edge still belongs to the device
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
                transfers, snapshots, stalls = d.complete(seed=7)
                self.assertEqual(transfers, oracle.transfers)
                self.assertEqual(snapshots, [r & ((1<<408)-1) for r in oracle.retirements])
                self.assertEqual([self.lib.native_data(i) for i in range(256)], oracle.memory)
                self.assertEqual(d.snapshot(), oracle.final_state)
                self.assertEqual(d.read(STATUS), 6 if oracle.fault else 2)
                self.assertEqual(d.read(TRANSFERS), len(oracle.transfers))
                self.assertEqual(d.read(INSTRUCTIONS), len(oracle.retirements))
                self.assertEqual(d.read(STALLS), stalls)
                self.assertEqual(d.read(CYCLES), oracle.base_cycles+stalls)
                for i, value in enumerate(oracle.memory): d.step(DATA+4*i, expected=value)
        self.replay(d, 'kernels')

    def test_busy_and_reset(self):
        d = Device(self.lib)
        program = model.image([model.word(model.LANE, rd=0), model.word(model.LDI, rd=1, imm=0xabcd),
                               model.word(model.STORE, rd=1, ra=0, imm=128), model.word(model.HLT)])
        d.load(program, [0x4321]*256)
        self.assertEqual(len(d.rows),513, 'waveform START must remain row 514 after ENTRY')
        d.launch()
        d.until(lambda: d.get(PHASE) == MEMORY)
        for address in (PROGRAM, PROGRAM+1020, DATA, DATA+1020):
            d.step(address, hold=True, error=True)
            d.step(address, 1, hold=True, error=True)
        d.step(BASE+COMMAND, START, hold=True, error=True)
        d.step(BASE+ENTRY, 1, hold=True, error=True)
        d.step(BASE+ENTRY, hold=True, expected=0)
        d.step(BASE+STATUS, hold=True, expected=1)
        self.assertEqual(d.read(TRANSFERS), 0)
        # Cancel an unaccepted store, using software reset on an otherwise ready edge.
        d.step(BASE+COMMAND, RESET)
        d.step(DATA+512, expected=0x4321)
        d.launch()
        d.until(lambda: d.get(PHASE) == MEMORY)
        d.step() # accept lane zero only
        self.assertEqual(d.read(TRANSFERS), 1)
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
        d.until(lambda: d.get(PHASE) == MEMORY)
        d.step()
        self.assertEqual(d.get(1), 0x7654)
        d.step(hold=True)
        d.step(BASE+COMMAND, RESET)
        self.assertEqual(d.snapshot(), 0)
        d.launch()
        d.complete()
        self.replay(d, 'reset')

    def test_live_reset_and_partial_fault(self):
        d = Device(self.lib)
        program = model.image([model.word(model.LDI, rd=0, imm=0x7fff),
            model.word(model.MAC, ra=0, rb=0), model.word(model.RDA, rd=1, imm=16),
            model.word(model.LANE, rd=2), model.word(model.STORE, rd=1, ra=2, imm=128),
            model.word(255)])
        d.load(program, [0x7654]*256)
        for abort_pc in (1,2):
            d.launch()
            d.until(lambda: d.get(PHASE) == EXECUTE and d.get(PC) == abort_pc+1)
            if abort_pc == 2:
                self.assertEqual([d.get(16+i) for i in range(4)], [0x3fff0001]*4)
            d.step(BASE+COMMAND,RESET)
            self.assertEqual(d.snapshot(),0)
        d.launch()
        d.complete(seed=17)
        d.step(BASE+STATUS,expected=6)
        for i in range(4): d.step(DATA+512+4*i,expected=0x3fff)
        # Fault doesn't clear the MAC result or roll back preceding stores.
        self.assertEqual([d.get(16+i) for i in range(4)], [0x3fff0001]*4)
        d.step(PROGRAM+20,0)  # replace only the faulting word, relaunch without reset
        d.launch()
        d.complete()
        d.step(BASE+STATUS,expected=2)
        self.replay(d,'live-reset-fault')

    def test_busy_faults_through_cpu(self):
        # A long loop remains busy across trap-handler CPU cycles on both backends.
        words = LI(1,BASE)+LI(2,PROGRAM)+LI(3,DATA)+LI(4,0x07ffffff)+[SW(4,2,0)]
        words += LI(4,0x08000001)+[SW(4,2,4)]+LI(4,RAM+1024)+[CSRRW(0,0x305,4)]
        words += LI(4,1)+[SW(4,1,0)]
        words += [LW(5,2,0),SW(4,2,0),LW(5,3,0),SW(4,3,0),SW(4,1,8),SW(4,1,0)]
        words += LI(4,2)+[SW(4,1,0),LW(5,1,4)]+FINISH()
        words += [0]*((1024//4)-len(words))
        words += [CSRRS(6,0x341,0),ADDI(6,6,4),CSRRW(0,0x341,6),MRET()]
        hexpath,binary = write_image(words,self.prefix,'busy-fault')
        emu = run_emulator('build/rv32/rv32emu',binary,self.prefix/'busy-fault.emu.trace')
        rtl = run_rtl(self.soc,hexpath,self.prefix/'busy-fault.rtl.trace',seed=19)
        check_passed(emu)
        check_passed(rtl)
        self.assertIsNone(compare_backends(rtl,emu,'results'))
        for result in (emu,rtl):
            traps = [line.split()[-2:] for line in result.trace if ' trap ' in line]
            self.assertEqual(traps, [['5',f'{PROGRAM:08x}'],['7',f'{PROGRAM:08x}'],
                ['5',f'{DATA:08x}'],['7',f'{DATA:08x}'],['7',f'{BASE+ENTRY:08x}'],['7',f'{BASE:08x}']])
            self.assertTrue(any('mem[20004004]->00000000/4' in line for line in result.trace))

    def test_trap_advances_device(self):
        # HLT needs FETCH + EXECUTE. The trap must supply FETCH so that the
        # handler's first instruction completes HLT, before its second reads STATUS.
        words = LI(1,BASE)+LI(2,PROGRAM)+[SW(0,2,0)]+LI(3,RAM+0x200)+[CSRRW(0,0x305,3)]
        words += LI(4,START)+[SW(4,1,COMMAND),ECALL()]+FINISH()
        words += [0]*(0x200//4-len(words))
        words += [ADDI(0,0,0),LW(5,1,STATUS)]+FINISH()
        hexpath,binary = write_image(words,self.prefix,'trap-tick')
        emu = run_emulator('build/rv32/rv32emu',binary,self.prefix/'trap-tick.emu.trace')
        rtl = run_rtl(self.soc,hexpath,self.prefix/'trap-tick.rtl.trace')
        for result in (emu,rtl):
            check_passed(result)
            self.assertEqual(sum(' trap ' in line for line in result.trace),1)
            self.assertTrue(any(f'mem[{BASE+STATUS:08x}]->{DONE:08x}/4' in line for line in result.trace))
        self.assertIsNone(compare_backends(rtl,emu,'results',compare_stores=True))

    def test_fixture_guards(self):
        d = Device(self.lib)
        # The guard stays active under python -O; do not rely on bare assert.
        with self.assertRaises(AssertionError): d.step(BASE+STATUS,expected=99)
        with self.assertRaises(AssertionError): d.step(BASE,error=False)
        row = d.rows[0]
        for index,bits in enumerate(FIELD_WIDTHS):
            for invalid in (-1, 1<<bits):
                malformed = row.copy()
                malformed[index] = invalid
                with self.assertRaisesRegex(ValueError,'field width'): fixture_text([malformed])
        with self.assertRaisesRegex(ValueError,'field count'): fixture_text([row+[0]])
        # Replay valid fixtures only; a failed expectation must not append a row.
        self.assertEqual(len(d.rows),1)

    def test_result_store_comparison(self):
        trace = [f'1 80000000 00000000 mem[{BASE+COMMAND:08x}]<-00000001/4',
                 '2 80000004 00000000 mem[80000100]<-00000042/4']
        base = Run(0,'PASS A2\n','','',trace,dict(halt='done',outcome='pass'),[])
        changed = base._replace(trace=[trace[0], trace[1].replace('80000100','80000104')])
        self.assertIn('store mismatch',compare_backends(changed,base,'results',compare_stores=True))
        shifted = base._replace(trace=['9 '+trace[0].split(' ',1)[1], '20 '+trace[1].split(' ',1)[1]])
        self.assertIsNone(compare_backends(shifted,base,'results',compare_stores=True))
        # Some polling code stores a changing iteration count; the opt-in must
        # not change default results-mode semantics for such programs or timers.
        self.assertIsNone(compare_backends(changed,base,'results'))
        with patch('tools.rv32_simd4_kernels.matrix',return_value=[0]*256):
            with self.assertRaisesRegex(ValueError,'four unrolled MAC'): kernels()

    def test_nonempty_fixture_guard(self):
        d = Device(self.lib)
        cmd, fixture = self.replay(d, 'guard')
        fixture.write_text('')
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn('Incomplete fixture', result.stdout+result.stderr)

    def test_simulator_rejects_wide_fixture(self):
        d = Device(self.lib)
        command,fixture = self.replay(d,'wide-guard')
        original = fixture.read_text().split()
        # Check a non-nibble-sized field, a word and the packed snapshot;
        # unknown digits are rejected even by a two-state simulator.
        for index,token in ((0,'2'),(3,'100000000'),(15,'1'+'0'*102),(3,'x')):
            fields = original.copy()
            fields[index] = token
            fixture.write_text(' '.join(fields)+'\n')
            result = subprocess.run(command,capture_output=True,text=True,timeout=20)
            self.assertNotEqual(result.returncode,0)
            self.assertRegex(result.stdout+result.stderr,r'Fixture field width|Invalid fixture hex')

    def test_invalid_stall_options(self):
        for options,message in ((['--simd-stall','-1'],'--simd-stall must be in'),
                (['--simd-seed','2147483648'],'--simd-seed must be in'),
                (['--simd-stall','1','--simd-seed','1'],'not allowed with argument'),
                (['--simd-stall','1','--backend','emulator'],'--simd-stall requires the RTL backend'),
                (['--compare-stores'],'--compare-stores requires --compare results')):
            result = subprocess.run([sys.executable,'tools/rv32_rtl.py',*options],capture_output=True,text=True,timeout=20)
            self.assertEqual(result.returncode,2)
            self.assertIn(message,result.stderr)

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
        for address,op,cause in cases:
            with self.subTest(address=hex(address), cause=cause):
                words = LI(1,address)+[op]+FINISH()
                hexpath, binary = write_image(words, self.prefix, 'fault')
                emu = run_emulator('build/rv32/rv32emu', binary, self.prefix/'fault.emu.trace')
                rtl = run_rtl(self.soc, hexpath, self.prefix/'fault.rtl.trace', seed=7)
                self.assertIsNone(diff_traces(rtl.trace, emu.trace))
                self.assertEqual(rtl.noise, '')
                self.assertEqual(rtl.status, 0, rtl.stderr)
                self.assertIsNotNone(rtl.halt, rtl.stderr)
                self.assertEqual((rtl.halt['halt'], rtl.halt['cause'], rtl.halt['tval']),
                                 ('double-fault',1,0), rtl.stderr)
                self.assertEqual(rtl.halt['steps'],len(rtl.trace))
                self.assertEqual(emu.halt['halt'], 'double-fault')
                self.assertIn(f'trap {cause} {address:08x}', emu.trace[3 if cause == 1 else 2])

    def test_firmware(self):
        binary = Path('build/rv32/simdcheck.bin')
        emu = run_emulator('build/rv32/rv32emu', binary, self.prefix/'firmware.emu.trace')
        check_passed(emu)
        self.assertEqual(emu.console, 'vector OK\nmatrix signed/unsigned OK\nrecovery OK\nPASS A2\n')
        start = next(i for i,line in enumerate(emu.trace) if f'mem[{BASE+COMMAND:08x}]<-{START:08x}/4' in line)
        done = next(i for i,line in enumerate(emu.trace[start+1:],start+1)
                    if f'mem[{BASE+STATUS:08x}]->{DONE:08x}/4' in line)
        self.assertEqual(done-start,201, '198 device ticks plus observation and poll-loop alignment')
        reports = []
        for stall, seed, simd_stall, simd_seed in ((0,None,0,None),(3,None,2,None),(None,7,None,19)):
            rtl = run_rtl(self.soc, 'build/rv32/simdcheck.hex', self.prefix/'firmware.rtl.trace', stall=stall, seed=seed,
                          simd_stall=simd_stall, simd_seed=simd_seed)
            check_passed(rtl)
            self.assertIsNone(compare_backends(rtl, emu, 'results', compare_stores=True))
            def counters(trace):
                reads = {offset: [int(m.group(1),16) for line in trace
                         if (m := re.search(rf'mem\[{BASE+offset:08x}\]->([0-9a-f]{{8}})',line))]
                         for offset in (CYCLES,STALLS,TRANSFERS,INSTRUCTIONS)}
                return [dict(zip(('cycles','stalls','transfers','instructions'),row))
                        for row in zip(*(reads[offset] for offset in (CYCLES,STALLS,TRANSFERS,INSTRUCTIONS)), strict=True)]
            device_counts = counters(rtl.trace)
            self.assertEqual(len(device_counts),3)
            if simd_stall is not None:
                self.assertEqual([c['stalls'] for c in device_counts], [simd_stall*c['transfers'] for c in device_counts])
            else:
                for counts in device_counts:
                    self.assertGreater(counts['stalls'],0)
                    self.assertLessEqual(counts['stalls'],3*counts['transfers'])
            reports.append(dict(cpu_stall=stall,cpu_seed=seed,device_stall=simd_stall,device_seed=simd_seed,
                rtl_cpu_instructions=len(rtl.trace),rtl_cpu_cycles=rtl.halt['cycles'],
                emulator_cpu_instructions=len(emu.trace),rtl_device_jobs=device_counts,emulator_device_jobs=counters(emu.trace)))
        (self.prefix/'measurements.json').write_text(json.dumps(reports,indent=2)+'\n')


if __name__ == '__main__':
    unittest.main()
