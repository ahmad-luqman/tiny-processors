"""Oracle integration and fail-closed differential harness tests."""
from pathlib import Path
import hashlib
import json
import subprocess
import tempfile
import unittest

from tools.fp32_vectors import ANCHORS, ROOT, oracle, requests, vector_text

REF = ROOT/'build/fp32/reference'
SIM = ROOT/'build/fp32/fp32.vvp'


class Fp32ToolsTest(unittest.TestCase):
    def test_vendored_reference_fingerprints(self):
        directory=ROOT/'third_party/softfloat'
        manifest=json.loads((directory/'SHA256SUMS.json').read_text())
        for name,digest in manifest.items():
            with self.subTest(name=name):
                self.assertEqual(hashlib.sha256((directory/name).read_bytes()).hexdigest(),digest)

    def test_literal_reference_anchors(self):
        self.assertEqual(oracle(REF,[r[:5] for r in ANCHORS]),[r[5:] for r in ANCHORS])

    def test_flags_do_not_accrue_between_reference_requests(self):
        rows=[(7,0,0x3f800000,0,0),(0,0,0x3f800000,0x3f800000,0)]
        self.assertEqual(oracle(REF,rows),[(0x7f800000,8,0),(0x40000000,0,0)])

    def test_oracle_rejects_malformed_input(self):
        for line in ('\n','0 0 0 0\n','0 0 0 0 0 extra\n','0 0 xyz 0 0\n',
                     '0 0 0 0 0','-1 0 0 0 0\n','4294967296 0 0 0 0\n',
                     '0 0 100000000 0 0\n','0 0 0z 0 0\n','0 8 0 0 0\n',
                     '0 0 '+('f'*300)+' 0 0\n'):
            with self.subTest(line=line):
                run=subprocess.run([str(REF)],input=line,text=True,capture_output=True,timeout=5)
                self.assertNotEqual(run.returncode,0)
                self.assertIn('malformed',run.stderr)

    def test_seeded_vectors_reproduce_and_cover_each_mode(self):
        rows=list(requests(7,2,list(range(18))))
        self.assertEqual(rows,list(requests(7,2,list(range(18)))))
        self.assertNotEqual(rows,list(requests(8,2,list(range(18)))))
        self.assertTrue({(op,rm) for op in range(18) for rm in range(5)} <= {(r[0],r[1]) for r in rows})

    def test_rtl_literal_anchors(self):
        with tempfile.TemporaryDirectory() as work:
            path=Path(work)/'anchors.txt'
            path.write_text(vector_text([r[:5] for r in ANCHORS],[r[5:] for r in ANCHORS]))
            run=subprocess.run(['vvp',str(SIM),f'+vectors={path}'],text=True,capture_output=True,timeout=20)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertIn(f'PASS fp32 vectors={len(ANCHORS)} ',run.stdout)

    def test_rtl_harness_rejects_empty_missing_malformed_and_wrong_results(self):
        with tempfile.TemporaryDirectory() as work:
            path=Path(work)/'bad.txt'
            cases=(None,'','nonsense\n','0 0 0 0 0\n','32 0 0 0 0 0 0 0\n',
                   '0 8 0 0 0 0 0 0\n','0 0 0 0 0 0 20 0\n','0 0 3f800000 3f800000 0 0 0 0\n')
            for contents in cases:
                with self.subTest(contents=contents):
                    if contents is not None: path.write_text(contents)
                    run=subprocess.run(['vvp',str(SIM),f'+vectors={path}'],text=True,capture_output=True,timeout=10)
                    self.assertNotEqual(run.returncode,0)
                    self.assertNotIn('PASS fp32',run.stdout)

    def test_runner_rejects_invalid_options_and_silent_success(self):
        with tempfile.TemporaryDirectory() as work:
            for args in (['--random','-1'],['--ops','18'],['--ops',''],
                         ['--simulator','/usr/bin/true','--anchors-only']):
                with self.subTest(args=args):
                    run=subprocess.run(['python3',str(ROOT/'tools/fp32_vectors.py'),'--work',work,*args],
                                       text=True,capture_output=True,timeout=20)
                    self.assertNotEqual(run.returncode,0)
                    self.assertIn('fp32:',run.stderr)


if __name__=='__main__':
    unittest.main()
