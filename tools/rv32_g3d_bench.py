#!/usr/bin/env python3
"""Measure one demo frame drawn by the C reference on the CPU and by the device.

Each variant is built at two frame counts and the difference is taken, so the
startup and the console output cancel (the guest prints fixed-width hex; see
programs/rv32/digitbench.c). A frame is the whole job on either side: clearing
the picture and the depth buffer, then shading, projecting and drawing the cube.
Emulator counts are retired instructions and RTL counts are clock cycles; they
are different quantities, reported separately and never divided into each other.
"""
import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.rv32_rtl import check_passed, run_emulator, run_rtl, write_image  # noqa: E402

SMALL, LARGE = 1, 3


def image_words(path):
    data = Path(path).read_bytes()
    return [int.from_bytes(data[i:i + 4], 'little') for i in range(0, len(data), 4)]


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--emulator', default='build/rv32/rv32emu')
    parser.add_argument('--simulator', default='build/verilator-rv32/rv32_sim')
    arguments = parser.parse_args()
    out = Path('build/g3d/bench')
    out.mkdir(parents=True, exist_ok=True)
    measured = {}
    for variant in ('cpu', 'hw'):
        for backend in ('emulator', 'rtl'):
            costs = {}
            for count in (SMALL, LARGE):
                name = f'g3dbench_{variant}_{count}'
                hex_path, bin_path = write_image(image_words(f'build/rv32/{name}.bin'), out, name)
                if backend == 'emulator':
                    run = run_emulator(arguments.emulator, bin_path, out / f'{name}.trace')
                else:
                    run = run_rtl(arguments.simulator, hex_path, out / f'{name}.rtl.trace', stall=0,
                                  max_cycles=600000000, timeout=3600)
                check_passed(run)
                costs[count] = run.halt['cycles'] if backend == 'rtl' else run.halt['steps']
            per = (costs[LARGE] - costs[SMALL]) // (LARGE - SMALL)
            measured[f'{variant}-{backend}'] = per
            unit = 'cycles' if backend == 'rtl' else 'instructions'
            print(f'{variant:3s} on {backend:8s}: {per:>11,} {unit} per frame')
    (out / 'measurements.json').write_text(json.dumps(measured, indent=1, sort_keys=True) + '\n')
    print(f'wrote {out / "measurements.json"}')


if __name__ == '__main__':
    main()
