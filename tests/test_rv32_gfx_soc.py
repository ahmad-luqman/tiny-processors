"""CPU-visible G1 ownership and fault contracts, on both backends."""
import os
from pathlib import Path
import unittest
from tools.rv32_asm import LI,LW,SW,SB,SH,ADDI,CSRRS,CSRRW,MRET,FINISH,ECALL,RAM,JALR
from tools.rv32_rtl import write_image,run_rtl,run_emulator,check_passed,compare_backends,uses_accelerator,simulator_command
BASE=0x20007000

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
        for i,v in enumerate(p):words+=LI(4,v&0xffffffff)+[SW(4,1,64+4*i)]
        return words
    def test_busy_ownership(self):
        source=RAM+0x10003
        p=[2,0,0,0,0,0,0,0,320,240,source,320,320,240,0,0]
        words=self.parameters(p)+LI(2,0x30000000)+LI(3,source)+LI(7,0x20002000)
        words+=LI(4,RAM+1024)+[CSRRW(0,0x305,4)]+LI(4,1)+[SW(4,1,0)]
        words += [LW(5,2,0),SW(4,2,0),SW(4,7,0),SW(4,1,64),SW(4,1,0),SB(4,3,0),SB(4,3,767)]
        # Writes before the extent and after it remain legal. Source reads remain legal.
        words += [SB(4,3,-1),LW(5,3,1)]+LI(6,source+76800)+[SB(4,6,0)]
        words += LI(4,2)+[SW(4,1,0),LW(5,1,4),SW(4,2,0),SB(4,3,0)]+FINISH()
        words += [0]*(256-len(words))+[CSRRS(6,0x341,0),ADDI(6,6,4),CSRRW(0,0x341,6),MRET()]
        for run in self.run_pair(words,'ownership',seed=19,gpu_seed=31):
            traps=[l.split()[-2:] for l in run.trace if ' trap ' in l]
            self.assertEqual(traps,[[str(c),f'{a:08x}'] for c,a in [(5,0x30000000),(7,0x30000000),(7,0x20002000),(7,BASE+64),(7,BASE),(7,source),(7,source+767)]])
            self.assertTrue(any('mem[20007004]->00000000/4' in l for l in run.trace))
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
        args=simulator_command(self.sim,'a',gpu_stall=2,gpu_seed=None)
        self.assertIn('+gpu-stall=2',args)

if __name__=='__main__':unittest.main()
