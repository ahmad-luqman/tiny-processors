"""Independent oracle anchors, contract pins, and fail-closed harness checks."""
from fractions import Fraction
from pathlib import Path
import hashlib
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from tools.fp32_vectors import (ANCHORS, ANCHOR_REQUESTS, ANCHOR_ANSWERS, OPS,
    ROUNDING, FLAGS, MAX_OP, MAX_RM, ROOT, cancellation_requests, oracle, requests, run_process,
    simulator_command, vector_text, verify_reference_sources)

REF = ROOT/'build/fp32/reference'
SIM = Path(os.environ.get('FP32_TEST_SIM', ROOT/'build/fp32/fp32.vvp')).resolve()
RUNNER = ROOT/'tools/fp32_vectors.py'


def exact_float(bits):
    exponent=(bits>>23)&255
    significand=(bits&0x7fffff) | ((1<<23) if exponent else 0)
    return (-1 if bits>>31 else 1)*Fraction(significand)*Fraction(2)**(max(1,exponent)-150)


class Fp32ToolsTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for path in (REF,SIM):
            if not path.is_file():
                raise RuntimeError(f'missing test prerequisite: {path}; build the FP32 test targets first')

    def run_main(self, work, *args):
        return subprocess.run([sys.executable,str(RUNNER),'--work',str(work),
                               '--simulator',str(SIM),*args],text=True,capture_output=True,timeout=30)

    def stub(self, work, name, body):
        path=Path(work)/name
        path.write_text(f'#!{sys.executable}\n'+body)
        path.chmod(0o700)
        return path

    def test_vendored_reference_fingerprints(self):
        verify_reference_sources()

    def test_manifest_rejects_empty_missing_extra_and_changed_sources(self):
        with tempfile.TemporaryDirectory() as work:
            directory=Path(work)
            (directory/'a.c').write_text('reference')
            manifest=directory/'SHA256SUMS.json'
            manifest.write_text('{}')
            with self.assertRaisesRegex(ValueError,'manifest must list every'):
                verify_reference_sources(directory)
            digest=hashlib.sha256(b'reference').hexdigest()
            manifest.write_text(json.dumps({'a.c':digest}))
            verify_reference_sources(directory)
            (directory/'stray.c').write_text('extra')
            with self.assertRaisesRegex(ValueError,'manifest must list every'):
                verify_reference_sources(directory)
            (directory/'stray.c').unlink()
            (directory/'a.c').write_text('changed')
            with self.assertRaisesRegex(ValueError,'fingerprint mismatch: a.c'):
                verify_reference_sources(directory)
            (directory/'a.c').unlink()
            with self.assertRaisesRegex(ValueError,'manifest must list every'):
                verify_reference_sources(directory)

    def test_encodings_match_documentation_rtl_and_reference(self):
        rtl=(ROOT/'rtl/fp32/fp32.v').read_text() + (ROOT/'rtl/fp32/fp32_ops.vh').read_text()
        reference=(ROOT/'tools/fp32_ref.c').read_text()
        docs=(ROOT/'docs/fp32.md').read_text()
        doc_codes={name:int(code) for code,name in re.findall(r'^\| (\d+) \| ([A-Z0-9_]+) \|',docs,re.M)}
        for prefix,mapping in [('OP_',OPS),('RM_',ROUNDING)]:
            rtl_codes={name:int(code) for name,code in re.findall(prefix+r'([A-Z0-9_]+)=\d+\x27d(\d+)',rtl)}
            self.assertEqual(rtl_codes,mapping)
            self.assertEqual({name:doc_codes.get(name) for name in mapping},mapping)
        self.assertEqual({name:int(code) for name,code in re.findall(r'OP_([A-Z0-9_]+)=(\d+)',(ROOT/'tools/rv32_fp.h').read_text())},OPS)
        self.assertEqual({name:int(code,16) for name,code in re.findall(r'FLAG_([A-Z]+)=5\x27h([0-9a-f]+)',rtl)},FLAGS)
        self.assertEqual({name:int(code,16) for code,name in re.findall(r'^\| 0x([0-9a-f]+) \| ([A-Z]+) \|',docs,re.M)},FLAGS)
        self.assertIn('NV,DZ,OF,UF,NX (bits 4 through 0)',docs)
        self.assertIn('rv32_fp(op, rm, a, b, c, &flags)', reference)
        self.assertIn('`include "fp32_ops.vh"', (ROOT/'rtl/rv32/rv32_fdecode.v').read_text())
        self.assertEqual(MAX_OP,max(OPS.values()))
        self.assertEqual(MAX_RM,max(ROUNDING.values()))
        self.assertIn('operation > OP_MAX || mode > RM_RMM',rtl)
        self.assertIn('op > OP_MAX || rm > softfloat_round_near_maxMag',reference)
        for name,equation in {'FMADD':'a*b+c','FMSUB':'a*b-c','FNMSUB':'-a*b+c','FNMADD':'-a*b-c'}.items():
            self.assertIn(f'| {OPS[name]} | {name} | `{equation}` |',docs)

    def test_state_coverage_bounds_follow_the_rtl(self):
        header=(ROOT/'rtl/fp32/fp32_states.vh').read_text()
        states={name:int(value) for name,value in re.findall(r'([A-Z_]+)=(\d+)',header)}
        self.assertEqual(set(states.values()),set(range(states['RESPONSE']+1)))
        self.assertEqual(len(states),states['RESPONSE']+1)
        self.assertIn("STATE_COUNT = {28'b0,RESPONSE} + 32'd1",header)

    def test_cancellation_inputs_really_cancel_each_fused_variant(self):
        rows=list(cancellation_requests(424242,1000))
        self.assertEqual(len(rows),20000)
        for op,rm,a,b,c in rows:
            product=exact_float(a)*exact_float(b)
            third=exact_float(c)
            # Independently spell out the four mathematical operations instead
            # of duplicating the generator's sign-bit XOR table.
            residual={OPS['FMADD']:product+third,OPS['FMSUB']:product-third,
                      OPS['FNMSUB']:-product+third,OPS['FNMADD']:-product-third}[op]
            magnitude=abs(product)
            exp=magnitude.numerator.bit_length()-magnitude.denominator.bit_length()
            if magnitude < Fraction(2)**exp: exp-=1
            ulp=Fraction(2)**(exp-23)
            self.assertLessEqual(abs(residual),2*ulp,(op,rm,hex(a),hex(b),hex(c)))

    def test_literal_reference_anchors(self):
        self.assertEqual(oracle(REF,ANCHOR_REQUESTS),ANCHOR_ANSWERS)
        self.assertEqual({row[1] for row in ANCHORS if row[1]<=MAX_RM},set(ROUNDING.values()))
        self.assertEqual({row[0] for row in ANCHORS if row[0]<=MAX_OP},set(OPS.values()))

    def test_flags_do_not_accrue_between_reference_requests(self):
        self.assertEqual(oracle(REF,[(7,0,0x3f800000,0,0),(0,0,0x3f800000,0x3f800000,0)]),
                         [(0x7f800000,8,0),(0x40000000,0,0)])

    def test_oracle_rejects_malformed_input(self):
        for line in ('\n','0 0 0 0\n','0 0 0 0 0 extra\n','0 0 xyz 0 0\n',
                     '0 0 0 0 0','-1 0 0 0 0\n','4294967296 0 0 0 0\n',
                     '0 0 100000000 0 0\n','0 0 0z 0 0\n','0 8 0 0 0\n',
                     '0 0 '+('f'*300)+' 0 0\n'):
            with self.subTest(line=line):
                run=subprocess.run([str(REF)],input=line,text=True,capture_output=True,timeout=5)
                self.assertEqual(run.returncode,2)
                self.assertIn('malformed oracle request',run.stderr)

    def test_oracle_diagnostic_reaches_the_cli(self):
        with tempfile.TemporaryDirectory() as work:
            stub=self.stub(work,'bad-reference','import sys\nprint("malformed oracle request: test diagnostic",file=sys.stderr)\nsys.exit(2)\n')
            run=self.run_main(work,'--reference',str(stub),'--anchors-only')
            self.assertEqual(run.returncode,1)
            self.assertIn('oracle failed (exit 2): malformed oracle request: test diagnostic',run.stderr)

    def test_timeout_keeps_partial_output(self):
        timeout=subprocess.TimeoutExpired(['simulator'],3,output=b'partial stdout\n',stderr=b'partial stderr\n')
        with patch('tools.fp32_vectors.subprocess.run',side_effect=timeout):
            with self.assertRaisesRegex(ValueError,'(?s)timed out.*partial stdout.*partial stderr'):
                run_process(['simulator'],timeout=3)

    def test_seeded_vectors_reproduce_and_cover_each_mode(self):
        rows=list(requests(7,2,list(OPS.values())))
        self.assertEqual(rows,list(requests(7,2,list(OPS.values()))))
        self.assertNotEqual(rows,list(requests(8,2,list(OPS.values()))))
        self.assertTrue({(op,rm) for op in OPS.values() for rm in ROUNDING.values()} <= {(r[0],r[1]) for r in rows})

    def test_rtl_literal_anchors(self):
        with tempfile.TemporaryDirectory() as work:
            path=Path(work)/'anchors.txt'
            path.write_text(vector_text(ANCHOR_REQUESTS,ANCHOR_ANSWERS))
            run=subprocess.run(simulator_command(SIM)+[f'+vectors={path}'],text=True,capture_output=True,timeout=20)
            self.assertEqual(run.returncode,0,run.stdout+run.stderr)
            self.assertIn(f'PASS fp32 vectors={len(ANCHORS)} ',run.stdout)

    def test_rtl_rejects_each_malformed_case_for_the_right_reason(self):
        cases=[(None,'cannot open vectors'),('','empty vector file'),
               ('nonsense\n','malformed vector'),('0 0 0 0 0\n','malformed vector'),
               ('0 0 0 0 0 0 0 0 extra\n','malformed vector'),
               ('0 0 0 0 0 0 0 0','unterminated or overlong line'),
               ('32 0 0 0 0 0 0 0\n','out-of-range vector'),
               ('0 8 0 0 0 0 0 0\n','out-of-range vector'),
               ('0 0 0 0 0 0 20 0\n','out-of-range vector')]
        correct='0 0 3f800000 3f800000 0 40000000 00 0'.split()
        for index in (2,3,4,5,6):
            for token in ('100000000','x','z','000000000'):
                fields=correct.copy(); fields[index]=token
                cases.append((' '.join(fields)+'\n','malformed vector'))
        for index,bad in ((5,'0'),(6,'01'),(7,'1')):
            fields=correct.copy(); fields[index]=bad
            cases.append((' '.join(fields)+'\n','vector 0 op=0'))
        with tempfile.TemporaryDirectory() as work:
            path=Path(work)/'bad.txt'
            for contents,diagnostic in cases:
                with self.subTest(contents=contents):
                    if contents is not None: path.write_text(contents)
                    run=subprocess.run(simulator_command(SIM)+[f'+vectors={path}'],text=True,capture_output=True,timeout=10)
                    self.assertNotEqual(run.returncode,0)
                    self.assertIn(diagnostic,run.stdout+run.stderr)
                    self.assertNotIn('PASS fp32',run.stdout)

    def test_runner_rejects_invalid_options_and_silent_success(self):
        with tempfile.TemporaryDirectory() as work:
            cases=[(['--random','-1'],'counts must be nonnegative'),
                   (['--ops','18'],'operations must be'),(['--ops',''],'operations must be'),
                   (['--simulator','/usr/bin/true','--anchors-only'],'RTL failed'),
                   (['--simulator','/usr/bin/true','--protocol'],'protocol RTL failed')]
            for args,diagnostic in cases:
                with self.subTest(args=args):
                    run=self.run_main(work,*args)
                    self.assertEqual(run.returncode,1)
                    self.assertIn(diagnostic,run.stderr)

    def test_failure_isolated_reproduced_and_removed_after_success(self):
        with tempfile.TemporaryDirectory() as work:
            state=Path(work)/'called'
            stub=self.stub(work,'reference-with-one-bad-answer',f'''import pathlib,subprocess,sys
run=subprocess.run([{str(REF)!r}],input=sys.stdin.read(),text=True,capture_output=True,check=True)
lines=run.stdout.splitlines()
state=pathlib.Path({str(state)!r})
if state.exists():
    fields=lines[0].split(); fields[0]=f"{{int(fields[0],16)^1:08x}}"; lines[0]=" ".join(fields)
state.touch()
print("\\n".join(lines))
''')
            run=self.run_main(work,'--reference',str(stub),'--anchors-only')
            self.assertEqual(run.returncode,1)
            self.assertIn('Isolated failing transaction:',run.stderr)
            failure=Path(work)/'failure-20260921.txt'
            self.assertTrue(failure.is_file())
            self.assertEqual(len(failure.read_text().splitlines()),1)
            replay=subprocess.run(simulator_command(SIM)+[f'+vectors={failure}'],text=True,capture_output=True,timeout=10)
            self.assertNotEqual(replay.returncode,0)
            self.assertIn('vector 0 op=0',replay.stdout+replay.stderr)
            good=self.run_main(work,'--anchors-only')
            self.assertEqual(good.returncode,0,good.stdout+good.stderr)
            self.assertFalse(failure.exists())


if __name__=='__main__':
    unittest.main()
