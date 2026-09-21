"""Reproducible exact-bit/flag differential runner for standalone FP32 RTL."""
import argparse
import hashlib
import json
from pathlib import Path
import random
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
OPS = {
    'ADD': 0, 'SUB': 1, 'MUL': 2, 'FMADD': 3, 'FMSUB': 4, 'FNMSUB': 5,
    'FNMADD': 6, 'DIV': 7, 'SQRT': 8, 'I32_TO_F32': 9, 'U32_TO_F32': 10,
    'F32_TO_I32': 11, 'F32_TO_U32': 12, 'EQ': 13, 'LT': 14, 'LE': 15,
    'MIN': 16, 'MAX': 17,
}
ROUNDING = {'RNE': 0, 'RTZ': 1, 'RDN': 2, 'RUP': 3, 'RMM': 4}
FLAGS = {'NX': 1, 'UF': 2, 'OF': 4, 'DZ': 8, 'NV': 16}
MAX_OP = OPS["MAX"]
MAX_RM = ROUNDING["RMM"]

# Explicitly include both signs, both NaN classes, subnormal/normal boundaries,
# half-integers, integer conversion limits and operands around one.
EDGES = (0, 0x80000000, 1, 0x80000001, 0x007fffff, 0x807fffff,
         0x00800000, 0x80800000, 0x3f000000, 0xbf000000, 0x3f800000,
         0xbf800000, 0x3f800001, 0x3f7fffff, 0x40000000, 0xc0000000,
         0x7f7fffff, 0xff7fffff, 0x7f800000, 0xff800000, 0x7fc00000,
         0x7f800001, 0xffc12345, 0xff812345, 0x4f000000, 0xcf000000,
         0x4f800000, 0xcf800000, 0x3fc00000, 0x40200000)
# Independent literal anchors validate wiring, operation identities and flags
# in the oracle too. tuple: op,rm,a,b,c,result,flags,error.
ANCHORS = (
    (0,0,0x3f800000,0x3f800000,0,0x40000000,0,0),
    (1,2,0x3f800000,0x3f800000,0,0x80000000,0,0),
    (0,0,0x3f800000,0x33800000,0,0x3f800000,1,0),
    (0,4,0x3f800000,0x33800000,0,0x3f800001,1,0),
    (2,0,1,0x3f000000,0,0,3,0),
    (2,0,0x00800000,0x3f7fffff,0,0x00800000,3,0),
    (2,0,0x00800000,0x3f000000,0,0x00400000,0,0),
    (2,0,0x7f7fffff,0x40000000,0,0x7f800000,5,0),
    (2,1,0x7f7fffff,0x40000000,0,0x7f7fffff,5,0),
    (3,0,0x3f800001,0x3f7ffffe,0xbf800000,0xa8800000,0,0),
    (3,0,0x7f7fffff,0x40000000,0xff7fffff,0x7f7fffff,0,0),
    (3,0,0,0x7f800000,0x7fc00000,0x7fc00000,16,0),
    (4,0,0x40000000,0x40400000,0x3f800000,0x40a00000,0,0),
    (5,0,0x40000000,0x40400000,0x3f800000,0xc0a00000,0,0),
    (6,0,0x40000000,0x40400000,0x3f800000,0xc0e00000,0,0),
    (7,0,0x3f800000,0,0,0x7f800000,8,0),
    (7,0,0,0,0,0x7fc00000,16,0),
    (7,0,0x3f800000,0x40400000,0,0x3eaaaaab,1,0),
    (8,0,0x40800000,0,0,0x40000000,0,0),
    (8,0,0x80000000,0,0,0x80000000,0,0),
    (8,0,0xbf800000,0,0,0x7fc00000,16,0),
    (9,0,0xffffffff,0,0,0xbf800000,0,0),
    (10,0,0xffffffff,0,0,0x4f800000,1,0),
    (11,0,0x40200000,0,0,2,1,0),
    (11,4,0x40200000,0,0,3,1,0),
    (11,0,0x4f000000,0,0,0x7fffffff,16,0),
    (11,0,0xcf000000,0,0,0x80000000,0,0),
    (12,1,0xbf000000,0,0,0,1,0),
    (12,2,0xbf000000,0,0,0,16,0),
    (13,0,0x7fc00000,0,0,0,0,0),
    (14,0,0x7fc00000,0,0,0,16,0),
    (15,0,0x80000000,0,0,1,0,0),
    (16,0,0,0x80000000,0,0x80000000,0,0),
    (17,0,0,0x80000000,0,0,0,0),
    (16,0,0x7fc00000,0x3f800000,0,0x3f800000,0,0),
    (17,0,0x7f800001,0x3f800000,0,0x3f800000,16,0),
    (16,0,0x7fc00000,0xffc00000,0,0x7fc00000,0,0),
    (31,0,0,0,0,0,0,1), (0,7,0,0,0,0,0,1),
    (0,3,0x3f800000,0x33800000,0,0x3f800001,1,0),
    (4,2,0x40000000,0x3f000000,0x3f800000,0x80000000,0,0),
    (4,0,0x40000000,0x3f000000,0x3f800000,0,0,0),
    (9,0,0x01000001,0,0,0x4b800000,1,0),
    (10,3,0x01000001,0,0,0x4b800001,1,0),
    (11,0,0x4effffff,0,0,0x7fffff80,0,0),
    (12,0,0x4f7fffff,0,0,0xffffff00,0,0),
    (11,0,0xcf000001,0,0,0x80000000,16,0),
)

ANCHOR_REQUESTS = [row[:5] for row in ANCHORS]
ANCHOR_ANSWERS = [row[5:] for row in ANCHORS]


def requests(seed, random_count, operations):
    rng = random.Random(seed)
    yield from (row[:5] for row in ANCHORS if row[0] in operations or row[0] > MAX_OP)
    for op in operations:
        for rm in range(MAX_RM+1):
            if op in (8,9,10,11,12):
                yield from ((op,rm,a,0,0) for a in EDGES)
            else:
                for a in EDGES:
                    for b in EDGES:
                        yield op,rm,a,b,rng.choice(EDGES)
            for _ in range(random_count):
                values = [rng.getrandbits(32) if rng.randrange(2) else rng.choice(EDGES) for _ in range(3)]
                yield op,rm,*values
    for op in range(32):
        for rm in (5,6,7):
            yield op,rm,0x3f800000,0,0
    for op in range(MAX_OP+1,32):
        yield op,0,0,0,0


def cancellation_requests(seed, count):
    """Choose c near the exact product using Python integers, not host floats.

    These are inputs, not expected answers: SoftFloat still supplies every
    result/flag. Concentrating c here exercises information that a rounded
    multiply followed by add loses, across the full normal exponent range.
    """
    rng = random.Random(seed ^ 0xF1)
    for _ in range(count):
        ea = rng.randrange(1,255)
        eb = rng.randrange(max(1,128-ea),min(255,381-ea))
        ma, mb = rng.randrange(1<<23,1<<24), rng.randrange(1<<23,1<<24)
        product = ma*mb
        shift = product.bit_length()-24
        exp = ea+eb-254+product.bit_length()-47
        c_mag = ((exp+127)<<23) | ((product>>shift)&0x7fffff)
        c_mag = max(0,min(0x7f7fffff,c_mag+rng.randrange(-1,2)))
        sa,sb = rng.randrange(2),rng.randrange(2)
        a,b = (sa<<31)|(ea<<23)|(ma&0x7fffff), (sb<<31)|(eb<<23)|(mb&0x7fffff)
        for op in (OPS["FMADD"],OPS["FMSUB"],OPS["FNMSUB"],OPS["FNMADD"]):
            product_sign = sa ^ sb ^ (op in (OPS["FNMSUB"],OPS["FNMADD"]))
            c_sign = product_sign ^ 1 ^ (op in (OPS["FMSUB"],OPS["FNMADD"]))
            for rm in range(MAX_RM+1):
                yield op,rm,a,b,(c_sign<<31)|c_mag


def verify_reference_sources(directory=ROOT/'third_party/softfloat'):
    manifest=json.loads((directory/'SHA256SUMS.json').read_text())
    actual={str(path.relative_to(directory)) for path in directory.rglob('*')
            if path.is_file() and str(path.relative_to(directory)) not in ('README.md','SHA256SUMS.json')}
    if not isinstance(manifest,dict) or not manifest or set(manifest) != actual:
        raise ValueError('SoftFloat manifest must list every reference source file')
    for name,digest in manifest.items():
        if hashlib.sha256((directory/name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'SoftFloat fingerprint mismatch: {name}')


def run_process(command, *, timeout, **kwargs):
    try:
        return subprocess.run(command,text=True,capture_output=True,timeout=timeout,**kwargs)
    except subprocess.TimeoutExpired as exc:
        def decoded(value):
            return value.decode(errors='replace') if isinstance(value,bytes) else value or ''
        detail=(decoded(exc.stdout)+decoded(exc.stderr)).strip()
        raise ValueError(f'process timed out after {timeout}s: {command[0]}' +
                         (f'\n{detail}' if detail else '')) from exc


def simulator_command(simulator):
    sim=simulator.resolve()
    return ['vvp',str(sim)] if sim.suffix=='.vvp' else [str(sim)]


def run_protocol(simulator, wave=None):
    command=simulator_command(simulator)
    if wave: command.append(f'+wave={wave.resolve()}')
    run=run_process(command,timeout=120)
    print(run.stdout,end=''); print(run.stderr,end='',file=sys.stderr)
    match=re.search(r'^PASS fp32 protocol checks=([1-9][0-9]*) reset_states=([0-9a-f]+)$',run.stdout,re.M)
    if run.returncode or not match or int(match[2],16)==0:
        raise ValueError('protocol RTL failed or omitted its PASS record')


def oracle(reference, rows):
    text = ''.join(f'{op} {rm} {a:08x} {b:08x} {c:08x}\n' for op,rm,a,b,c in rows)
    run = run_process([str(reference)],input=text,timeout=120)
    if run.returncode:
        raise ValueError(f'oracle failed (exit {run.returncode}): {run.stderr.strip() or run.stdout.strip() or "no diagnostic"}')
    lines = run.stdout.splitlines()
    if len(lines) != len(rows):
        raise ValueError('oracle result count mismatch')
    answers=[]
    for line in lines:
        if not re.fullmatch(r'[0-9a-f]{8} [0-1][0-9a-f] [01]',line):
            raise ValueError(f'malformed oracle result: {line!r}')
        result,flags,error = line.split()
        answers.append((int(result,16),int(flags,16),int(error)))
    return answers


def vector_text(rows, answers):
    return ''.join(f'{op} {rm} {a:08x} {b:08x} {c:08x} {result:08x} {flags:02x} {error}\n'
                   for (op,rm,a,b,c),(result,flags,error) in zip(rows,answers,strict=True))


def main():
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--reference',type=Path,default=ROOT/'build/fp32/reference')
    p.add_argument('--simulator',type=Path)
    p.add_argument('--seed',type=int,default=20260921)
    p.add_argument('--random',type=int,default=100)
    p.add_argument('--ops',default=','.join(map(str,range(MAX_OP+1))))
    p.add_argument('--work',type=Path,default=ROOT/'build/fp32')
    p.add_argument('--wave',type=Path)
    p.add_argument('--anchors-only',action='store_true')
    p.add_argument('--cancellation',type=int,default=100)
    p.add_argument('--stats',action='store_true')
    p.add_argument('--protocol',action='store_true')
    p.add_argument('--check-reference',action='store_true')
    args=p.parse_args()
    try:
        if args.protocol:
            run_protocol(args.simulator or ROOT/'build/fp32/protocol.vvp',args.wave)
            return
        verify_reference_sources()
        if args.check_reference:
            print('PASS pinned SoftFloat source fingerprints')
            return
        if not re.fullmatch(r'[0-9]+(?:,[0-9]+)*',args.ops):
            raise ValueError('operations must be comma-separated codes 0..17')
        ops=list(map(int,args.ops.split(',')))
        if any(op>MAX_OP for op in ops):
            raise ValueError('operations must be 0..17')
        if args.random<0 or args.cancellation<0:
            raise ValueError('random and cancellation counts must be nonnegative')
        anchors=oracle(args.reference,ANCHOR_REQUESTS)
        if anchors != ANCHOR_ANSWERS:
            raise ValueError(f'oracle does not match literal anchors: {[(i,a,ANCHORS[i][5:]) for i,a in enumerate(anchors) if a != ANCHORS[i][5:]]}')
        rows=ANCHOR_REQUESTS if args.anchors_only else list(requests(args.seed,args.random,ops))
        if not args.anchors_only:
            rows.extend(row for row in cancellation_requests(args.seed,args.cancellation) if row[0] in ops)
        answers=oracle(args.reference,rows)
        args.work.mkdir(parents=True,exist_ok=True)
        path=args.work/f'vectors-{args.seed}.txt'
        path.write_text(vector_text(rows,answers))
        command=simulator_command(args.simulator or ROOT/'build/fp32/fp32.vvp')+[f'+vectors={path.resolve()}']
        if args.stats: command.append('+stats')
        if args.wave: command.append(f'+wave={args.wave.resolve()}')
        run=run_process(command,timeout=600)
        print(run.stdout,end=''); print(run.stderr,end='',file=sys.stderr)
        if run.returncode or not re.search(rf'^PASS fp32 vectors={len(rows)} ',run.stdout,re.M):
            match=re.search(r'vector (\d+) op=',run.stdout+run.stderr)
            if match:
                idx=int(match[1]); failure=args.work/f'failure-{args.seed}.txt'
                if idx < len(rows):
                    failure.write_text(vector_text(rows[idx:idx+1],answers[idx:idx+1]))
                    print(f'Isolated failing transaction: {failure}',file=sys.stderr)
            raise ValueError(f'RTL failed; seed={args.seed}, vectors={path}')
        (args.work/f'failure-{args.seed}.txt').unlink(missing_ok=True)
        print(f'Exact result/flag/error agreement; seed={args.seed}, operations={ops}')
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        p.exit(1,f'fp32: {exc}\n')

if __name__=='__main__':
    main()
