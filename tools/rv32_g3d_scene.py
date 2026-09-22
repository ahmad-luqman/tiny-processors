"""G2 demo content: the cube mesh, camera matrices and the three vertex shaders.

Constant bank layout, shared by every shader (docs/rv32-3d.md "Constants"):
  c0..c15   MVP matrix, row-major; row i is (m[i][0], m[i][1], m[i][2], m[i][3])
  c16..c18  light direction in object space, unit length (the CPU rotates it,
            so shaders never transform normals)
  c19       ambient intensity
  c20       frame counter, raw integer (wobble)
  c21..c31  zero
Vertex input slots: 0..2 position, 3 base colour 0x00RRGGBB (raw), 4..6 normal.
"""
import math

from tools.rv32_g3d_model import ONE, CONSTS, SLOTS

FACES = (  # normal, base colour; the four corners are derived, counter-clockwise from outside
    ((1, 0, 0), 0xe04030), ((-1, 0, 0), 0x30c040), ((0, 1, 0), 0x3060e0),
    ((0, -1, 0), 0xe0c020), ((0, 0, 1), 0xc040c0), ((0, 0, -1), 0x20c0c0),
)


def q(v):
    return round(v * ONE) & 0xffffffff


def cube():
    """24 vertices (4 per face, so each face keeps its own normal) and 12 triangles."""
    inputs, triangles = [], []
    for normal, colour in FACES:
        n = normal
        # Two axes spanning the face, ordered so (u, v, n) is right-handed.
        u = (n[1], n[2], n[0]) if n[0] == 0 else (0, n[0], 0)
        u = tuple(abs(c) for c in u)
        v = (n[1] * u[2] - n[2] * u[1], n[2] * u[0] - n[0] * u[2], n[0] * u[1] - n[1] * u[0])
        base = len(inputs)
        for su, sv in ((-1, -1), (1, -1), (1, 1), (-1, 1)):
            p = [0.5 * (n[i] + su * u[i] + sv * v[i]) for i in range(3)]
            inputs.append([q(p[0]), q(p[1]), q(p[2]), colour, q(n[0]), q(n[1]), q(n[2]), 0])
        triangles += [(base, base + 1, base + 2), (base, base + 2, base + 3)]
    return inputs, triangles


def matmul(a, b):
    return [[sum(a[i][k] * b[k][j] for k in range(4)) for j in range(4)] for i in range(4)]


def camera(angle, distance=3.0, near=1.0, far=6.0, fov=60.0):
    """MVP for a cube spun about y then tilted about x, seen from +z; z_clip in [0, w]."""
    c, s = math.cos(angle), math.sin(angle)
    ct, st = math.cos(0.5), math.sin(0.5)
    spin = [[c, 0, s, 0], [0, 1, 0, 0], [-s, 0, c, 0], [0, 0, 0, 1]]
    tilt = [[1, 0, 0, 0], [0, ct, -st, 0], [0, st, ct, 0], [0, 0, 0, 1]]
    model = matmul(tilt, spin)
    view = [[1, 0, 0, 0], [0, 1, 0, 0], [0, 0, 1, -distance], [0, 0, 0, 1]]
    f = 1 / math.tan(math.radians(fov) / 2)
    a = far / (near - far)
    proj = [[f * 240 / 320, 0, 0, 0], [0, f, 0, 0], [0, 0, a, a * near], [0, 0, -1, 0]]
    return matmul(proj, matmul(view, model)), model


def constants(angle, frame=0, light=(0.3, 0.6, 0.8), ambient=0.25):
    mvp, model = camera(angle)
    length = math.sqrt(sum(c * c for c in light))
    world = [c / length for c in light]
    # Object-space light = model^T * world, since the model matrix is a rotation.
    local = [sum(model[k][i] * world[k] for k in range(3)) for i in range(3)]
    bank = [q(mvp[i][j]) for i in range(4) for j in range(4)] + [q(c) for c in local] + [q(ambient), frame]
    return bank + [0] * (CONSTS - len(bank))


_TRANSFORM = """
    IN r0, 0
    IN r1, 1
    IN r2, 2
{wobble}
    LDC r4, 3          ; x_clip = row 0 . (p, 1)
    LDC r5, 0
    MAD r4, r5, r0, r4
    LDC r5, 1
    MAD r4, r5, r1, r4
    LDC r5, 2
    MAD r4, r5, r2, r4
    OUT 0, r4
    LDC r4, 7          ; y_clip
    LDC r5, 4
    MAD r4, r5, r0, r4
    LDC r5, 5
    MAD r4, r5, r1, r4
    LDC r5, 6
    MAD r4, r5, r2, r4
    OUT 1, r4
    LDC r4, 11         ; z_clip
    LDC r5, 8
    MAD r4, r5, r0, r4
    LDC r5, 9
    MAD r4, r5, r1, r4
    LDC r5, 10
    MAD r4, r5, r2, r4
    OUT 2, r4
    LDC r4, 15         ; w_clip
    LDC r5, 12
    MAD r4, r5, r0, r4
    LDC r5, 13
    MAD r4, r5, r1, r4
    LDC r5, 14
    MAD r4, r5, r2, r4
    OUT 3, r4
    IN r0, 4           ; N . L into r6
    LDC r5, 16
    MUL r6, r0, r5
    IN r0, 5
    LDC r5, 17
    MAD r6, r0, r5, r6
    IN r0, 6
    LDC r5, 18
    MAD r6, r0, r5, r6
"""

_COLOUR = """
    IN r0, 3           ; base colour channels scaled by the intensity in r8
    LDI r1, 255
    SRA r2, r0, 16
    AND r2, r2, r1
    SHL r2, r2, 16
    MUL r2, r2, r8
    OUT 4, r2
    SRA r2, r0, 8
    AND r2, r2, r1
    SHL r2, r2, 16
    MUL r2, r2, r8
    OUT 5, r2
    AND r2, r0, r1
    SHL r2, r2, 16
    MUL r2, r2, r8
    OUT 6, r2
    END
"""

# Diffuse bends each face normal toward its corner (n + p, unnormalized) so the
# four corners of a face light differently and Gouraud interpolation shows.
_ROUNDED = """
    IN r0, 0           ; N.L with N = n + p, into r6
    IN r5, 4
    ADD r0, r0, r5
    LDC r5, 16
    MUL r6, r0, r5
    IN r0, 1
    IN r5, 5
    ADD r0, r0, r5
    LDC r5, 17
    MAD r6, r0, r5, r6
    IN r0, 2
    IN r5, 6
    ADD r0, r0, r5
    LDC r5, 18
    MAD r6, r0, r5, r6
"""
DIFFUSE = _TRANSFORM.format(wobble='') + _ROUNDED + """
    LDI r7, 0
    MAX r8, r6, r7     ; intensity = min(1, max(0, N.L) + ambient)
    LDC r7, 19
    ADD r8, r8, r7
    LDI r7, 1.0f
    MIN r8, r8, r7
""" + _COLOUR

# Toon: three bands chosen per vertex, so lanes of one batch diverge.
TOON = _TRANSFORM.format(wobble='') + """
    LDI r7, 0.5f
    SLT r7, r6         ; P = 0.5 < N.L
    IF
        LDI r8, 1.0f
    ELSE
        LDI r7, 0.1f
        SLT r7, r6     ; P = 0.1 < N.L
        IF
            LDI r8, 0.6f
        ELSE
            LDI r8, 0.3f
        ENDIF
    ENDIF
""" + _COLOUR

# Wobble: each vertex is pushed along its normal by a triangle wave of the frame
# counter and its vertex id; the loop folds the phase into range per lane, so
# lanes leave the loop after different iteration counts.
_WOBBLE = """
    LDC r3, 20         ; phase = frame + 5 * vid, raw
    SPC r4, vid
    SHL r5, r4, 2
    ADD r4, r4, r5
    ADD r3, r3, r4
    LDI r4, 32
    LOOP               ; phase mod 32 by repeated subtraction; lanes exit separately
        SLT r3, r4     ; P = phase < 32
        IF
            BREAK
        ENDIF
        SUB r3, r3, r4
    ENDLOOP
    LDI r4, 16         ; triangle wave: t = phase < 16 ? phase : 32 - phase
    SLT r3, r4
    IF
    ELSE
        LDI r5, 32
        SUB r3, r5, r3
    ENDIF
    SHL r3, r3, 10     ; amplitude: t/64 in Q16.16, up to 0.25
    IN r4, 4
    MAD r0, r4, r3, r0
    IN r4, 5
    MAD r1, r4, r3, r1
    IN r4, 6
    MAD r2, r4, r3, r2
"""
WOBBLE = _TRANSFORM.format(wobble=_WOBBLE) + """
    LDI r7, 0
    MAX r8, r6, r7
    LDC r7, 19
    ADD r8, r8, r7
    LDI r7, 1.0f
    MIN r8, r8, r7
""" + _COLOUR

SHADERS = {'diffuse': DIFFUSE, 'toon': TOON, 'wobble': WOBBLE}
