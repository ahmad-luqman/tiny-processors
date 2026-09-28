#!/usr/bin/env python3
"""Train the N1 digit model once and export its integer weights. NOT part of any build.

This is the only file in the repository that imports a third-party package (numpy),
and the only one that triggers network access: the download itself is
`tools.digit_data.fetch_training_set`, which nothing else calls. No make target runs
either of them. Everything the
build, the tests and the firmware need is the committed output,
`programs/rv32/digit_model.json`, read back by the standard-library oracle in
`tools/digit_ref.py`. Rerun this only to retrain:

    python3 -m tools.digit_train                # train, quantize, export
    python3 -m tools.digit_train --dry-run      # train and report, write nothing

The network is a 196 -> 32 -> 10 multilayer perceptron with ReLU, trained on the
MNIST training set after the same `tools.digit_data.prepare` preprocessing the
guest applies to a drawn canvas. Each training image is used as distributed and
also redrawn keyboard-style by `tools.digit_drawn.draw` (binary, one brush wide,
at random heights from 8 to 28 pixels), because that is what the screen produces.
The last 5,000 training images, with their drawings, are held out to calibrate
the hidden shift `s1`; the vendored 10,000-image test set and the keyboard-style
set built from it tune nothing and are only measured at the end.

Quantization (the contract `docs/rv32-digit.md` states and the exporter proves):

    x       uint8 0..255, scale sx = 1/255
    W1,W2   int8, per-tensor symmetric, sw = max|W|/127
    acc1    exact sum of W1_int * x  (the accelerator computes this)
    h       clamp((acc1 + b1 + 2^(s1-1)) >> s1, 0, 255), scale sh = sx*sw1*2^s1
    logit   exact sum of W2_int * h + b2

The accumulator is 32 bits and wraps, so the exporter refuses to write a model
whose worst case could reach 2^31.
"""

import argparse
import json
import math
import random
from datetime import date
from pathlib import Path

import numpy as np

from tools.digit_data import INPUTS, POOLED, fetch_training_set, load_test_set, prepare
from tools.digit_drawn import HEIGHTS, draw, verified_test_set

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / 'programs/rv32/digit_model.json'
PREDICTIONS = ROOT / 'programs/rv32/digit_model.predictions'

HIDDEN = 32
CLASSES = 10
HMAX = 255              # the largest hidden value; any value <= 32767 is a legal MAC operand
WMAX = 127              # int8 weights are symmetric, so -128 is never emitted
INT8_BOUND = 128        # but the wrap proof assumes the full int8 magnitude
SEED = 20260922
EPOCHS = 30
BATCH = 64
RATE = 0.05
HOLDOUT = 5000
COPIES = 1              # keyboard-style drawings per training image
DRAWN_HEIGHTS = (8, 28)  # inclusive range of their heights


def prepare_many(images):
    """`tools.digit_data.prepare` over a list, as a uint8 matrix. The stdlib contract
    itself, not a vectorized copy, so there is no second definition to drift."""
    return np.frombuffer(b''.join(prepare(image) for image in images), dtype=np.uint8).reshape(len(images), INPUTS)


def drawings(images, rng):
    """COPIES keyboard-style drawings of each image at random heights, in image order."""
    return [draw(image, rng.randint(*DRAWN_HEIGHTS), rng) for image in images for _ in range(COPIES)]


def train(x, y, rng):
    """Plain SGD on a one-hidden-layer ReLU network with softmax cross-entropy."""
    w1 = rng.normal(0, math.sqrt(2.0 / INPUTS), (INPUTS, HIDDEN))
    b1 = np.zeros(HIDDEN)
    w2 = rng.normal(0, math.sqrt(2.0 / HIDDEN), (HIDDEN, CLASSES))
    b2 = np.zeros(CLASSES)
    onehot = np.eye(CLASSES)[y]
    for epoch in range(EPOCHS):
        order = rng.permutation(len(x))
        for start in range(0, len(x) - BATCH + 1, BATCH):
            pick = order[start:start + BATCH]
            xb, tb = x[pick], onehot[pick]
            hidden = np.maximum(0.0, xb @ w1 + b1)
            scores = hidden @ w2 + b2
            scores -= scores.max(axis=1, keepdims=True)
            probability = np.exp(scores)
            probability /= probability.sum(axis=1, keepdims=True)
            dscores = (probability - tb) / len(pick)
            dhidden = (dscores @ w2.T) * (hidden > 0)
            w2 -= RATE * (hidden.T @ dscores)
            b2 -= RATE * dscores.sum(axis=0)
            w1 -= RATE * (xb.T @ dhidden)
            b1 -= RATE * dhidden.sum(axis=0)
        accuracy = float((np.argmax(np.maximum(0.0, x @ w1 + b1) @ w2 + b2, axis=1) == y).mean())
        print(f'  epoch {epoch + 1:2d}/{EPOCHS}  training accuracy {accuracy:.4f}')
    return w1, b1, w2, b2


def quantize(w1, b1, w2, b2, x_holdout):
    """Derive the integer model and the hidden shift; return ints plus the scale metadata."""
    sx = 1.0 / 255.0
    sw1 = float(np.abs(w1).max()) / WMAX
    w1_int = np.clip(np.rint(w1 / sw1), -WMAX, WMAX).astype(np.int64)
    b1_int = np.rint(b1 / (sx * sw1)).astype(np.int64)
    hidden_float = np.maximum(0.0, x_holdout @ w1 + b1)
    h_max = float(hidden_float.max())
    s1 = max(0, math.ceil(math.log2(h_max / (sx * sw1 * HMAX)))) if h_max > 0 else 0
    sw2 = float(np.abs(w2).max()) / WMAX
    w2_int = np.clip(np.rint(w2 / sw2), -WMAX, WMAX).astype(np.int64)
    sh = sx * sw1 * (2 ** s1)
    b2_int = np.rint(b2 / (sh * sw2)).astype(np.int64)
    scales = {'sx': sx, 'sw1': sw1, 'sw2': sw2, 'sh': sh, 'hidden_max_float': h_max}
    return w1_int, b1_int, s1, w2_int, b2_int, scales


def prove_no_wrap(b1_int, s1, b2_int):
    """Refuse to export a model whose worst-case accumulator could reach 2^31."""
    limit = 2 ** 31
    roundterm = (1 << (s1 - 1)) if s1 else 0
    first = INPUTS * 255 * INT8_BOUND + int(np.abs(b1_int).max()) + roundterm
    second = HIDDEN * HMAX * INT8_BOUND + int(np.abs(b2_int).max())
    if first >= limit or second >= limit:
        raise ValueError(f'accumulator could wrap: layer bounds {first} and {second}, limit {limit}')
    return first, second


def infer_int(x, w1_int, b1_int, s1, w2_int, b2_int):
    """The integer model, vectorized. `tools.digit_ref` is the readable definition."""
    roundterm = (1 << (s1 - 1)) if s1 else 0
    acc1 = x.astype(np.int64) @ w1_int + b1_int + roundterm
    hidden = np.clip(acc1 >> s1, 0, HMAX)
    hidden[acc1 < 0] = 0
    return hidden @ w2_int + b2_int


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--dry-run', action='store_true', help='train and report without writing the model')
    parser.add_argument('--mirror', type=int, default=0, choices=(0, 1), help='which training-set mirror to use')
    arguments = parser.parse_args()

    print('fetching the training set (the only network access in this repository)')
    train_images, train_labels = fetch_training_set(mirror=arguments.mirror)
    test_images, test_labels = load_test_set()
    print(f'preprocessing {len(train_images)} training and {len(test_images)} test images')
    draw_rng = random.Random(SEED)
    fit_images, holdout_images = train_images[:-HOLDOUT], train_images[-HOLDOUT:]
    fit_labels = train_labels[:-HOLDOUT]
    fit_drawn = drawings(fit_images, draw_rng)
    holdout_drawn = drawings(holdout_images, draw_rng)
    x_fit = prepare_many(fit_images + fit_drawn) / 255.0
    y_fit = np.array(fit_labels + [label for label in fit_labels for _ in range(COPIES)])
    x_holdout = prepare_many(holdout_images + holdout_drawn) / 255.0
    x_test, y_test = prepare_many(test_images), np.array(test_labels)
    keyboard = verified_test_set()
    x_keyboard = {height: prepare_many(keyboard[height]) for height in HEIGHTS}

    print(f'training on {len(x_fit)} inputs ({len(fit_images)} images and {COPIES} drawing each), '
          f'holding out {len(x_holdout)} to calibrate the shift')
    rng = np.random.default_rng(SEED)
    w1, b1, w2, b2 = train(x_fit, y_fit, rng)

    w1_int, b1_int, s1, w2_int, b2_int, scales = quantize(w1, b1, w2, b2, x_holdout)
    bounds = prove_no_wrap(b1_int, s1, b2_int)
    print(f'shift s1 = {s1}; worst-case accumulators {bounds[0]} and {bounds[1]} (limit {2**31})')

    float_test = float((np.argmax(np.maximum(0.0, (x_test / 255.0) @ w1 + b1) @ w2 + b2, axis=1) == y_test).mean())
    logits = infer_int(x_test, w1_int, b1_int, s1, w2_int, b2_int)
    predictions = np.argmax(logits, axis=1)
    int_test = float((predictions == y_test).mean())
    print(f'test accuracy: float {float_test:.4f}, integer {int_test:.4f}, gap {float_test - int_test:+.4f}')
    keyboard_accuracy = {}
    for height in HEIGHTS:
        guesses = np.argmax(infer_int(x_keyboard[height], w1_int, b1_int, s1, w2_int, b2_int), axis=1)
        keyboard_accuracy[str(height)] = round(float((guesses == y_test).mean()), 6)
    print('keyboard-style test set, integer: ' +
          ', '.join(f'{height} px {value:.4f}' for height, value in keyboard_accuracy.items()))

    if arguments.dry_run:
        print('dry run: nothing written')
        return
    model = {
        'comment': 'Generated by tools/digit_train.py; see docs/rv32-digit.md. Do not edit by hand.',
        'architecture': {'inputs': INPUTS, 'hidden': HIDDEN, 'classes': CLASSES, 'pooled_side': POOLED},
        'shift': s1, 'hidden_max': HMAX,
        'w1': w1_int.T.tolist(), 'b1': b1_int.tolist(),
        'w2': w2_int.T.tolist(), 'b2': b2_int.tolist(),
        'metadata': {
            'seed': SEED, 'epochs': EPOCHS, 'batch': BATCH, 'rate': RATE, 'holdout': HOLDOUT,
            'drawn_copies': COPIES, 'drawn_heights': list(DRAWN_HEIGHTS),
            'keyboard_int_accuracy': keyboard_accuracy,
            'trained': date.today().isoformat(), 'numpy': np.__version__,
            'float_test_accuracy': round(float_test, 6), 'int_test_accuracy': round(int_test, 6),
            'quantization_gap': round(float_test - int_test, 6),
            'accumulator_bounds': list(bounds), 'scales': scales,
        },
    }
    MODEL.write_text(json.dumps(model, indent=1, sort_keys=True) + '\n')
    PREDICTIONS.write_text(''.join(f'{int(p)}\n' for p in predictions))
    print(f'wrote {MODEL.relative_to(ROOT)} and {PREDICTIONS.relative_to(ROOT)}')


if __name__ == '__main__':
    main()
