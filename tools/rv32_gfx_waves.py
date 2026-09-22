#!/usr/bin/env python3
"""Focused fill and reset-over-held-write waveforms from the byte-port harness."""
from pathlib import Path
import subprocess
import sys
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from tools.rv32_gfx_fixture import fixture_text
out=Path('build/gfx');out.mkdir(parents=True,exist_ok=True)
# Small fill: first request, accepted pixels, and completion. Reset fixture
# reaches a held transfer and then relaunches with a distinct color.
a=[1,0xa6,1,1,0,0,0,0,3,2,0,0,0,0,0,0]
b=a.copy();b[1]=0x1f
for name,rows in [('fill',[(0,-1,a)]),('reset',[(1,10,a),(0,-1,b)])]:
    fixture=out/(name+'.txt');fixture.write_text(fixture_text(rows))
    run=subprocess.run(['vvp',str(out/'gpu.vvp'),f'+input={fixture}',f'+wave={out/(name+".vcd")}'],capture_output=True,text=True,check=True)
    if f'PASS {len(rows)}' not in run.stdout:raise RuntimeError(run.stdout)
    results=[[int(v,16) for v in line.split()[2:]] for line in run.stdout.splitlines() if line.startswith('RESULT ')]
    expected=[(2,6)] if name=='fill' else [(0,0),(2,6)]
    if [(r[1],r[6]) for r in results]!=expected:raise RuntimeError(f'{name}: incorrect completion/write counters: {results}')
    (out/(name+'.wave.log')).write_text(run.stdout)
    print(run.stdout,end='')
