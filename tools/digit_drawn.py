#!/usr/bin/env python3
"""MNIST digits redrawn the way the keyboard screen draws them, in the standard library.

A digit drawn with the arrow keys differs from an MNIST image in three ways: it
is binary, its stroke is one brush wide whatever the digit's size, and it can be
any size. `draw` turns an MNIST image into such a drawing:

1. binarize at 128;
2. scale the ink box so its longer side is the chosen height, with the same
   integer nearest-neighbour rule as the preprocessing contract
   (`tools.digit_data.resize`);
3. thin to a one-pixel skeleton (Zhang-Suen);
4. repaint every skeleton pixel with the UI's 2x2 brush, at a random position,
   clipped at the canvas edge exactly as `programs/rv32/digit_ui.c` clips it.

Thinning after scaling is deliberate: the keyboard draws the same stroke width at
every size. Everything is integer arithmetic, so the bytes do not depend on the
platform, and the keyboard-style test set is pinned by its SHA-256.

One definition serves three users: the pinned test set that the acceptance test
measures (`keyboard_test_set`), the training augmentation in `tools/digit_train.py`,
and the height table in `tools/digit_drawn_accuracy.py`.

    python3 -m tools.digit_drawn          # generate the test set and check its digest
"""

import hashlib
import random

from tools.digit_data import PIXELS, SIDE, load_test_set, resize

HEIGHTS = (10, 14, 20, 24, 28)          # the heights the acceptance targets name
SEED = 20260929
# SHA-256 of the test set's canvases concatenated in `keyboard_test_set` order.
TEST_SET_DIGEST = 'a499728a360d315fd5507d77722bb8592cb476a384b76b1796bb5c0f7657f817'

# Zhang-Suen neighbours P2..P9, clockwise from north, as (row, column) offsets.
NEIGHBOURS = ((-1, 0), (-1, 1), (0, 1), (1, 1), (1, 0), (1, -1), (0, -1), (-1, -1))


def _removable(mask, step):
    """Zhang-Suen's deletion test for one neighbourhood; bit i of `mask` is P(i+2)."""
    p = [(mask >> i) & 1 for i in range(8)]
    p2, _, p4, _, p6, _, p8, _ = p
    count = sum(p)
    transitions = sum(1 for i in range(8) if p[i] == 0 and p[(i + 1) % 8] == 1)
    if step == 0:
        side = p2 * p4 * p6 == 0 and p4 * p6 * p8 == 0
    else:
        side = p2 * p4 * p8 == 0 and p2 * p6 * p8 == 0
    return 2 <= count <= 6 and transitions == 1 and side


# One lookup per sub-iteration, indexed by the 8-bit neighbourhood.
REMOVABLE = tuple(tuple(_removable(mask, step) for mask in range(256)) for step in (0, 1))


def binarize(image):
    """255 where the pixel is at least 128, else 0."""
    return bytes(255 if value >= 128 else 0 for value in image)


def thin(canvas):
    """Zhang-Suen thinning of the nonzero pixels; returns the skeleton as a set of (row, column).

    Pixels outside the canvas count as background. Each sub-iteration decides every
    deletion before applying any, as the algorithm requires.
    """
    ink = {(i // SIDE, i % SIDE) for i in range(PIXELS) if canvas[i]}
    changed = True
    while changed:
        changed = False
        for table in REMOVABLE:
            doomed = [(r, c) for r, c in ink
                      if table[sum(1 << bit for bit, (dr, dc) in enumerate(NEIGHBOURS)
                                   if (r + dr, c + dc) in ink)]]
            if doomed:
                ink.difference_update(doomed)
                changed = True
    return ink


def brush(points, down=0, across=0):
    """Paint the UI's 2x2 brush at each point shifted by (down, across), clipped at the edge."""
    out = bytearray(PIXELS)
    for r, c in points:
        for dr in (0, 1):
            y = r + down + dr
            if y >= SIDE:
                continue
            for dc in (0, 1):
                x = c + across + dc
                if x < SIDE:
                    out[y * SIDE + x] = 255
    return bytes(out)


def draw(image, height, rng):
    """A keyboard-style drawing of `image` whose ink box is `height` pixels on its longer side.

    The skeleton's position is drawn from `rng`, anywhere the cursor could have
    visited, so the brush clips at the right and bottom edges when a stroke ends on
    the last cell, as it does on the screen. A blank image stays blank.
    """
    skeleton = thin(resize(binarize(image), height))
    if not skeleton:
        return bytes(PIXELS)
    bottom = max(r for r, _ in skeleton)
    right = max(c for _, c in skeleton)
    return brush(skeleton, rng.randrange(SIDE - bottom), rng.randrange(SIDE - right))


def keyboard_test_set(images=None, heights=HEIGHTS, seed=SEED):
    """{height: [canvas per test image]} from the vendored test set, deterministically.

    Each drawing's position comes from its own generator, seeded by the height and
    the image's index, so the first N images at any height are exactly the first N
    of the pinned set: a partial run measures a true subset of it.
    """
    if images is None:
        images, _ = load_test_set()
    return {height: [draw(image, height, random.Random(f'{seed}:{height}:{index}'))
                     for index, image in enumerate(images)] for height in heights}


def digest(test_set):
    """SHA-256 of every canvas in height order, then image order."""
    hasher = hashlib.sha256()
    for height in sorted(test_set):
        for canvas in test_set[height]:
            hasher.update(canvas)
    return hasher.hexdigest()


def check_digest(test_set, pinned=None):
    """Return `test_set` if its digest is the pinned one, else refuse it."""
    pinned = pinned or TEST_SET_DIGEST
    found = digest(test_set)
    if found != pinned:
        raise ValueError(f'keyboard test set digest {found} differs from the pinned {pinned}')
    return test_set


def verified_test_set():
    """The full keyboard-style test set, refused unless it matches the pinned digest."""
    return check_digest(keyboard_test_set())


if __name__ == '__main__':
    print(digest(keyboard_test_set()))
