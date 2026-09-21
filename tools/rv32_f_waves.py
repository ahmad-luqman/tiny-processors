#!/usr/bin/env python3
"""Short F2 issue/completion/flags and canceled-request waveforms."""
from pathlib import Path
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_asm import CSRRS, CSRRWI, FINISH
from tools.rv32_f_asm import arithmetic, fli
from tools.rv32_rtl import write_image, run_rtl, run_emulator, diff_traces, has_value_changes


def main():
    out = ROOT/'build/rv32/f-waves'; out.mkdir(parents=True, exist_ok=True)
    words = fli(1, 0x3f800000) + fli(2, 0x40400000) + [CSRRWI(0, 2, 3), arithmetic(7, 7, rd=0), CSRRS(9, 1, 0)] + FINISH()
    hexfile, binary = write_image(words, out, 'floating')
    emu = run_emulator(ROOT/'build/rv32/rv32emu', binary, out/'floating.emu.trace')
    for name, reset_at in [('arithmetic', None), ('reset', 40)]:
        wave = out/(name+'.vcd')
        rtl = run_rtl(ROOT/'build/rv32/rv32_tb.vvp', hexfile, out/(name+'.trace'), wave=wave, reset_at=reset_at)
        if rtl.status or not rtl.halt or rtl.halt['outcome'] != 'pass': raise RuntimeError(rtl.stderr + rtl.noise)
        if not has_value_changes(wave.read_text()): raise RuntimeError(f'empty waveform: {wave}')
        if reset_at is None:
            mismatch = diff_traces(rtl.trace, emu.trace)
            if emu.status or mismatch: raise RuntimeError(f'floating waveform: {emu.stderr}; {mismatch}')
            if not any(line.endswith('f0=3eaaaaab fcsr=61') for line in rtl.trace): raise RuntimeError('missing rounded result and NX')
        else:
            start = [i for i,line in enumerate(rtl.trace) if line.split()[1] == '80000000'][-1]
            mismatch = diff_traces([line.split(' ',1)[1] for line in rtl.trace[start:]],
                                   [line.split(' ',1)[1] for line in emu.trace])
            if mismatch: raise RuntimeError(f'reset waveform replay: {mismatch}')
        print(f'{name}: {rtl.halt["cycles"]} cycles, {len(rtl.trace)} retirements; {wave}')


if __name__ == '__main__': main()
