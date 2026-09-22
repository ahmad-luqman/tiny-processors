#!/usr/bin/env python3
"""Measure one digit inference on the CPU and on the accelerator.

Each variant is built at two workload sizes and the difference is taken, so the
startup cost, the program-bank load and the console output all cancel and what
remains is the marginal cost of one classification. Emulator counts are retired
instructions; RTL counts are clock cycles. They are different quantities and are
reported separately, never divided into one another to claim a speedup.
"""

import argparse
import json
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tools.rv32_rtl import check_passed, run_emulator, run_rtl, write_image

SMALL, LARGE = 1, 5


def image_words(path):
    data = Path(path).read_bytes()
    return [int.from_bytes(data[i:i + 4], 'little') for i in range(0, len(data), 4)]


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--emulator', default='build/rv32/rv32emu')
    parser.add_argument('--simulator', default='build/verilator-rv32/rv32_sim')
    arguments = parser.parse_args()
    out = Path('build/digit/bench')
    out.mkdir(parents=True, exist_ok=True)
    measured = {}
    for variant in ('cpu', 'hw'):
        for backend in ('emulator', 'rtl'):
            costs = {}
            for count in (SMALL, LARGE):
                name = f'digitbench_{variant}_{count}'
                hex_path, bin_path = write_image(image_words(f'build/rv32/{name}.bin'), out, name)
                if backend == 'emulator':
                    run = run_emulator(arguments.emulator, bin_path, out / f'{name}.trace')
                else:
                    run = run_rtl(arguments.simulator, hex_path, out / f'{name}.rtl.trace',
                                  stall=0, max_cycles=400000000)
                check_passed(run)
                costs[count] = run.halt["cycles"] if backend == "rtl" else run.halt["steps"]
            per = (costs[LARGE] - costs[SMALL]) // (LARGE - SMALL)
            measured[f'{variant}-{backend}'] = per
            unit = 'cycles' if backend == 'rtl' else 'instructions'
            print(f'{variant:3s} on {backend:8s}: {per:>9,} {unit} per inference')
    (out / 'measurements.json').write_text(json.dumps(measured, indent=1, sort_keys=True) + '\n')
    print(f'wrote {out / "measurements.json"}')


if __name__ == '__main__':
    main()
