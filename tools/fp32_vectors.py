"""Reproducible exact-bit/flag differential runner for standalone FP32 RTL."""
import argparse
from pathlib import Path
import random
import re
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
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
)


def requests(seed, random_count, operations):
    rng = random.Random(seed)
    yield from (row[:5] for row in ANCHORS if row[0] in operations or row[0] > 17)
    for op in operations:
        for rm in range(5):
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
    for op in range(18,32):
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
        eb = rng.randrange(max(1,128-ea),min(255,382-ea))
        ma, mb = rng.randrange(1<<23,1<<24), rng.randrange(1<<23,1<<24)
        product = ma*mb
        shift = product.bit_length()-24
        exp = ea+eb-254+product.bit_length()-47
        c_mag = ((exp+127)<<23) | ((product>>shift)&0x7fffff)
        c_mag = max(0,min(0x7f7fffff,c_mag+rng.randrange(-2,3)))
        sa,sb = rng.randrange(2),rng.randrange(2)
        a,b = (sa<<31)|(ea<<23)|(ma&0x7fffff), (sb<<31)|(eb<<23)|(mb&0x7fffff)
        for op in (3,4,5,6):
            product_sign = sa ^ sb ^ (op in (5,6))
            c_sign = product_sign ^ 1 ^ (op in (4,6))
            for rm in range(5):
                yield op,rm,a,b,(c_sign<<31)|c_mag


def oracle(reference, rows):
    text = ''.join(f'{op} {rm} {a:08x} {b:08x} {c:08x}\n' for op,rm,a,b,c in rows)
    run = subprocess.run([str(reference)],input=text,text=True,capture_output=True,timeout=120,check=True)
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
    p.add_argument('--simulator',type=Path,default=ROOT/'build/fp32/fp32.vvp')
    p.add_argument('--seed',type=int,default=20260921)
    p.add_argument('--random',type=int,default=100)
    p.add_argument('--ops',default=','.join(map(str,range(18))))
    p.add_argument('--work',type=Path,default=ROOT/'build/fp32')
    p.add_argument('--wave',type=Path)
    p.add_argument('--anchors-only',action='store_true')
    p.add_argument('--cancellation',type=int,default=100)
    p.add_argument('--stats',action='store_true')
    args=p.parse_args()
    try:
        ops=list(map(int,args.ops.split(',')))
        if not ops or any(op<0 or op>17 for op in ops) or args.random<0 or args.cancellation<0:
            raise ValueError('operations must be 0..17 and random count nonnegative')
        anchors=oracle(args.reference,[row[:5] for row in ANCHORS])
        if anchors != [row[5:] for row in ANCHORS]:
            raise ValueError(f'oracle does not match literal anchors: {[(i,a,ANCHORS[i][5:]) for i,a in enumerate(anchors) if a != ANCHORS[i][5:]]}')
        rows=[row[:5] for row in ANCHORS] if args.anchors_only else list(requests(args.seed,args.random,ops))
        if not args.anchors_only:
            rows.extend(row for row in cancellation_requests(args.seed,args.cancellation) if row[0] in ops)
        answers=oracle(args.reference,rows)
        args.work.mkdir(parents=True,exist_ok=True)
        path=args.work/f'vectors-{args.seed}.txt'
        path.write_text(vector_text(rows,answers))
        sim=args.simulator.resolve()
        command=(['vvp',str(sim)] if sim.suffix=='.vvp' else [str(sim)])+[f'+vectors={path.resolve()}']
        if args.stats: command.append('+stats')
        if args.wave: command.append(f'+wave={args.wave.resolve()}')
        run=subprocess.run(command,text=True,capture_output=True,timeout=600)
        print(run.stdout,end=''); print(run.stderr,end='',file=sys.stderr)
        if run.returncode or not re.search(rf'^PASS fp32 vectors={len(rows)} ',run.stdout,re.M):
            match=re.search(r'vector (\d+) op=',run.stdout+run.stderr)
            if match:
                idx=int(match[1]); failure=args.work/f'failure-{args.seed}.txt'
                failure.write_text(vector_text(rows[idx:idx+1],answers[idx:idx+1]))
                print(f'Isolated failing transaction: {failure}',file=sys.stderr)
            raise ValueError(f'RTL failed; seed={args.seed}, vectors={path}')
        print(f'Exact result/flag/error agreement; seed={args.seed}, operations={ops}')
    except (ValueError,OSError,subprocess.SubprocessError) as exc:
        p.exit(1,f'fp32: {exc}\n')

if __name__=='__main__':
    main()
