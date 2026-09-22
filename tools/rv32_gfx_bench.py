#!/usr/bin/env python3
"""Measure compiled CPU/device jobs; require literal expected frame checkpoints."""
import argparse
import json
from pathlib import Path
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.rv32_rtl import run_rtl,run_emulator,check_passed,write_image

def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--emulator',default='build/rv32/rv32emu')
    parser.add_argument('--simulator',default='build/verilator-rv32/rv32_sim')
    args=parser.parse_args()
    out=Path('build/gfx/bench');out.mkdir(parents=True,exist_ok=True)
    results={};reference=None
    for name in ('gfxbench_cpu','gfxbench'):
        image=Path(f'build/rv32/{name}.bin');data=image.read_bytes()
        words=[int.from_bytes(data[i:i+4],'little') for i in range(0,len(data),4)]
        hp,bp=write_image(words,out,name)
        for backend,stall in [('emulator',0),('rtl',0),('rtl',2)]:
            tag=f'{name}-{backend}-{stall}'
            if backend=='emulator':run=run_emulator(args.emulator,bp,out/(tag+'.trace'),checkpoints=out/(tag+'.checkpoints'))
            else:run=run_rtl(args.simulator,hp,out/(tag+'.trace'),stall=stall,gpu_stall=stall,max_cycles=30000000,checkpoints=out/(tag+'.checkpoints'))
            check_passed(run)
            # Four measured jobs always produce the same four images on all backends.
            checks=(out/(tag+'.checkpoints')).read_text().splitlines()
            if len(checks)!=4:raise RuntimeError('missing benchmark checkpoints')
            if reference is None:reference=checks
            if checks!=reference:raise RuntimeError(f'{tag}: framebuffer mismatch')
            jobs=[list(map(int,line.split()[1:])) for line in run.console.splitlines() if line.startswith('BENCH ')]
            if [j[0] for j in jobs]!=[1,2,3,4] or not run.console.endswith('PASS BENCH\n'):raise RuntimeError('invalid benchmark output')
            results[tag]={'jobs':jobs,'halt':run.halt,'checkpoints':checks}
            print(tag,jobs,flush=True)
    (out/'measurements.json').write_text(json.dumps(results,indent=2)+'\n')

if __name__=='__main__':main()
