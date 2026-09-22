"""CPU-visible G1 ownership and fault contracts, on both backends."""
import os
from pathlib import Path
import unittest
from tools.rv32_asm import LI,LW,SW,SB,SH,ADDI,CSRRS,CSRRW,MRET,FINISH,ECALL,RAM,JALR,LBU,BNE,GPU_BASE,GPU_STATUS,GPU_PARAMS,GPU_ERROR,GPU_STALLS,GPU_COMMAND,DISPLAY,FB
from tools.rv32_rtl import write_image,run_rtl,run_emulator,check_passed,compare_backends,uses_accelerator,simulator_command
BASE=GPU_BASE

class GraphicsSoC(unittest.TestCase):
    def setUp(self):
        self.out=Path('build/gfx/soc-'+os.environ.get('G1_SIM','icarus'));self.out.mkdir(parents=True,exist_ok=True)
        self.sim='build/verilator-rv32/rv32_sim' if os.environ.get('G1_SIM')=='verilator' else 'build/rv32/rv32_tb.vvp'
    def run_pair(self,words,name,**kw):
        hp,bp=write_image(words,self.out,name)
        emu=run_emulator('build/rv32/rv32emu',bp,self.out/(name+'.emu.trace'))
        rtl=run_rtl(self.sim,hp,self.out/(name+'.rtl.trace'),**kw)
        check_passed(emu);check_passed(rtl)
        self.assertIsNone(compare_backends(rtl,emu,'results'))
        return emu,rtl
    def parameters(self,p):
        words=LI(1,BASE)
        for i,v in enumerate(p):words+=LI(4,v&0xffffffff)+[SW(4,1,GPU_PARAMS+4*i)]
        return words
    def test_busy_ownership(self):
        source=RAM+0x10003
        p=[2,0,0,0,0,0,0,0,320,240,source,320,320,240,0,0]
        words=self.parameters(p)+LI(2,0x30000000)+LI(3,source)+LI(7,0x20002000)
        words+=LI(4,0x11223344)+[SW(4,3,-3)]+LI(4,0x55667788)+[SW(4,3,765)]
        words+=LI(8,FB+76800-4)+LI(4,0xaabbccdd)+[SW(4,8,0)]
        words+=LI(4,RAM+1024)+[CSRRW(0,0x305,4)]+LI(4,1)+[SW(4,1,0)]
        words += [LW(5,2,0),SW(4,2,0),SW(4,7,0),SW(4,1,64),SW(4,1,0),SB(4,3,0),SB(4,3,767),SW(4,8,0),LW(5,7,4),LW(5,7,8),LW(5,7,12)]
        # Writes before the extent and after it remain legal. Source reads remain legal.
        words += [SB(4,3,-1),LW(5,3,1)]+LI(6,source+76800)+[SB(4,6,0)]
        words += [LW(5,1,GPU_STALLS)]+LI(4,2)+[SW(4,1,GPU_COMMAND),LW(5,1,GPU_STATUS)]
        words += [LBU(5,3,0),LBU(5,3,767),LW(5,8,0)]
        words += [LW(5,1,GPU_PARAMS+4*i) for i in range(16)]
        words += LI(4,1)+[SW(4,1,GPU_COMMAND)]
        words += [ADDI(0,0,0),LW(5,1,GPU_STATUS),LW(5,1,GPU_ERROR)]
        words += [SW(4,2,0),SB(4,3,0)]+FINISH()
        words += [0]*(256-len(words))+[CSRRS(6,0x341,0),ADDI(6,6,4),CSRRW(0,0x341,6),MRET()]
        for suffix,timing in [('random',dict(seed=19,gpu_seed=31)),('fixed',dict(stall=1,gpu_stall=2))]:
            for backend,run in zip(('emulator','rtl'),self.run_pair(words,'ownership-'+suffix,**timing)):
                traps=[l.split()[-2:] for l in run.trace if ' trap ' in l]
                self.assertEqual(traps,[[str(c),f'{a:08x}'] for c,a in [(5,0x30000000),(7,0x30000000),(7,0x20002000),(7,BASE+64),(7,BASE),(7,source),(7,source+767),(7,FB+76800-4)]])
                for address,value,width in [(BASE+GPU_STATUS,0,4),(source,0x11,1),(source+767,0x66,1),
                                            (FB+76800-4,0xaabbccdd,4),(BASE+GPU_STATUS,4,4),(BASE+GPU_ERROR,1,4),
                                            (DISPLAY+4,0,4),(DISPLAY+8,320,4),(DISPLAY+12,240,4)]+[(BASE+GPU_PARAMS+4*i,0,4) for i in range(16)]:
                    self.assertTrue(any(f'mem[{address:08x}]->{value:08x}/{width}' in l for l in run.trace),(address,value))
                stalls=[int(l.split('->')[1].split('/')[0],16) for l in run.trace if f'mem[{BASE+GPU_STALLS:08x}]->' in l]
                self.assertEqual(len(stalls),1)
                if backend=='rtl':self.assertGreater(stalls[0],0)
    def test_source_lock_lanes(self):
        source=RAM+0x10003
        end=source+239*320+319
        p=[2,0,0,0,0,0,0,0,320,240,source,320,319,240,0,0]
        words=self.parameters(p)+LI(3,source)+LI(7,end)+LI(4,0x12345678)
        words += [SW(4,3,-3),SW(4,3,317),SW(4,7,-2)]+LI(4,RAM+4096)+[CSRRW(0,0x305,4)]
        words += LI(4,1)+[SW(4,1,GPU_COMMAND),SW(4,3,-3),SH(4,3,-1),SB(4,3,319),SW(4,7,-2)]
        # Adjacent unprotected bytes and unrelated stores remain live during the blit.
        words += [SH(4,7,0),SB(4,3,-1)]+LI(8,RAM+0x30000)+[SW(4,8,0),LW(5,8,0)]
        words += LI(4,2)+[SW(4,1,GPU_COMMAND),LBU(5,3,0),LBU(5,3,319),LBU(5,7,-1)]+FINISH()
        words += [0]*(1024-len(words))+[CSRRS(6,0x341,0),ADDI(6,6,4),CSRRW(0,0x341,6),MRET()]
        for run in self.run_pair(words,'source-lanes',stall=1,gpu_stall=2):
            self.assertEqual([l.split()[-2:] for l in run.trace if ' trap ' in l],
                [['7',f'{a:08x}'] for a in (source-3,source-1,source+319,end-2)])
            for address,value,width in [(source,0x12,1),(source+319,0x34,1),(end-1,0x56,1),(RAM+0x30000,1,4)]:
                self.assertTrue(any(f'mem[{address:08x}]->{value:08x}/{width}' in l for l in run.trace),(address,value))

    def test_trap_tick_and_empty_done(self):
        words=self.parameters([1]+[0]*15)+LI(4,RAM+1024)+[CSRRW(0,0x305,4)]
        words+=LI(4,1)+[SW(4,1,0),ECALL()]+FINISH()
        words += [0]*(256-len(words))+[LW(5,1,4)]+FINISH()
        for run in self.run_pair(words,'trap-tick'):
            self.assertTrue(any('mem[20007004]->00000002/4' in l for l in run.trace))
            self.assertEqual(sum(' trap ' in l for l in run.trace),1)
    def test_register_faults(self):
        for n,(off,access) in enumerate([(0,LW),(28,LW),(32,LW),(60,LW),(4,SW),(4,SB),(64,SH)]):
            words=LI(1,BASE)+LI(4,RAM+1024)+[CSRRW(0,0x305,4)]+[access(5,1,off)]+FINISH()
            words += [0]*(256-len(words))+[CSRRS(6,0x341,0),ADDI(6,6,4),CSRRW(0,0x341,6),MRET()]
            for run in self.run_pair(words,f'fault{n}'):
                self.assertEqual([l.split()[-2:] for l in run.trace if ' trap ' in l],[[str(5 if access==LW else 7),f'{BASE+off:08x}']])
    def test_runner_contract(self):
        self.assertTrue(uses_accelerator(['1 80000000 00000000 mem[20007004]->00000001/4']))
        self.assertFalse(uses_accelerator(['1 80000000 00000000 trap 5 20007004']))
        self.assertFalse(uses_accelerator(['1 80000000 00000000 mem[20007040]<-00000001/4']))
        args=simulator_command(self.sim,'a',gpu_stall=2,gpu_seed=None)
        self.assertIn('+gpu-stall=2',args)

if __name__=='__main__':unittest.main()
