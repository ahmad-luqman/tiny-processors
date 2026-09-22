#!/usr/bin/env python3
"""Measure compiled CPU/device jobs; require literal expected frame checkpoints."""
import argparse
import json
import re
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
    results={}
    reference=['frame 1 be955505', 'frame 2 ec454505', 'frame 3 4d8285eb', 'frame 4 bb38af12']
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
            if checks!=reference:raise RuntimeError(f'{tag}: framebuffer mismatch')
            jobs=[list(map(int,line.split()[1:])) for line in run.console.splitlines() if line.startswith('BENCH ')]
            if [j[0] for j in jobs]!=[1,2,3,4] or not run.console.endswith('PASS BENCH\n'):raise RuntimeError('invalid benchmark output')
            intervals=[];active=None
            for line in run.trace:
                if 'mem[20000000]->' in line:
                    if active is None:active={'cpu_instructions':0,'command_stores':0,'status_reads':0,'framebuffer_reads':0,'framebuffer_writes':0}
                    else:intervals.append(active);active=None
                elif active is not None:
                    active['cpu_instructions']+=1
                    match=re.search(r'mem\[([0-9a-f]{8})\](->|<-)',line)
                    if match:
                        addr=int(match[1],16);write=match[2]=='<-'
                        if 0x20007000<=addr<0x20007080 and write:active['command_stores']+=1
                        if addr==0x20007004 and not write:active['status_reads']+=1
                        if 0x30000000<=addr<0x30012c00:active['framebuffer_writes' if write else 'framebuffer_reads']+=1
            if len(intervals)!=4 or active is not None:raise RuntimeError('missing timing intervals')
            results[tag]={'jobs':jobs,'traffic':intervals,'halt':run.halt,'checkpoints':checks}
            print(tag,jobs,flush=True)
    (out/'measurements.json').write_text(json.dumps(results,indent=2)+'\n')

if __name__=='__main__':main()
