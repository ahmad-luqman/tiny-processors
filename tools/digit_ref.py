#!/usr/bin/env python3
"""The integer digit model in the standard library: the oracle every other path is checked against.

Nothing here imports numpy or touches the network, so this reference runs wherever
`make` runs. It reads the committed `programs/rv32/digit_model.json` and reproduces
exactly the arithmetic the guest performs, in the order the guest performs it:

    acc1[n] = sum_k W1[n][k] * x[k]                     exact, the accelerator's job
    h[n]    = clamp((acc1[n] + b1[n] + round) >> s1, 0, HMAX)   ReLU, round, saturate
    logit[c]= sum_n W2[c][n] * h[n] + b2[c]             exact
    class   = argmax, lowest index on a tie

The shifts are written so a negative pre-activation never reaches `>>`: the sign is
tested first and ReLU returns zero, which is also what the guest C does to avoid
implementation-defined signed shifts.

    python3 -m tools.digit_ref --count 10000     # accuracy on the vendored test set
    python3 -m tools.digit_ref --image 0         # one image's logits and prediction
"""

import argparse
import json
from pathlib import Path

from tools.digit_data import INPUTS, load_test_set, prepare

ROOT = Path(__file__).resolve().parents[1]
MODEL = ROOT / 'programs/rv32/digit_model.json'
PREDICTIONS = ROOT / 'programs/rv32/digit_model.predictions'
_CACHE = {}


def load_model(path=MODEL):
    """Read and validate the committed integer model."""
    key = str(path)
    if key in _CACHE:
        return _CACHE[key]
    model = json.loads(Path(path).read_text())
    shape = model['architecture']
    w1, b1, w2, b2 = model['w1'], model['b1'], model['w2'], model['b2']
    if shape['inputs'] != INPUTS:
        raise ValueError(f"model expects {shape['inputs']} inputs, preprocessing produces {INPUTS}")
    if len(w1) != shape['hidden'] or any(len(row) != shape['inputs'] for row in w1) or len(b1) != shape['hidden']:
        raise ValueError('layer 1 shape disagrees with the architecture block')
    if len(w2) != shape['classes'] or any(len(row) != shape['hidden'] for row in w2) or len(b2) != shape['classes']:
        raise ValueError('layer 2 shape disagrees with the architecture block')
    if any(not -127 <= value <= 127 for row in w1 + w2 for value in row):
        raise ValueError('weights must be int8 and symmetric: -127..127')
    if not 0 <= model['shift'] <= 31:
        raise ValueError('shift must be 0..31')
    if not 0 < model['hidden_max'] <= 32767:
        raise ValueError('hidden_max must be a legal 16-bit MAC operand')
    _CACHE[key] = model
    return model


def accumulate(weights, values, bias):
    """One exact dot product plus bias, the quantity the accelerator reassembles from two halves."""
    return sum(w * v for w, v in zip(weights, values)) + bias


def hidden_layer(x, model):
    """Layer 1 with the requantization the CPU performs after reading the accumulator back."""
    shift, hmax = model['shift'], model['hidden_max']
    roundterm = (1 << (shift - 1)) if shift else 0
    out = []
    for weights, bias in zip(model['w1'], model['b1']):
        total = accumulate(weights, x, bias) + roundterm
        out.append(0 if total < 0 else min(total >> shift, hmax))
    return out


def infer(x, model=None):
    """The ten logits for one preprocessed 196-byte input."""
    model = model or load_model()
    if len(x) != INPUTS:
        raise ValueError(f'expected {INPUTS} inputs, got {len(x)}')
    hidden = hidden_layer(x, model)
    return [accumulate(weights, hidden, bias) for weights, bias in zip(model['w2'], model['b2'])]


def argmax(logits):
    """The predicted class and the non-negative margin over the runner-up."""
    best = 0
    for index in range(1, len(logits)):
        if logits[index] > logits[best]:
            best = index
    second = max(value for index, value in enumerate(logits) if index != best)
    return best, logits[best] - second


def predict(x, model=None):
    return argmax(infer(x, model))[0]


def classify_canvas(canvas, model=None):
    """Preprocess a 784-byte canvas and classify it, exactly as the guest does."""
    return argmax(infer(prepare(canvas), model))


def accuracy(count=None, model=None):
    """Measure the integer model on the vendored test set; returns (correct, total, predictions)."""
    model = model or load_model()
    images, labels = load_test_set()
    if count:
        images, labels = images[:count], labels[:count]
    predictions = [predict(prepare(image), model) for image in images]
    correct = sum(p == label for p, label in zip(predictions, labels))
    return correct, len(labels), predictions


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument('--count', type=int, default=None, help='how many test images to measure')
    parser.add_argument('--image', type=int, default=None, help='report one image instead of accuracy')
    parser.add_argument('--write-predictions', action='store_true', help='regenerate the pinned prediction file')
    arguments = parser.parse_args()
    model = load_model()
    if arguments.image is not None:
        images, labels = load_test_set()
        x = prepare(images[arguments.image])
        logits = infer(x, model)
        best, margin = argmax(logits)
        print(f'image {arguments.image}: label {labels[arguments.image]} predicted {best} margin {margin}')
        print('logits: ' + ' '.join(str(value) for value in logits))
        return
    correct, total, predictions = accuracy(arguments.count, model)
    print(f'{correct}/{total} = {correct / total:.4f} on the vendored MNIST test set')
    if arguments.write_predictions:
        PREDICTIONS.write_text(''.join(f'{p}\n' for p in predictions))
        print(f'wrote {PREDICTIONS.relative_to(ROOT)}')
    else:
        pinned = PREDICTIONS.read_text().split()
        if len(pinned) == len(predictions) and [int(v) for v in pinned] != predictions:
            raise SystemExit('predictions differ from the pinned file')


if __name__ == '__main__':
    main()
