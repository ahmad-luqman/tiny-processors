"""Measure the committed digit model on MNIST test digits redrawn the way the
keyboard screen draws them, at several heights, with and without a resize to
MNIST's 20-pixel box. The numbers are the evidence in
docs/planning/digit-size-fix.md.

Each digit is binarized at 128, rescaled so its longer side is the given
height, thinned to a one-pixel skeleton (Zhang-Suen), and repainted with the
UI's 2x2 brush. Thinning after the resize is deliberate: a drawn stroke is one
brush wide whatever the digit's size, which is what the keyboard produces. The
"resized" column then scales the ink's bounding box back to 20 pixels (nearest
neighbour) before the unchanged preprocessing.

Like tools/digit_train.py this needs numpy, and nothing depends on it.

    python3 -m tools.digit_drawn_accuracy --count 2000
"""
import argparse

import numpy as np

from tools import digit_data, digit_ref

SIDE = 28


def thin(image):
    """Zhang-Suen thinning of a 0/1 image."""
    image = image.astype(np.uint8).copy()
    changed = True
    while changed:
        changed = False
        for step in (0, 1):
            p = np.pad(image, 1)
            n = [p[:-2, 1:-1], p[:-2, 2:], p[1:-1, 2:], p[2:, 2:],
                 p[2:, 1:-1], p[2:, :-2], p[1:-1, :-2], p[:-2, :-2]]   # P2..P9, clockwise from north
            p2, _, p4, _, p6, _, p8, _ = n
            neighbours = sum(x.astype(int) for x in n)
            transitions = sum(((n[i] == 0) & (n[(i + 1) % 8] == 1)).astype(int) for i in range(8))
            if step == 0:
                side = (p2 * p4 * p6 == 0) & (p4 * p6 * p8 == 0)
            else:
                side = (p2 * p4 * p8 == 0) & (p2 * p6 * p8 == 0)
            remove = (image == 1) & (neighbours >= 2) & (neighbours <= 6) & (transitions == 1) & side
            if remove.any():
                image[remove] = 0
                changed = True
    return image


def brush(skeleton):
    """The digit screen's 2x2 brush at every skeleton pixel."""
    out = np.zeros((SIDE, SIDE), np.uint8)
    for y, x in zip(*np.nonzero(skeleton)):
        out[y:y + 2, x:x + 2] = 255
    return out


def rescale(binary, height):
    """Nearest-neighbour resize of the ink box so its longer side is `height`, placed top-left."""
    ys, xs = np.nonzero(binary)
    crop = binary[ys.min():ys.max() + 1, xs.min():xs.max() + 1]
    rows, cols = crop.shape
    scale = height / max(rows, cols)
    new_rows, new_cols = max(1, round(rows * scale)), max(1, round(cols * scale))
    r = np.minimum((np.arange(new_rows) / scale).astype(int), rows - 1)
    c = np.minimum((np.arange(new_cols) / scale).astype(int), cols - 1)
    out = np.zeros((SIDE, SIDE), np.uint8)
    out[:new_rows, :new_cols] = crop[np.ix_(r, c)]
    return out


def resized(canvas):
    binary = (canvas > 0).astype(np.uint8)
    return rescale(binary, 20) * 255 if binary.any() else canvas


def accuracy(canvases, labels, model):
    correct = sum(digit_ref.classify_canvas(bytes(c.astype(np.uint8).ravel()), model)[0] == label
                  for c, label in zip(canvases, labels))
    return 100.0 * correct / len(labels)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--count', type=int, default=2000, help='test images to use, from the first')
    parser.add_argument('--heights', type=int, nargs='+', default=[10, 14, 20, 24, 28])
    args = parser.parse_args()
    model = digit_ref.load_model()
    images, labels = digit_data.load_test_set()
    images, labels = images[:args.count], labels[:args.count]
    originals = [np.frombuffer(i, np.uint8).reshape(SIDE, SIDE) for i in images]
    binaries = [(o >= 128).astype(np.uint8) for o in originals]
    print(f'original MNIST: {accuracy(originals, labels, model):.2f}%, '
          f'resized: {accuracy([resized(o) for o in originals], labels, model):.2f}%')
    print('height  today  resized')
    for height in args.heights:
        drawn = [brush(thin(rescale(b, height))) for b in binaries]
        print(f'{height:6d} {accuracy(drawn, labels, model):6.2f}% '
              f'{accuracy([resized(d) for d in drawn], labels, model):6.2f}%')


if __name__ == '__main__':
    main()
