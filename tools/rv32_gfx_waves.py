#!/usr/bin/env python3
"""Focused fill and reset-over-held-write waveforms from the byte-port harness."""
from pathlib import Path
import subprocess
out=Path('build/gfx');out.mkdir(parents=True,exist_ok=True)
# Small fill: first request, accepted pixels, and completion. Reset fixture
# reaches a held transfer and then relaunches with a distinct color.
a=[1,0xa6,1,1,0,0,0,0,3,2,0,0,0,0,0,0]
b=a.copy();b[1]=0x1f
for name,rows in [('fill',[(0,-1,a)]),('reset',[(1,5,a),(0,-1,b)])]:
    fixture=out/(name+'.txt');fixture.write_text(''.join(f'{m} {abort} '+' '.join(f'{v:08x}' for v in p)+'\n' for m,abort,p in rows))
    run=subprocess.run(['vvp',str(out/'gpu.vvp'),f'+input={fixture}',f'+wave={out/(name+".vcd")}'],capture_output=True,text=True,check=True)
    if f'PASS {len(rows)}' not in run.stdout:raise RuntimeError(run.stdout)
    (out/(name+'.wave.log')).write_text(run.stdout)
    print(run.stdout,end='')
