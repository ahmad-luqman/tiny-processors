"""G2 programmable 3D: executable contract, standard library only.

docs/rv32-3d.md is the prose contract; this module is the independent Python
oracle for it. It shares no code with the guest C reference
(programs/rv32/g3d_ref.c), the emulator device or the RTL, so agreement
between them is evidence rather than an echo.

Three layers, each usable on its own:
  assemble()      text -> 32-bit shader words, with structured nesting checked
  run_shader()    the 4-lane SIMT vertex stage with its typed mask stack
  render()        fixed-function projection, cull, Gouraud raster and Z test
render_float() repeats the fixed-function pipeline in doubles; it exists only
to measure what Q16.16 / S12.4 / 16-bit Z cost, never as a pass/fail oracle.
"""
import math

# Device window (docs/rv32-3d.md "Registers").
G3D_BASE = 0x20008000
G3D_SIZE = 0x2000
G3D_COMMAND, G3D_STATUS, G3D_ERROR, G3D_FAULT_PC = 0x00, 0x04, 0x08, 0x0c
G3D_CYCLES, G3D_STALLS, G3D_INSTRUCTIONS, G3D_TRANSFERS = 0x10, 0x14, 0x18, 0x1c
G3D_DIVIDES, G3D_PIXELS, G3D_ZFAIL, G3D_CULLED = 0x20, 0x24, 0x28, 0x2c
G3D_VCOUNT, G3D_TCOUNT, G3D_ZBASE, G3D_LIMIT = 0x40, 0x44, 0x48, 0x4c
G3D_CONST, G3D_PROGRAM, G3D_VERTEX, G3D_TRIANGLE = 0x400, 0x800, 0x1000, 0x1800
G3D_START, G3D_RESET, G3D_CLEAR_Z = 1, 2, 3
G3D_IDLE, G3D_BUSY, G3D_DONE, G3D_FAULT = 0, 1, 2, 4
# ERROR values; 0 is none.
(E_NONE, E_PARAM, E_INTERNAL, E_ILLEGAL, E_OVERFLOW, E_MISMATCH, E_LIMIT,
 E_PC, E_INDEX) = range(9)

LANES = 4
REGS = 16
PROGRAM_WORDS = 128
CONSTS = 32
SLOTS = 8            # vertex input slots and shader output slots per lane
VMAX = 32
TMAX = 64
DEPTH = 8            # mask-stack entries
WIDTH, HEIGHT = 320, 240
Z_BYTES = WIDTH * HEIGHT * 2
RAM_BASE, RAM_SIZE = 0x80000000, 0x400000
ONE = 1 << 16        # Q16.16
W_NEAR = ONE >> 4    # 1/16
GUARD = 4            # |x|,|y| <= GUARD*w
SUB = 16             # subpixels per pixel (S12.4)
HALF_W, HALF_H = WIDTH // 2 * SUB, HEIGHT // 2 * SUB
Z_MAX = 0xffff
# Output slots written by a vertex shader.
OUT_X, OUT_Y, OUT_Z, OUT_W, OUT_R, OUT_G, OUT_B = range(7)

OPS = ('END LDI LDC IN OUT SPC MOV ADD SUB MUL MAD MIN MAX ABS AND OR XOR SHL SRA ADDI '
       'SLT SEQ IF ELSE ENDIF LOOP ENDLOOP BREAK').split()
OP = {name: code for code, name in enumerate(OPS)}
IF_KIND, LOOP_KIND = 'IF', 'LOOP'


def s32(v):
    v &= 0xffffffff
    return v - (1 << 32) if v & 0x80000000 else v


def u32(v):
    return v & 0xffffffff


def trunc_div(n, d):
    """C99 division: truncate toward zero."""
    q = abs(n) // abs(d)
    return q if (n >= 0) == (d > 0) else -q


def div_sat(n, d):
    """The fixed-function divider: truncating n/d for d > 0, clamped to int32."""
    assert d > 0
    return max(-(1 << 31), min((1 << 31) - 1, trunc_div(n, d)))


def qmul(a, b):
    """Q16.16 product: bits [47:16] of the exact 64-bit product (floor)."""
    return s32((s32(a) * s32(b)) >> 16)


# ---------------------------------------------------------------- assembler

def encode(op, rd=0, ra=0, rb=0, rc=0, imm=0):
    return (OP[op] << 26 | (rd & 15) << 22 | (ra & 15) << 18 | (rb & 15) << 14
            | (rc & 15) << 10 | imm & 0x3fff)


def _reg(tok):
    if not (tok[:1] in 'rR' and tok[1:].isdigit() and int(tok[1:]) < REGS):
        raise ValueError(f'bad register {tok!r}')
    return int(tok[1:])


def _num(tok, lo, hi):
    v = int(tok, 0)
    if not lo <= v <= hi:
        raise ValueError(f'{tok} outside {lo}..{hi}')
    return v


def assemble(text):
    """One instruction per line, ';' comments. Returns 32-bit words.

    Nesting is checked here, and IF/ELSE/ENDLOOP targets are resolved to
    absolute PCs, so a program the assembler accepts can only fault at run
    time through the instruction limit or a data-dependent stack depth.
    """
    lines = []
    for raw in text.splitlines():
        body = raw.split(';', 1)[0].strip()
        if body:
            name, _, rest = body.partition(' ')
            lines.append((name.upper(), [t.strip() for t in rest.split(',') if t.strip()]))
    words, open_blocks, patches = [], [], []
    for pc, (name, args) in enumerate(lines):
        if name not in OP:
            raise ValueError(f'unknown mnemonic {name}')
        r = [_reg(a) for a in args] if name in ('MOV', 'ADD', 'SUB', 'MUL', 'MAD', 'MIN', 'MAX', 'ABS',
                                                'AND', 'OR', 'XOR', 'SLT', 'SEQ') else None
        if name in ('END', 'ENDIF', 'LOOP', 'BREAK'):
            w = encode(name)
        elif name == 'LDI':
            v = args[1]
            value = round(float(v[:-1]) * ONE) if v.endswith('f') else int(v, 0)
            if not -(1 << 21) <= value < 1 << 21:
                raise ValueError(f'LDI immediate {v} outside 22 signed bits')
            w = encode('LDI', _reg(args[0])) | value & 0x3fffff
        elif name == 'LDC':
            w = encode(name, _reg(args[0]), imm=_num(args[1], 0, CONSTS - 1))
        elif name == 'IN':
            w = encode(name, _reg(args[0]), imm=_num(args[1], 0, SLOTS - 1))
        elif name == 'OUT':
            w = encode(name, ra=_reg(args[1]), imm=_num(args[0], 0, SLOTS - 1))
        elif name == 'SPC':
            w = encode(name, _reg(args[0]), imm={'lane': 0, 'vid': 1, 'vcount': 2}[args[1].lower()])
        elif name in ('SHL', 'SRA'):
            w = encode(name, _reg(args[0]), _reg(args[1]), imm=_num(args[2], 0, 31))
        elif name == 'ADDI':
            w = encode(name, _reg(args[0]), _reg(args[1]), imm=_num(args[2], -(1 << 13), (1 << 13) - 1))
        elif name in ('MOV', 'ABS'):
            w = encode(name, r[0], r[1])
        elif name in ('SLT', 'SEQ'):
            w = encode(name, 0, r[0], r[1])
        elif name == 'MAD':
            w = encode(name, r[0], r[1], r[2], r[3])
        elif name in ('IF', 'ELSE', 'ENDLOOP'):
            w = encode(name)
        else:
            w = encode(name, r[0], r[1], r[2])
        words.append(w)
        # Structured nesting; the hardware re-checks at run time.
        if name == 'IF':
            open_blocks.append(['IF', pc, None])
        elif name == 'ELSE':
            if not open_blocks or open_blocks[-1][0] != 'IF' or open_blocks[-1][2] is not None:
                raise ValueError(f'ELSE at {pc} without open IF')
            patches.append((open_blocks[-1][1], pc))
            open_blocks[-1][2] = pc
        elif name == 'ENDIF':
            if not open_blocks or open_blocks[-1][0] != 'IF':
                raise ValueError(f'ENDIF at {pc} without open IF')
            _, at_if, at_else = open_blocks.pop()
            patches.append((at_else if at_else is not None else at_if, pc))
        elif name == 'LOOP':
            open_blocks.append(['LOOP', pc, None])
        elif name == 'ENDLOOP':
            if not open_blocks or open_blocks[-1][0] != 'LOOP':
                raise ValueError(f'ENDLOOP at {pc} without open LOOP')
            patches.append((pc, open_blocks.pop()[1] + 1))
        elif name == 'BREAK':
            if not any(b[0] == 'LOOP' for b in open_blocks):
                raise ValueError(f'BREAK at {pc} outside LOOP')
        if len(open_blocks) > DEPTH:
            raise ValueError(f'nesting deeper than {DEPTH} at {pc}')
    if open_blocks:
        raise ValueError(f'unclosed {open_blocks[-1][0]} at {open_blocks[-1][1]}')
    if not lines or lines[-1][0] != 'END':
        raise ValueError('program must end with END')
    if len(words) > PROGRAM_WORDS:
        raise ValueError(f'{len(words)} words exceed {PROGRAM_WORDS}')
    for at, target in patches:
        words[at] |= target
    return words


# ------------------------------------------------------------ shader stage

class Fault(Exception):
    def __init__(self, reason, pc=0, batch=0):
        super().__init__(reason)
        self.reason, self.pc, self.batch = reason, pc, batch
        self.instructions = 0


def break_lanes(stack, mask):
    """BREAK: the active lanes leave the innermost loop.

    `stack` is a list of [kind, parent, taken] entries, innermost last; `mask`
    is the 4-bit active mask. Every IF entry above the innermost LOOP must
    forget these lanes, or its ENDIF/ELSE would revive a lane that already left
    the loop. The LOOP entry itself keeps them: ENDLOOP restores its parent.
    Return the new active mask; raise Fault(E_MISMATCH) when no LOOP is open.
    """
    loops = [i for i, entry in enumerate(stack) if entry[0] == LOOP_KIND]
    if not loops:
        raise Fault(E_MISMATCH)
    # Clearing `parent` is enough: ELSE computes parent & ~taken, so a lane
    # missing from parent can come back through neither ELSE nor ENDIF.
    for entry in stack[loops[-1] + 1:]:
        entry[1] &= ~mask & 15
    return 0


def run_shader(program, consts, inputs, vcount, limit):
    """Run the vertex stage over `vcount` vertices in batches of four lanes.

    inputs[v][s] and the result's outputs[v][s] are 32-bit words. Returns
    (outputs, instructions). Raises Fault on any run-time fault.
    """
    program = list(program) + [0] * (PROGRAM_WORDS - len(program))
    outputs = [[0] * SLOTS for _ in range(vcount)]
    state = {'executed': 0}
    try:
        _batches(program, consts, inputs, vcount, limit, outputs, state)
    except Fault as fault:
        fault.instructions = state['executed']
        fault.outputs = outputs
        raise
    return outputs, state['executed']


def _batches(program, consts, inputs, vcount, limit, outputs, state):
    for batch in range((vcount + LANES - 1) // LANES):
        vids = [batch * LANES + lane for lane in range(LANES)]
        mask = sum(1 << lane for lane in range(LANES) if vids[lane] < vcount)
        regs = [[0] * REGS for _ in range(LANES)]
        pred = 0
        stack = []
        pc = count = 0
        while True:
            if count == limit:
                raise Fault(E_LIMIT, pc, batch)
            if pc >= PROGRAM_WORDS:
                raise Fault(E_PC, pc, batch)
            w = program[pc]
            op, rd, ra, rb, rc = w >> 26, w >> 22 & 15, w >> 18 & 15, w >> 14 & 15, w >> 10 & 15
            imm, target = w & 0x3fff, w & 0x7f
            if op >= len(OPS):
                raise Fault(E_ILLEGAL, pc, batch)
            name = OPS[op]
            count += 1
            state['executed'] += 1
            nxt = pc + 1
            if name == 'END':
                if stack:
                    raise Fault(E_MISMATCH, pc, batch)
                break
            elif name == 'IF':
                if len(stack) == DEPTH:
                    raise Fault(E_OVERFLOW, pc, batch)
                stack.append([IF_KIND, mask, mask & pred])
                mask &= pred
                if not mask:
                    nxt = target
            elif name == 'ELSE':
                if not stack or stack[-1][0] != IF_KIND:
                    raise Fault(E_MISMATCH, pc, batch)
                mask = stack[-1][1] & ~stack[-1][2] & 15
                if not mask:
                    nxt = target
            elif name == 'ENDIF':
                if not stack or stack[-1][0] != IF_KIND:
                    raise Fault(E_MISMATCH, pc, batch)
                mask = stack.pop()[1]
            elif name == 'LOOP':
                if len(stack) == DEPTH:
                    raise Fault(E_OVERFLOW, pc, batch)
                stack.append([LOOP_KIND, mask, 0])
            elif name == 'ENDLOOP':
                if not stack or stack[-1][0] != LOOP_KIND:
                    raise Fault(E_MISMATCH, pc, batch)
                if mask:
                    nxt = target
                else:
                    mask = stack.pop()[1]
            elif name == 'BREAK':
                try:
                    mask = break_lanes(stack, mask)
                except Fault as fault:
                    raise Fault(fault.reason, pc, batch) from None
            else:
                for lane in range(LANES):
                    if not mask >> lane & 1:
                        continue
                    r = regs[lane]
                    a, b = r[ra], r[rb]
                    if name in ('SLT', 'SEQ'):
                        hit = s32(a) < s32(b) if name == 'SLT' else a == b
                        pred = pred & ~(1 << lane) | int(hit) << lane
                        continue
                    if name == 'OUT':
                        outputs[vids[lane]][imm & 7] = a
                        continue
                    value = {
                        'LDI': lambda: u32(s32((w & 0x3fffff) << 10) >> 10),
                        'LDC': lambda: consts[imm & 31],
                        'IN': lambda: inputs[vids[lane]][imm & 7],
                        'SPC': lambda: (lane, vids[lane], vcount, 0)[imm & 3],
                        'MOV': lambda: a,
                        'ADD': lambda: u32(a + b),
                        'SUB': lambda: u32(a - b),
                        'MUL': lambda: u32(qmul(a, b)),
                        'MAD': lambda: u32(r[rc] + qmul(a, b)),
                        'MIN': lambda: a if s32(a) < s32(b) else b,
                        'MAX': lambda: a if s32(a) > s32(b) else b,
                        'ABS': lambda: u32(abs(s32(a))),
                        'AND': lambda: a & b,
                        'OR': lambda: a | b,
                        'XOR': lambda: a ^ b,
                        'SHL': lambda: u32(a << (imm & 31)),
                        'SRA': lambda: u32(s32(a) >> (imm & 31)),
                        'ADDI': lambda: u32(a + (imm - (1 << 14) if imm & 0x2000 else imm)),
                    }[name]()
                    r[rd] = value
            pc = nxt


# ------------------------------------------------------- fixed function

def project(out):
    """Clip-space outputs -> (sx, sy, z16, r, g, b) or None when culled."""
    x, y, z, w = (s32(out[i]) for i in (OUT_X, OUT_Y, OUT_Z, OUT_W))
    if w < W_NEAR or abs(x) > GUARD * w or abs(y) > GUARD * w or not 0 <= z <= w:
        return None
    sx = HALF_W + trunc_div(x * HALF_W, w)
    sy = HALF_H - trunc_div(y * HALF_H, w)
    zq = trunc_div(z * Z_MAX, w)
    color = [max(0, min(255, s32(out[i]) >> 16)) for i in (OUT_R, OUT_G, OUT_B)]
    return (sx, sy, zq, *color)


BAYER = (0, 8, 2, 10, 12, 4, 14, 6, 3, 11, 1, 9, 15, 7, 13, 5)


def rgb332(r, g, b):
    return (r & 0xe0) | (g >> 3 & 0x1c) | b >> 6


def dither(x, y, r, g, b):
    """Ordered 4x4 Bayer dither before truncation to RGB332: a threshold of up
    to one quantization step (32 for red/green, 64 for blue) keyed by position."""
    t = BAYER[(y & 3) * 4 + (x & 3)]
    return rgb332(min(255, r + 2 * t), min(255, g + 2 * t), min(255, b + 4 * t))


def edge(a, b, px, py):
    return (b[0] - a[0]) * (py - a[1]) - (b[1] - a[1]) * (px - a[0])


def covers(a, b, px, py):
    e = edge(a, b, px, py)
    dx, dy = b[0] - a[0], b[1] - a[1]
    return e > 0 or e == 0 and (dy < 0 or dy == 0 and dx > 0)


def triangle_setup(v0, v1, v2):
    """Front-facing screen triangles have negative area; normalize to positive.

    Returns (v0, v1, v2, area, gradients) or None when culled. Gradients are
    (gx, gy) pairs in Q16 attribute units per subpixel for z, r, g, b.
    """
    area = (v1[0] - v0[0]) * (v2[1] - v0[1]) - (v1[1] - v0[1]) * (v2[0] - v0[0])
    if area >= 0:
        return None
    v1, v2, area = v2, v1, -area
    ex1, ey1, ex2, ey2 = v1[0] - v0[0], v1[1] - v0[1], v2[0] - v0[0], v2[1] - v0[1]
    grads = []
    for k in range(2, 6):
        d1, d2 = v1[k] - v0[k], v2[k] - v0[k]
        grads.append((div_sat((d1 * ey2 - d2 * ey1) << 16, area),
                      div_sat((d2 * ex1 - d1 * ex2) << 16, area)))
    return v0, v1, v2, area, grads


DIVIDE_TICKS = 32


def valid_zbase(zbase):
    return zbase % 4 == 0 and RAM_BASE <= zbase and zbase - RAM_BASE + Z_BYTES <= RAM_SIZE


def render(program, consts, inputs, vcount, triangles, limit=4096, fb=None, zbuf=None, zbase=0x80040000):
    """The whole START job. `triangles` is a list of (i0, i1, i2).

    Returns a dict with fb (bytearray 76800), zbuf (list of 16-bit), the
    device counters, and 'fault' (None or Fault). On a fault fb/zbuf are
    untouched: every documented fault precedes the first memory transfer.

    'cycles' is the device's busy-tick count with no memory stalls, following
    the tick schedule in docs/rv32-3d.md "Time": the emulator and the RTL
    must reproduce it (plus STALLS on the RTL).
    """
    fb = bytearray(WIDTH * HEIGHT) if fb is None else bytearray(fb)
    zbuf = [Z_MAX] * (WIDTH * HEIGHT) if zbuf is None else list(zbuf)
    r = dict(fb=fb, zbuf=zbuf, fault=None, instructions=0, divides=0, pixels=0, zfail=0, culled=0,
             transfers=0, cycles=1)                          # the validate tick
    if not (1 <= vcount <= VMAX and 0 <= len(triangles) <= TMAX and 1 <= limit <= 0xffff and valid_zbase(zbase)):
        r['fault'] = Fault(E_PARAM)
        return r
    for t, tri in enumerate(triangles):
        r['cycles'] += 1                                     # one index tick per triangle
        if any(i >= vcount for i in tri):
            r['fault'] = Fault(E_INDEX)
            return r
    batches = (vcount + LANES - 1) // LANES
    try:
        outputs, r['instructions'] = run_shader(program, consts, inputs, vcount, limit)
        done = batches
    except Fault as fault:
        r['fault'] = fault
        r['instructions'] = fault.instructions
        outputs, done = fault.outputs, fault.batch
        # Batches before the faulting one were shaded and projected; the faulting
        # batch spent its setup tick, and a refused fetch spends one more tick.
        r['cycles'] += 1 + (fault.reason in (E_LIMIT, E_PC, E_ILLEGAL))
    verts = []
    for v in range(min(vcount, done * LANES)):
        verts.append(project(outputs[v]))
        r['divides'] += 3 if verts[-1] else 0
    r['cycles'] += done + r['instructions'] + len(verts)     # batch setup, instructions, vertex checks
    if r['fault']:
        r['cycles'] += DIVIDE_TICKS * r['divides']
        return r
    scanned = 0
    for tri in triangles:
        r['cycles'] += 1                                     # fetch
        vs = [verts[i] for i in tri]
        if None in vs:
            r['culled'] += 1
            continue
        r['cycles'] += 1                                     # area and scan box
        setup = triangle_setup(*vs)
        if setup is None:
            r['culled'] += 1
            continue
        r['divides'] += 8
        v0, v1, v2, _, grads = setup
        left = max(0, min(v[0] for v in (v0, v1, v2)) // SUB)
        right = min(WIDTH - 1, max(v[0] for v in (v0, v1, v2)) // SUB)
        top = max(0, min(v[1] for v in (v0, v1, v2)) // SUB)
        bottom = min(HEIGHT - 1, max(v[1] for v in (v0, v1, v2)) // SUB)
        for y in range(top, bottom + 1):
            py = y * SUB + SUB // 2
            for x in range(left, right + 1):
                scanned += 1
                px = x * SUB + SUB // 2
                if not (covers(v0, v1, px, py) and covers(v1, v2, px, py) and covers(v2, v0, px, py)):
                    continue
                attrs = []
                for k, (gx, gy), hi in zip(range(2, 6), grads, (Z_MAX, 255, 255, 255)):
                    value = v0[k] + ((gx * (px - v0[0]) + gy * (py - v0[1])) >> 16)
                    attrs.append(max(0, min(hi, value)))
                i = y * WIDTH + x
                r['transfers'] += 1
                if attrs[0] < zbuf[i]:
                    zbuf[i] = attrs[0]
                    fb[i] = dither(x, y, *attrs[1:])
                    r['pixels'] += 1
                    r['transfers'] += 2
                else:
                    r['zfail'] += 1
    r['cycles'] += DIVIDE_TICKS * r['divides'] + scanned + r['transfers'] + 1   # finish tick
    return r


def clear_cycles():
    """CLEAR_Z: validate, one word write per tick, finish."""
    return 1 + Z_BYTES // 4 + 1


# ------------------------------------------------- float reference

def render_float(outputs, triangles):
    """The fixed-function stages in doubles from the same shader outputs.

    No subpixel snapping, no truncated divides, no clamped gradients: the
    distance from render() measures only the fixed-function number formats.
    """
    fb = bytearray(WIDTH * HEIGHT)
    zbuf = [math.inf] * (WIDTH * HEIGHT)
    verts = []
    for out in outputs:
        x, y, z, w = (s32(out[i]) / ONE for i in (OUT_X, OUT_Y, OUT_Z, OUT_W))
        if w < W_NEAR / ONE or abs(x) > GUARD * w or abs(y) > GUARD * w or not 0 <= z <= w:
            verts.append(None)
            continue
        verts.append((WIDTH / 2 * (1 + x / w), HEIGHT / 2 * (1 - y / w), z / w,
                      *(min(255.0, max(0.0, s32(out[i]) / ONE)) for i in (OUT_R, OUT_G, OUT_B))))
    for tri in triangles:
        vs = [verts[i] for i in tri]
        if None in vs:
            continue
        v0, v1, v2 = vs
        area = (v1[0] - v0[0]) * (v2[1] - v0[1]) - (v1[1] - v0[1]) * (v2[0] - v0[0])
        if area >= 0:
            continue
        v1, v2, area = v2, v1, -area
        for y in range(max(0, math.floor(min(v[1] for v in vs))), min(HEIGHT, math.ceil(max(v[1] for v in vs)) + 1)):
            for x in range(max(0, math.floor(min(v[0] for v in vs))), min(WIDTH, math.ceil(max(v[0] for v in vs)) + 1)):
                px, py = x + 0.5, y + 0.5
                w0 = edge(v1, v2, px, py)
                w1 = edge(v2, v0, px, py)
                w2 = edge(v0, v1, px, py)
                if w0 < 0 or w1 < 0 or w2 < 0:
                    continue
                attrs = [(w0 * v0[k] + w1 * v1[k] + w2 * v2[k]) / area for k in range(2, 6)]
                i = y * WIDTH + x
                if attrs[0] < zbuf[i]:
                    zbuf[i] = attrs[0]
                    fb[i] = dither(x, y, *(int(c) for c in attrs[1:]))
    return fb
