"""G2 RTL device alone: the oracle corpus through rv32_g3d.v with held and cancelled transfers.

Icarus runs a prefix of the corpus (it covers every scene, fault class and hold
pattern); Verilator (G2_SIM=verilator) runs all of it.
"""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_g3d_corpus import jobs  # noqa: E402

BUILD = ROOT / 'build/g3d'
SOURCES = ['tests/rv32_g3d_tb.sv', 'rtl/rv32/rv32_g3d.v', 'rtl/rv32/rv32_g3d_core.v']


class ShaderRTL(unittest.TestCase):
    def test_corpus(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        corpus = subprocess.run([sys.executable, 'tools/rv32_g3d_corpus.py'], cwd=ROOT, check=True,
                                capture_output=True, text=True).stdout.splitlines()
        self.assertEqual(len(corpus), len(jobs()))
        sim = os.environ.get('G2_SIM', 'icarus')
        if sim == 'icarus':
            # The scenes come first: every fault class, the three shaders, loops and raster edges.
            corpus = corpus[:20]
            subprocess.run(['iverilog', '-g2012', '-Wall', '-s', 'rv32_g3d_tb', '-o', str(BUILD / 'g3d.vvp'), *SOURCES],
                           cwd=ROOT, check=True)
            run = ['vvp', str(BUILD / 'g3d.vvp')]
        elif sim == 'verilator':
            with (BUILD / 'verilator-build.log').open('w') as log:
                subprocess.run(['verilator', '--binary', '--timing', '--top-module', 'rv32_g3d_tb', '--Mdir',
                                str(BUILD / 'verilator'), '-o', 'g3d_sim', *SOURCES], cwd=ROOT, stdout=log,
                               stderr=subprocess.STDOUT, check=True)
            run = [str(BUILD / 'verilator/g3d_sim')]
        else:
            self.fail(f'unknown G2_SIM {sim}')
        path = BUILD / f'corpus-{sim}.txt'
        path.write_text('\n'.join(corpus) + '\n')
        result = subprocess.run(run + [f'+input={path}', '+cancel=1'], cwd=ROOT, capture_output=True, text=True,
                                timeout=3600)
        self.assertEqual(result.returncode, 0, result.stdout[-3000:] + result.stderr[-3000:])
        self.assertIn(f'PASS {len(corpus)}', result.stdout)


if __name__ == '__main__':
    unittest.main()
