# Digit recognition fix: size normalization

Planned 2026-09-28. Not implemented; S1 changes no digit behaviour.

The live digit screen misreads most digits drawn at sizes other than about 20
pixels, because preprocessing never resizes a drawing to the size the model was
trained on. Adding that one step, then retraining on keyboard-style strokes, is
the fix.

## Symptom

Hand-testing the boot menu's digit screen on 2026-09-28 gave poor predictions.
The published figure for the same model is 96.16% on the MNIST test set (N1,
PR #12). The hardware and arithmetic are not at fault: the CPU path, the SIMD4
path and the Python reference agree bit for bit on the same 196 inputs. The live
input differs from the test images, not the machine.

## Root cause and measurements

`digit_prepare` (`programs/rv32/digit_model.c`) centres the ink by its bounding
box and pools 2x2; it never scales. Every MNIST digit was fit to a 20x20 box
before release, so the model has only seen digits about 20 pixels tall. Arrow-key
drawings tend to be small or to fill the canvas, the two worst cases below.

Accuracy on the first 2,000 vendored MNIST test digits, redrawn with the UI's 2x2
brush, committed model, no retrain:

| Drawn height (px of 28) | Today | With a resize to 20 px |
| --- | --- | --- |
| 10 | 19.4% | 79.3% |
| 14 | 46.9% | 85.5% |
| 20 | 90.4% | 90.2% |
| 24 | 84.5% | 89.9% |
| 28 | 58.1% | 89.2% |

Method: binarize each test digit at 128, thin it to a one-pixel skeleton
(Zhang-Suen), rescale it to the given height, and repaint it with the UI's 2x2
brush. The resized column adds a nearest-neighbour resize of the bounding box to
20 pixels on its longer side. `python3 -m tools.digit_drawn_accuracy --count 2000`
reproduces the table (it needs numpy, like `tools/digit_train.py`). Clean MNIST digits drop from 94.95% to 91.70% under
that prototype resize, which retraining should recover. At 8 pixels the prototype
reaches only 57%: enlarging a small drawing also thickens its strokes.

## Fix design

`digit_prepare` gains one step between finding the bounding box and centring:
resize the box so its longer side is 20 pixels, keeping the aspect ratio. It stays
the only preprocessing definition. The pipeline becomes bounding box, resize,
centre, then 2x2 pool.

The resize is integer nearest-neighbour, specified exactly so that every
implementation produces the same 784 bytes:

- `s` = the longer side of the ink box, 1 to 28. The shorter side `t` becomes
  `n = (t * 20 + s / 2) / s`, at least 1.
- Destination pixel `d` (0 to 19 along the long side, 0 to n-1 along the short
  one) reads source offset `(d * s * 3277) >> 16`. That equals `floor(d * s / 20)`
  for every `d * s` up to 532, which the tests prove exhaustively.
- Values are copied, not averaged, so the canvas stays 0 or 255 and the pool is
  unchanged.

The guest is RV32I with no divide instruction. The single division for `n` uses a
28-entry reciprocal table (or a subtraction loop of at most 20 steps) instead of
`rv32_div`. A blank canvas still produces 196 zeros.

| Place | File |
| --- | --- |
| Guest C, both inference paths | `programs/rv32/digit_model.c` |
| Python reference and training | `tools/digit_data.py` (`prepare`) |
| Native harness | `tools/rv32_digit_native.py`, `tests/rv32_capstone_native.c` |
| Contract text | `docs/rv32-digit.md`, "Preprocessing is one contract" |

## Retraining

Retrain once with `tools/digit_train.py` on the new `digit_prepare`, adding
keyboard-style copies of each training digit: binarized, thinned and repainted with
the 2x2 brush at random heights from 8 to 28 pixels and random positions. Keep the
original digits too, so the clean MNIST figure stays at or above 95%.

Lock two acceptance targets before measuring, as N1 did:

1. Clean MNIST test set, integer model: at least 95%.
2. A keyboard-style test set built deterministically from the same 10,000 test
   digits at heights 10, 14, 20, 24 and 28: at least 85% at every height.

The second set is generated from a fixed seed and pinned by SHA-256, and a test
asserts both targets. Architecture, quantization, the K=49 launch layout and the
no-wrap proof are unchanged; only weights, biases and possibly the requantization
shift move.

## Verification and re-pinning

A retrain changes every value derived from the weights. Regenerate each, never
hand-edit:

- [ ] Exhaustive test that the resize's shift-multiply equals floor division for every `d` and `s`
- [ ] Byte-level tests: C, Python and the native harness produce identical 196 bytes, including tall, wide, one-pixel and full-canvas boxes
- [ ] `digit_model.json` and `digit_model.predictions` regenerated; `make accuracy-rv32-digit` meets both targets
- [ ] `make waves-rv32-digit` sink literal (`bench 3791` today) re-pinned
- [ ] Menu hashes re-pinned with `tools/rv32_capstone_native.py --write`, never from an emulator run, including any S1 session checkpoint that includes a classification
- [ ] Cost re-measured: instructions and RTL cycles per inference on both paths, since the resize adds CPU work
- [ ] Full `make test-rv32` exits 0, and Codex reviews the PR
- [ ] Hand test on the emulator window: digits drawn small, medium and full-canvas

## Risks and open questions

- **Small drawings stay weaker.** If retraining leaves 8 to 10 pixel drawings under
  target, record stroke points instead of pixels and repaint them after scaling, so
  stroke width stays constant.
- **Sequencing with S1.** S1's end-to-end scenario pins a digit checkpoint. This fix
  runs as its own branch and PR after S1 merges, then re-pins that checkpoint once.
- **Resize method.** Nearest-neighbour keeps the canvas binary and the contract
  simple. Bilinear would match MNIST's anti-aliased strokes more closely but adds
  grey levels and more integer rounding to specify.

Until the fix lands, draw digits about 18 to 22 cells tall for the best results.
