#!/usr/bin/env python3
"""Generate the G2 guest headers from the Python oracle.

g3d_shaders.h  the three demo shaders, the cube and a Q16.16 sine table
g3d_scenes.h   g3dcheck's scenarios with the oracle's expected counters and hashes

The guest checks the device against these numbers directly: running the
integer C reference on an RV32I guest costs tens of millions of instructions
per frame (every 64-bit product is four software multiplies), which no RTL
replay could afford. The C reference is checked against the same oracle
natively instead (tests/test_rv32_3d.py).
"""
import argparse
import math
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools import rv32_g3d_model as M  # noqa: E402
from tools.rv32_g3d_model import assemble, encode, render  # noqa: E402
from tools.rv32_g3d_scene import SHADERS, constants, cube  # noqa: E402

ZBASE = 0x80040000


def display_hash(words, h=5381):
    """docs/rv32.md "Display": h = ((h << 5) + h) ^ word, over little-endian words."""
    for w in words:
        h = (((h << 5) + h) & 0xffffffff) ^ w
    return h


def fb_words(fb):
    return [fb[i] | fb[i + 1] << 8 | fb[i + 2] << 16 | fb[i + 3] << 24 for i in range(0, len(fb), 4)]


def z_words(zbuf):
    return [zbuf[i] | zbuf[i + 1] << 16 for i in range(0, len(zbuf), 2)]


def passthrough():
    return assemble('\n'.join(f'IN r{i}, {i}\nOUT {i}, r{i}' for i in range(7)) + '\nEND')


def vertex(x, y, z=0.5, w=1.0, r=255, g=255, b=255):
    q = [round(v * M.ONE) & 0xffffffff for v in (x, y, z, w)]
    return q + [(c << 16) & 0xffffffff for c in (r, g, b)] + [0]


def scenes():
    """(name, program, consts, inputs, triangles, vcount, limit, zbase)."""
    inputs, triangles = cube()
    out = [
        ('diffuse', assemble(SHADERS['diffuse']), constants(0.7), inputs, triangles, 24, 4096, ZBASE),
        ('toon', assemble(SHADERS['toon']), constants(2.2), inputs, triangles, 24, 4096, ZBASE),
        ('wobble', assemble(SHADERS['wobble']), constants(1.3, frame=37), inputs, triangles, 24, 4096, ZBASE),
    ]
    raster = [vertex(-.9, -.8, z=.2, r=250, g=10, b=0), vertex(.8, -.7, z=.9, r=0, g=0, b=200),
              vertex(.1, .9, z=.5, r=0, g=90, b=0), vertex(-4, -1, z=.95, r=40, g=40, b=40),
              vertex(4, -1, z=.95, r=40, g=40, b=40), vertex(0, 4, z=.95, r=40, g=40, b=40),
              vertex(0, 0, w=-1), vertex(.3, .3, z=.1, r=200, g=200, b=0)]
    out.append(('raster', passthrough(), [0] * 32, raster,
                [(0, 1, 2), (0, 2, 1), (3, 4, 5), (0, 1, 6), (7, 0, 1), (1, 1, 2), (0, 1, 2)], 8, 4096, ZBASE))
    # Divergent loops over a partial batch: lane v iterates v+1 times, with a BREAK two IFs deep.
    loops = assemble('''
        SPC r0, vid
        LDI r1, 0
        LDI r2, 1
        LOOP
            ADD r1, r1, r2
            SLT r0, r1
            IF
                LDI r3, 3
                SLT r3, r1
                IF
                    BREAK
                ENDIF
                BREAK
            ENDIF
        ENDLOOP
        SHL r1, r1, 14      ; x offset from the iteration count
        IN r4, 0
        ADD r4, r4, r1
        OUT 0, r4
        IN r4, 1
        OUT 1, r4
        IN r4, 2
        OUT 2, r4
        IN r4, 3
        OUT 3, r4
        IN r4, 4
        OUT 4, r4
        IN r4, 5
        OUT 5, r4
        IN r4, 6
        OUT 6, r4
        END''')
    six = [vertex(-.8, -.6, z=.3, r=255, g=0, b=0), vertex(.2, -.6, z=.3, r=0, g=255, b=0),
           vertex(-.3, .5, z=.3, r=0, g=0, b=255), vertex(-.6, -.2, z=.6, r=255, g=255, b=0),
           vertex(.3, -.3, z=.6, r=0, g=255, b=255), vertex(-.1, .7, z=.6, r=255, g=0, b=255)]
    out.append(('loops', loops, [0] * 32, six, [(0, 1, 2), (3, 4, 5)], 6, 4096, ZBASE))
    out.append(('limit', assemble('LOOP\nENDLOOP\nEND'), [0] * 32, six, [(0, 1, 2)], 6, 200, ZBASE))
    nine = assemble('LDI r0, 1\nSEQ r0, r0\nEND')[:2] + [encode('IF')] * 9
    out.append(('overflow', nine, [0] * 32, six, [], 6, 4096, ZBASE))
    out.append(('index', passthrough(), [0] * 32, six, [(0, 1, 2), (0, 1, 6)], 6, 4096, ZBASE))
    out.append(('zbase', passthrough(), [0] * 32, six, [(0, 1, 2)], 6, 4096, ZBASE + 2))
    return out


def expected(scene):
    name, program, consts, inputs, triangles, vcount, limit, zbase = scene
    r = render(program, consts, inputs, vcount, triangles, limit, zbase=zbase)
    fault = r['fault']
    return dict(status=M.G3D_FAULT if fault else M.G3D_DONE, error=fault.reason if fault else 0,
                fault_pc=fault.pc | fault.batch << 8 if fault and fault.reason not in (M.E_PARAM, M.E_INDEX) else 0,
                instructions=r['instructions'], transfers=r['transfers'], divides=r['divides'],
                pixels=r['pixels'], zfail=r['zfail'], culled=r['culled'], cycles=r['cycles'],
                fb_hash=display_hash(fb_words(r['fb'])), z_hash=display_hash(z_words(r['zbuf'])))


def words(values, per_line=8):
    values = [v & 0xffffffff for v in values]
    return '\n'.join('    ' + ', '.join(f'0x{v:08x}u' for v in values[i:i + per_line]) + ','
                     for i in range(0, len(values), per_line))


def shaders_header():
    inputs, triangles = cube()
    lines = ['/* Generated by tools/rv32_g3d_header.py; do not edit. */', '#include <stdint.h>']
    for name, text in SHADERS.items():
        program = assemble(text)
        lines += [f'#define G3D_{name.upper()}_WORDS {len(program)}u',
                  f'static const uint32_t g3d_{name}_program[{len(program)}] = {{', words(program), '};']
    lines += [f'#define G3D_CUBE_VERTICES {len(inputs)}u', f'#define G3D_CUBE_TRIANGLES {len(triangles)}u',
              'static const uint32_t g3d_cube_inputs[G3D_CUBE_VERTICES][8] = {']
    lines += ['    {' + ', '.join(f'0x{w:08x}u' for w in row) + '},' for row in inputs]
    lines += ['};', 'static const uint32_t g3d_cube_triangles[G3D_CUBE_TRIANGLES] = {',
              words([a | b << 8 | c << 16 for a, b, c in triangles]), '};']
    # sin(2*pi*i/256) in Q16.16; cos is the table a quarter turn on.
    sine = [round(math.sin(2 * math.pi * i / 256) * M.ONE) for i in range(256)]
    lines += ['static const int32_t g3d_sine[256] = {',
              '\n'.join('    ' + ', '.join(str(v) for v in sine[i:i + 8]) + ',' for i in range(0, 256, 8)), '};']
    return '\n'.join(lines) + '\n'


def scenes_header():
    lines = ['/* Generated by tools/rv32_g3d_header.py; do not edit. */', '#include <stdint.h>',
             'struct g3d_scene {', '    const char *name;',
             '    const uint32_t *program, *consts, *inputs, *triangles;',
             '    uint32_t program_words, vcount, tcount, zbase, limit;',
             '    uint32_t status, error, fault_pc, instructions, transfers, divides, pixels, zfail, culled, cycles;',
             '    uint32_t fb_hash, z_hash;', '};']
    table = []
    for i, scene in enumerate(scenes()):
        name, program, consts, inputs, triangles, vcount, limit, zbase = scene
        e = expected(scene)
        flat = [w for row in inputs for w in row]
        tri_words = [a | b << 8 | c << 16 for a, b, c in triangles] or [0]
        lines += [f'static const uint32_t scene{i}_program[{len(program)}] = {{', words(program), '};',
                  f'static const uint32_t scene{i}_consts[32] = {{', words(consts), '};',
                  f'static const uint32_t scene{i}_inputs[{len(flat)}] = {{', words(flat), '};',
                  f'static const uint32_t scene{i}_triangles[{len(tri_words)}] = {{', words(tri_words), '};']
        table.append(f'    {{"{name}", scene{i}_program, scene{i}_consts, scene{i}_inputs, scene{i}_triangles, '
                     f'{len(program)}u, {vcount}u, {len(triangles)}u, 0x{zbase:08x}u, {limit}u, '
                     + ', '.join(f'{e[k]}u' for k in ('status', 'error', 'fault_pc', 'instructions', 'transfers',
                                                      'divides', 'pixels', 'zfail', 'culled', 'cycles'))
                     + f', 0x{e["fb_hash"]:08x}u, 0x{e["z_hash"]:08x}u}},')
    lines += [f'#define G3D_SCENES {len(table)}u', 'static const struct g3d_scene g3d_scenes[G3D_SCENES] = {', *table, '};']
    return '\n'.join(lines) + '\n'


def write_if_changed(path, text):
    # Always touched, so make sees both targets newer than their dependencies.
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text)


def ensure_headers(out_dir):
    write_if_changed(Path(out_dir) / 'g3d_shaders.h', shaders_header())
    write_if_changed(Path(out_dir) / 'g3d_scenes.h', scenes_header())


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--out', default=str(ROOT / 'build/rv32'))
    ensure_headers(parser.parse_args().out)


if __name__ == '__main__':
    main()
