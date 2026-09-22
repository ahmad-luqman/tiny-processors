#!/usr/bin/env python3
"""Write G2 corpus jobs with the oracle's expected results.

The native sanitizer harness (tests/rv32_g3d_native.c) runs every job through
the C reference and the emulator device; the RTL testbench (tests/rv32_g3d_tb.sv)
runs them through the device RTL. Each job is one line of hex words:

  vcount tcount limit zbase flags | 128 program | 32 consts |
  min(vcount,32)*8 inputs | min(tcount,64) triangles |
  status error fault_pc instructions transfers divides pixels zfail culled cycles fb_hash z_hash

flags bit 0 withholds memory acceptance on a pattern; bit 1 marks a duplicate
job the RTL testbench resets mid-transfer (it is checked for a cleared, idle
device, and its original is compared in full).

Order matters: `make test-rv32-3d` runs the first ICARUS_JOBS jobs on Icarus,
so the scenes and every fault class come first.
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import rv32_g3d_model as M  # noqa: E402
from tools.rv32_g3d_header import ZBASE, expected, scenes  # noqa: E402
from tools.rv32_g3d_model import assemble, encode, pack_triangle  # noqa: E402
from tools.rv32_g3d_scene import SHADERS, constants, cube, passthrough, random_program, vertex  # noqa: E402

KEYS = ('status', 'error', 'fault_pc', 'instructions', 'transfers', 'divides', 'pixels', 'zfail', 'culled',
        'cycles', 'fb_hash', 'z_hash')
HOLD, CANCEL = 1, 2
ICARUS_JOBS = 32


def job(name, program, triangles=(), inputs=None, vcount=None, limit=4096, zbase=ZBASE, consts=None):
    inputs = inputs if inputs is not None else [[0] * 8 for _ in range(3)]
    vcount = len(inputs) if vcount is None else vcount
    return (name, list(program), consts or [0] * 32, inputs, list(triangles), vcount, limit, zbase)


def directed_faults():
    """Every fault class, including the three only reachable from raw words."""
    true = assemble('LDI r0, 1\nSEQ r0, r0\nEND')[:2]
    false = assemble('LDI r0, 1\nSLT r0, r0\nEND')[:2]
    six = [vertex(-.8, -.6), vertex(.2, -.6), vertex(-.3, .5)] * 2
    return [
        job('illegal-28', [28 << 26]),
        job('illegal-63', true + [63 << 26]),
        job('else-without-if', [encode('ELSE')]),
        job('endif-in-loop', [encode('LOOP'), encode('ENDIF')]),
        job('break-outside-loop', true + [encode('IF') | 3, encode('BREAK')]),
        job('end-in-block', [encode('LOOP'), encode('END')]),
        job('pc-128', [encode('LDI')] * 128),
        job('pc-128-by-jump', false + [encode('IF') | 127] + [encode('LDI')] * 125),
        job('loop-overflow', true + [encode('LOOP')] * 9),
        # The second batch runs away: FAULT_PC carries batch 1, after batch 0 was projected.
        job('limit-batch-1', assemble('SPC r0, vid\nLDI r1, 4\nSLT r0, r1\nIF\nELSE\nLOOP\nENDLOOP\nENDIF\n'
                                      + '\n'.join(f'IN r{i}, {i}\nOUT {i}, r{i}' for i in range(7)) + '\nEND'),
            [(0, 1, 2)], six + [vertex(0, 0)] * 2, limit=80),
        job('tcount-65', passthrough(), [(0, 1, 2)] * 65),
        job('vcount-0', passthrough(), vcount=0),
        job('vcount-33', passthrough(), inputs=[vertex(0, 0)] * 33),
        job('limit-0', passthrough(), limit=0),
        job('limit-65536', passthrough(), limit=0x10000),
        job('zbase-below-ram', passthrough(), zbase=0x7ffffffc),
        job('zbase-past-ram', passthrough(), zbase=0x80400000 - M.Z_BYTES + 4),
        job('index-top-byte', passthrough(), [0xff020100, 0x00030100]),
    ]


def raster_cases():
    tri = [vertex(-.8, -.6, z=.3, r=250, g=10, b=0), vertex(.8, -.5, z=.6, r=0, g=0, b=200),
           vertex(.1, .9, z=.9, r=0, g=90, b=0)]
    quad = [vertex(-.7, -.6, r=200), vertex(.8, -.5, g=200), vertex(.6, .7, b=200), vertex(-.5, .8, r=90, g=90)]
    fan = [vertex(0.03, -0.01, z=.4)] + [vertex(0.9 * c, 0.9 * s, z=.4, r=40 * i, g=255 - 30 * i) for i, (c, s) in
                                          enumerate(((1, 0), (.6, .8), (-.3, .95), (-1, .1), (-.4, -.9), (.7, -.7)))]
    near = [vertex(-.8, -.8, z=.2, r=255, g=0, b=0), vertex(.8, -.8, z=.2, r=255, g=0, b=0), vertex(0, .8, z=.2, r=255, g=0, b=0)]
    far = [vertex(-.6, -.9, z=.8, r=0, g=255, b=0), vertex(.9, -.2, z=.8, r=0, g=255, b=0), vertex(-.2, .9, z=.8, r=0, g=255, b=0)]
    ramp = [vertex(-1, -1, z=0), vertex(1, -1, z=1), vertex(-1, 1, z=0), vertex(1, 1, z=1)]
    # A sliver: huge gradients take the divider's saturation path, and it still draws a few pixels.
    sliver = [vertex(-1, -.02, z=0, r=0), vertex(1, 0.001, z=1, r=255), vertex(1, -0.0005, z=.02, r=0, b=255)]
    # Mirrored in x and with the colour ramp reversed, so the gradients saturate the other way.
    mirrored = [vertex(1, -.02, z=1, r=255), vertex(-1, 0.001, z=0, r=0), vertex(-1, -0.0005, z=.98, r=255, b=0)]
    # Colours outside 0..255 exercise the vertex clamp; a z of exactly w and 0 the depth range edges.
    clamp = [vertex(-.9, -.9, z=0, r=300, g=-20, b=1000), vertex(.9, -.9, z=1, r=-5, g=400, b=128),
             vertex(0, .9, z=.5, r=128, g=128, b=-300)]
    # 32 vertices and 64 triangles, the window limits; every one drawn (a 4x8 grid of small triangles).
    grid = [vertex(-.9 + .25 * (i % 8), -.9 + .5 * (i // 8), z=.1 + .02 * i, r=8 * i, g=255 - 8 * i, b=4 * i)
            for i in range(32)]
    cells = [(r * 8 + c, r * 8 + c + 1, (r + 1) * 8 + c + 1) for r in range(3) for c in range(7)]
    cells += [(r * 8 + c, (r + 1) * 8 + c + 1, (r + 1) * 8 + c) for r in range(3) for c in range(7)]
    cells += [(i, (i + 1) % 32, (i + 9) % 32) for i in range(64 - len(cells))]
    return [
        job('shared-quad', passthrough(), [(0, 1, 2), (0, 2, 3)], quad),
        job('fan', passthrough(), [(0, i, i % 6 + 1) for i in range(1, 7)], fan),
        job('near-then-far', passthrough(), [(0, 1, 2), (3, 4, 5)], near + far),
        job('far-then-near', passthrough(), [(3, 4, 5), (0, 1, 2)], near + far),
        job('equal-depth', passthrough(), [(0, 1, 2), (0, 1, 2)], tri),
        job('depth-ramp', passthrough(), [(0, 1, 3), (0, 3, 2)], ramp),
        job('sliver', passthrough(), [(0, 1, 2), (0, 2, 1)], sliver),
        job('sliver-mirrored', passthrough(), [(0, 1, 2), (0, 2, 1)], mirrored),
        job('clamps', passthrough(), [(0, 1, 2)], clamp),
        job('limits-32-64', passthrough(), cells, grid, limit=0xffff),
        job('zbase-top-of-ram', passthrough(), [(0, 1, 2)], tri, zbase=0x80400000 - M.Z_BYTES),
        job('limit-exact', assemble('LDI r0, 0\nLDI r0, 0\nEND'), inputs=[[0] * 8] * 8, limit=3),
        job('tcount-0', passthrough(), (), tri),
    ]


def random_drawing(rng):
    """A random structured prefix, then outputs from valid clip-space inputs, so random
    shader control flow is followed by triangles that actually draw."""
    while True:
        try:
            body = random_program(rng)
            program = assemble('\n'.join(body + [f'IN r{i}, {i}\nOUT {i}, r{i}' for i in range(7)] + ['END']))
            break
        except ValueError:
            continue
    vcount = rng.randrange(3, 33)
    inputs = []
    for _ in range(vcount):
        w = rng.uniform(.3, 2.5)
        inputs.append(vertex(rng.uniform(-1.3, 1.3) * w, rng.uniform(-1.3, 1.3) * w, rng.uniform(0, 1) * w, w,
                             rng.randrange(-40, 300), rng.randrange(-40, 300), rng.randrange(-40, 300)))
    tris = [tuple(rng.randrange(vcount) for _ in range(3)) for _ in range(rng.randrange(1, 20))]
    return job('random-draw', program, tris, inputs, limit=4096,
               consts=[rng.getrandbits(32) for _ in range(32)])


def random_raw(rng):
    """Unstructured words: every run-time fault class, from arbitrary positions and batches."""
    control = [encode(n) for n in ('IF', 'ELSE', 'ENDIF', 'LOOP', 'ENDLOOP', 'BREAK', 'END')]
    words = [rng.choice(control) | rng.randrange(128) if rng.random() < 0.3 else
             rng.getrandbits(32) if rng.random() < 0.1 else
             encode(rng.choice(M.OPS[1:22]), *(rng.randrange(16) for _ in range(4)), imm=rng.getrandbits(14))
             for _ in range(rng.randrange(1, 40))]
    vcount = rng.randrange(1, 12)
    return job('random-raw', words, (), [[rng.getrandbits(32) for _ in range(8)] for _ in range(vcount)],
               limit=300, consts=[rng.getrandbits(32) for _ in range(32)])


def jobs():
    """(job, flags) pairs in corpus order."""
    out = [(j, i % 2 * HOLD) for i, j in enumerate(scenes())]
    # One reset-mid-transfer duplicate inside the Icarus prefix: the divergent-loop scene.
    out.append((next(j for j, _ in out if j[0] == 'loops'), HOLD | CANCEL))
    out += [(j, HOLD if i % 3 == 0 else 0) for i, j in enumerate(directed_faults())]
    out += [(j, i % 2 * HOLD) for i, j in enumerate(raster_cases())]
    inputs, triangles = cube()
    for i, (name, text) in enumerate(SHADERS.items()):
        for angle in (0.2, 2.9, 5.1):
            out.append((job(name, assemble(text), triangles, inputs, consts=constants(angle, frame=int(angle * 50))),
                        HOLD if angle > 1 else 0))
    rng = random.Random(4242)
    for i in range(30):
        out.append((random_drawing(rng), i % 2 * HOLD))
    for i in range(40):
        out.append((random_raw(rng), 0))
    for i in range(20):
        while True:
            try:
                words = assemble('\n'.join(random_program(rng) + ['END']))
                break
            except ValueError:
                continue
        vcount = rng.randrange(1, 33)
        out.append((job('random-shader', words, (), [[rng.getrandbits(32) for _ in range(8)] for _ in range(vcount)],
                        limit=rng.choice([60, 400, 4096]), consts=[rng.getrandbits(32) for _ in range(32)]), 0))
    # Duplicates that the RTL testbench resets mid-transfer: a scene with divergent loops and two frames.
    by_name = {j[0]: j for j, _ in out}
    for name in ('diffuse', 'limits-32-64'):
        out.append((by_name[name], HOLD | CANCEL))
    return out


def job_line(scene, flags):
    name, program, consts, inputs, triangles, vcount, limit, zbase = scene
    e = expected(scene)
    rows = inputs[:min(vcount, M.VMAX)]
    words = [vcount, len(triangles), limit, zbase, flags]
    words += list(program) + [0] * (M.PROGRAM_WORDS - len(program)) + list(consts)
    words += [w for row in rows for w in row] + [t if isinstance(t, int) else pack_triangle(t) for t in triangles[:M.TMAX]]
    words += [e[k] for k in KEYS]
    return ' '.join(f'{w & 0xffffffff:x}' for w in words)


def main():
    for scene, flags in jobs():
        print(job_line(scene, flags))


if __name__ == '__main__':
    main()
