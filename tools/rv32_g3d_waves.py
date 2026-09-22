#!/usr/bin/env python3
"""Dump one small G2 job to a VCD and report how long each phase took.

The job is a single triangle (424 pixels) shaded by the divergent-loop
program over a partial batch of three vertices, so the dump shows the mask
stack, the divider, setup and the per-pixel transfers without the megabytes a
whole frame would produce. The phase totals must equal the oracle's cycle count.
"""
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_g3d_corpus import job_line  # noqa: E402
from tools.rv32_g3d_header import ZBASE, expected, scenes  # noqa: E402
from tools.rv32_g3d_scene import vertex  # noqa: E402
from tools.rv32_rtl import has_value_changes  # noqa: E402

PHASES = ('IDLE VALIDATE INDEX BATCH EXEC VCHECK DIVIDE FETCH AREA TEST ZREAD ZWRITE PWRITE FINISH CLEAR').split()


def job():
    loops = next(s for s in scenes() if s[0] == 'loops')
    verts = [vertex(-.10, -.10, z=.3, r=250, g=20, b=20), vertex(.02, -.10, z=.4, r=20, g=250, b=20),
             vertex(-.05, .02, z=.5, r=20, g=20, b=250)]
    return ('waves', loops[1], [0] * 32, verts, [(0, 1, 2)], 3, 4096, ZBASE)


def phase_ticks(vcd):
    """Clock periods spent in each dut.state value, from the VCD's value changes."""
    lines = vcd.splitlines()
    scope, code = [], None
    for line in lines:
        parts = line.split()
        if parts[:1] == ['$scope']:
            scope.append(parts[2])
        elif parts[:1] == ['$upscope']:
            scope.pop()
        elif parts[:1] == ['$var'] and parts[4] == 'state' and scope[-1] == 'dut':
            code = parts[3]
    time, changes = 0, []
    for line in lines:
        if line.startswith('#'):
            time = int(line[1:])
        elif code and line.startswith('b') and line.split()[1] == code:
            bits = line.split()[0][1:]
            if set(bits) <= {'0', '1'}:   # before reset the state is unknown
                changes.append((time, int(bits, 2)))
    ticks = {}
    for (start, state), (end, _) in zip(changes, changes[1:]):
        if state:
            ticks[PHASES[state]] = ticks.get(PHASES[state], 0) + (end - start) // 10000
    return ticks


def main():
    out = ROOT / 'build/g3d/waves'
    out.mkdir(parents=True, exist_ok=True)
    scene = job()
    e = expected(scene)
    (out / 'job.txt').write_text(job_line(scene, 0) + '\n')
    subprocess.run(['iverilog', '-g2012', '-s', 'rv32_g3d_tb', '-o', str(out / 'g3d.vvp'), 'tests/rv32_g3d_tb.sv',
                    'rtl/rv32/rv32_g3d.v', 'rtl/rv32/rv32_g3d_core.v'], cwd=ROOT, check=True)
    result = subprocess.run(['vvp', str(out / 'g3d.vvp'), f'+input={out / "job.txt"}', f'+wave={out / "g3d.vcd"}', '+waves-only'],
                            cwd=ROOT, capture_output=True, text=True)
    if 'PASS 1' not in result.stdout:
        sys.exit(result.stdout + result.stderr)
    vcd = (out / 'g3d.vcd').read_text()
    if not has_value_changes(vcd):
        sys.exit('empty VCD')
    ticks = phase_ticks(vcd)
    busy = sum(ticks.values())
    print(f'wrote {out / "g3d.vcd"} ({len(vcd) // 1024} KiB)')
    print(f"oracle: {e['instructions']} instructions, {e['divides']} divides, {e['pixels']} pixels, "
          f"{e['transfers']} transfers, {e['cycles']} cycles")
    print('ticks per phase: ' + ', '.join(f'{k} {v}' for k, v in ticks.items()))
    if busy != e['cycles']:
        sys.exit(f'phases add to {busy} busy ticks, oracle says {e["cycles"]}')
    print(f'START to DONE: {busy} ticks, equal to the oracle')


if __name__ == '__main__':
    main()
