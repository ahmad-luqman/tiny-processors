"""G2 RTL device alone: the oracle corpus through rv32_g3d.v with held and cancelled transfers.

Icarus runs the corpus prefix (tools/rv32_g3d_corpus.ICARUS_JOBS jobs: the
g3dcheck scenes and one job for every fault class, including the illegal-opcode,
control-mismatch and PC faults only raw words reach). Verilator
(G2_SIM=verilator) runs all of it, including the jobs reset mid-transfer.
"""
import os
from pathlib import Path
import subprocess
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_g3d_corpus import ICARUS_JOBS, job_line, jobs  # noqa: E402

BUILD = ROOT / 'build/g3d'
SOURCES = ['tests/rv32_g3d_tb.sv', 'rtl/rv32/rv32_g3d.v', 'rtl/rv32/rv32_g3d_core.v']


class ShaderRTL(unittest.TestCase):
    def test_corpus(self):
        BUILD.mkdir(parents=True, exist_ok=True)
        corpus = [job_line(scene, flags) for scene, flags in jobs()]
        sim = os.environ.get('G2_SIM', 'icarus')
        if sim == 'icarus':
            corpus = corpus[:ICARUS_JOBS]
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
