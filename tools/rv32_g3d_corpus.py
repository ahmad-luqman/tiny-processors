#!/usr/bin/env python3
"""Write G2 corpus jobs with the oracle's expected results for the native sanitizer run.

Each job is one line of hex words:
  vcount tcount limit zbase hold_seed | 128 program | 32 consts | vcount*8 inputs | tcount triangles |
  status error fault_pc instructions transfers divides pixels zfail culled cycles fb_hash z_hash
"""
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_g3d_header import ZBASE, expected, scenes  # noqa: E402
from tools.rv32_g3d_model import assemble  # noqa: E402
from tools.rv32_g3d_scene import SHADERS, constants, cube  # noqa: E402
from tests.test_rv32_3d import random_program  # noqa: E402

KEYS = ('status', 'error', 'fault_pc', 'instructions', 'transfers', 'divides', 'pixels', 'zfail', 'culled',
        'cycles', 'fb_hash', 'z_hash')


def jobs():
    out = list(scenes())
    inputs, triangles = cube()
    for name, text in SHADERS.items():
        for angle in (0.2, 2.9, 5.1):
            out.append((name, assemble(text), constants(angle, frame=int(angle * 50)), inputs, triangles, 24, 4096, ZBASE))
    rng = random.Random(4242)
    while len(out) < 80:
        try:
            words = assemble('\n'.join(random_program(rng) + ['END']))
        except ValueError:
            continue
        vcount = rng.randrange(1, 33)
        tris = [tuple(rng.randrange(vcount) for _ in range(3)) for _ in range(rng.randrange(0, 6))]
        out.append(('random', words, [rng.getrandbits(32) for _ in range(32)],
                    [[rng.getrandbits(32) for _ in range(8)] for _ in range(vcount)], tris, vcount,
                    rng.choice([60, 400, 4096]), ZBASE))
    return out


def main():
    for i, job in enumerate(jobs()):
        name, program, consts, inputs, triangles, vcount, limit, zbase = job
        e = expected(job)
        words = [vcount, len(triangles), limit, zbase, i % 3]
        words += list(program) + [0] * (128 - len(program)) + list(consts)
        words += [w for row in inputs for w in row] + [a | b << 8 | c << 16 for a, b, c in triangles]
        words += [e[k] for k in KEYS]
        print(' '.join(f'{w & 0xffffffff:x}' for w in words))


if __name__ == '__main__':
    main()
