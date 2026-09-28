"""Measure the committed digit model on the keyboard-style test set, one row per height.

The drawings come from `tools.digit_drawn` (binarize, scale to the height, thin,
repaint with the UI's 2x2 brush), the same pinned set the acceptance test measures.
Standard library only.

    python3 -m tools.digit_drawn_accuracy              # all 10,000 digits per height
    python3 -m tools.digit_drawn_accuracy --count 2000 --heights 8 10 14
"""
import argparse

from tools import digit_data, digit_drawn, digit_ref


def accuracy(canvases, labels, model):
    correct = sum(digit_ref.classify_canvas(canvas, model)[0] == label
                  for canvas, label in zip(canvases, labels))
    return correct / len(labels)


def main():
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument('--count', type=int, default=None, help='test images to use, from the first')
    parser.add_argument('--heights', type=int, nargs='+', default=list(digit_drawn.HEIGHTS))
    parser.add_argument('--floor', type=float, default=0.85,
                        help='exit non-zero if any height scores below this (default: the acceptance target)')
    args = parser.parse_args()
    if args.count is not None and not 1 <= args.count <= 10000:
        parser.error('--count must be 1 to 10000')
    model = digit_ref.load_model()
    images, labels = digit_data.load_test_set()
    if args.count is not None:
        images, labels = images[:args.count], labels[:args.count]
    print(f'clean MNIST: {100 * accuracy(images, labels, model):.2f}%')
    if args.count is None and tuple(args.heights) == digit_drawn.HEIGHTS:
        drawn = digit_drawn.verified_test_set()
        print('keyboard-style set matches its pinned digest')
    else:
        drawn = digit_drawn.keyboard_test_set(images, args.heights, digit_drawn.SEED)
        print('UNVERIFIED: a subset or other heights, not the whole pinned set')
    print('height  accuracy')
    missed = []
    for height in args.heights:
        score = accuracy(drawn[height], labels, model)
        print(f'{height:6d}  {100 * score:6.2f}%')
        if score < args.floor:
            missed.append(height)
    if missed:
        raise SystemExit(f'below the {100 * args.floor:.0f}% floor at height(s) {missed}')


if __name__ == '__main__':
    main()
