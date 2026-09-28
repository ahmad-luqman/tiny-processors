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
    args = parser.parse_args()
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
    print('height  accuracy')
    for height in args.heights:
        print(f'{height:6d}  {100 * accuracy(drawn[height], labels, model):6.2f}%')


if __name__ == '__main__':
    main()
