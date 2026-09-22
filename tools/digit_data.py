#!/usr/bin/env python3
"""The MNIST test set and the one preprocessing contract, in the standard library.

Two things live here so that every other part of N1 agrees by construction:

* `verify_dataset` checks the vendored files against `SHA256SUMS.json` exactly as
  `tools.fp32_vectors.verify_reference_sources` checks SoftFloat, including the
  inventory, so a new or deleted file fails instead of being ignored.
* `prepare` is the *only* definition of how 784 canvas bytes become the 196 model
  inputs: centre the ink by its bounding box, then average each 2x2 block. The
  guest C in `programs/rv32/digit_model.c` reimplements exactly this, and the
  tests compare the 196 bytes directly rather than only the logits, because
  preprocessing that both inference paths skip would cancel out of a logit
  comparison and hide itself.

Why bounding-box centring, when the distributed images are already centred by
centre of mass: a digit drawn with the arrow keys is not centred at all. Training,
calibration, evaluation and both runtime paths therefore all run through this same
function, so the model always sees one distribution. The source page asks that this
substitution be reported; `third_party/mnist/README.md` and `docs/rv32-digit.md`
report it.
"""

import gzip
import hashlib
import json
import struct
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATASET = ROOT / 'third_party/mnist'
BUILD = ROOT / 'build/mnist'

SIDE = 28                      # canvas is SIDE x SIDE bytes
POOLED = SIDE // 2             # model input is POOLED x POOLED bytes
INPUTS = POOLED * POOLED       # 196
PIXELS = SIDE * SIDE           # 784
IMAGE_MAGIC, LABEL_MAGIC = 0x803, 0x801

# Digests of the two files fetched but deliberately not vendored; the training
# script checks its download against these. The vendored pair is in SHA256SUMS.json.
TRAINING_DIGESTS = {
    'train-images-idx3-ubyte.gz': '440fcabf73cc546fa21475e81ea370265605f56be210a4024d2ca8f203523609',
    'train-labels-idx1-ubyte.gz': '3552534a0a558bbed6aed32b30c495cca23d567ec52cac8be1a0730e8010255c',
}
MIRRORS = ('https://ossci-datasets.s3.amazonaws.com/mnist/',
           'https://storage.googleapis.com/cvdf-datasets/mnist/')


def verify_dataset(directory=DATASET):
    """Check every vendored file against the manifest, and the manifest against the directory."""
    manifest = json.loads((Path(directory) / 'SHA256SUMS.json').read_text())
    actual = {str(path.relative_to(directory)) for path in Path(directory).rglob('*')
              if path.is_file() and str(path.relative_to(directory)) not in ('README.md', 'SHA256SUMS.json')}
    if not isinstance(manifest, dict) or not manifest or set(manifest) != actual:
        raise ValueError('MNIST manifest must list every dataset file')
    for name, digest in manifest.items():
        if hashlib.sha256((Path(directory) / name).read_bytes()).hexdigest() != digest:
            raise ValueError(f'MNIST fingerprint mismatch: {name}')


def read_images(path):
    """Parse a gzip-compressed IDX image file into a list of 784-byte images."""
    with gzip.open(path, 'rb') as stream:
        blob = stream.read()
    magic, count, rows, columns = struct.unpack('>IIII', blob[:16])
    if magic != IMAGE_MAGIC or rows != SIDE or columns != SIDE:
        raise ValueError(f'{path}: expected {IMAGE_MAGIC:#x} and {SIDE}x{SIDE}, got {magic:#x} and {rows}x{columns}')
    if len(blob) != 16 + count * PIXELS:
        raise ValueError(f'{path}: header promises {count} images, file holds {len(blob) - 16} pixel bytes')
    return [blob[16 + i * PIXELS:16 + (i + 1) * PIXELS] for i in range(count)]


def read_labels(path):
    """Parse a gzip-compressed IDX label file into a list of ints 0..9."""
    with gzip.open(path, 'rb') as stream:
        blob = stream.read()
    magic, count = struct.unpack('>II', blob[:8])
    if magic != LABEL_MAGIC:
        raise ValueError(f'{path}: expected magic {LABEL_MAGIC:#x}, got {magic:#x}')
    if len(blob) != 8 + count:
        raise ValueError(f'{path}: header promises {count} labels, file holds {len(blob) - 8}')
    labels = list(blob[8:])
    if any(label > 9 for label in labels):
        raise ValueError(f'{path}: label outside 0..9')
    return labels


def load_test_set(directory=DATASET, verify=True):
    """The 10,000 vendored test images and their labels, after checking fingerprints."""
    if verify:
        verify_dataset(directory)
    images = read_images(Path(directory) / 't10k-images-idx3-ubyte.gz')
    labels = read_labels(Path(directory) / 't10k-labels-idx1-ubyte.gz')
    if len(images) != len(labels):
        raise ValueError('test set image and label counts differ')
    return images, labels


def centre(canvas):
    """Shift the ink so its bounding box is centred in the 28x28 field.

    A blank canvas is returned unchanged (there is no bounding box to centre).
    Odd leftovers floor, so a 3-wide box in 28 columns starts at column 12.
    """
    rows = [r for r in range(SIDE) if any(canvas[r * SIDE + c] for c in range(SIDE))]
    if not rows:
        return bytes(PIXELS)
    columns = [c for c in range(SIDE) if any(canvas[r * SIDE + c] for r in range(SIDE))]
    top, bottom, left, right = rows[0], rows[-1], columns[0], columns[-1]
    down = (SIDE - (bottom - top + 1)) // 2 - top
    across = (SIDE - (right - left + 1)) // 2 - left
    out = bytearray(PIXELS)
    for r in range(top, bottom + 1):
        for c in range(left, right + 1):
            out[(r + down) * SIDE + (c + across)] = canvas[r * SIDE + c]
    return bytes(out)


def pool(canvas):
    """Average each 2x2 block with a truncating shift: 14x14 bytes, each 0..255."""
    out = bytearray(INPUTS)
    for i in range(POOLED):
        for j in range(POOLED):
            r, c = 2 * i, 2 * j
            out[i * POOLED + j] = (canvas[r * SIDE + c] + canvas[r * SIDE + c + 1] +
                                   canvas[(r + 1) * SIDE + c] + canvas[(r + 1) * SIDE + c + 1]) >> 2
    return bytes(out)


def prepare(canvas):
    """The preprocessing contract: centre, then pool. 784 bytes in, 196 bytes out."""
    if len(canvas) != PIXELS:
        raise ValueError(f'canvas must be {PIXELS} bytes, got {len(canvas)}')
    return pool(centre(canvas))


def fetch_training_set(directory=BUILD, mirror=0):
    """Download the training pair into the ignored build directory and check its digests.

    Only `tools/digit_train.py` calls this. Nothing reachable from a make target
    touches the network; the vendored test set is what the tests and the firmware use.
    """
    from urllib.request import urlopen
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    paths = {}
    for name, digest in TRAINING_DIGESTS.items():
        path = directory / name
        if not path.exists() or hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            with urlopen(MIRRORS[mirror] + name, timeout=120) as response:
                path.write_bytes(response.read())
        found = hashlib.sha256(path.read_bytes()).hexdigest()
        if found != digest:
            raise ValueError(f'{name}: expected sha256 {digest}, downloaded {found}')
        paths[name] = path
    return (read_images(paths['train-images-idx3-ubyte.gz']),
            read_labels(paths['train-labels-idx1-ubyte.gz']))


if __name__ == '__main__':
    verify_dataset()
    images, labels = load_test_set()
    print(f'{len(images)} test images verified; first label {labels[0]}')
