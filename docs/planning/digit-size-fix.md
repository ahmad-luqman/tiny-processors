# Digit recognition fix: size normalization

Planned 2026-09-28; implemented 2026-09-29 (see [Outcome](#outcome)).

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

Method: binarize each test digit at 128, rescale it to the given height, thin it
to a one-pixel skeleton (Zhang-Suen), and repaint it with the UI's 2x2 brush, so
the stroke is one brush wide at every size, as the keyboard draws it. The resized column adds a nearest-neighbour resize of the bounding box to
20 pixels on its longer side. `python3 -m tools.digit_drawn_accuracy --count 2000`
at 660d22e reproduces the table (that version needed numpy; the script has since
become a standard-library CLI over `tools/digit_drawn.py`). Clean MNIST digits drop from 94.95% to 91.70% under
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

*As implemented, the sampling rule changed* (see [Outcome](#outcome)): each axis
reads the source pixel under each destination pixel's centre,
`floor((2d + 1) * e / (2m))` for an axis of `e` source pixels becoming `m`, because
`floor(d * s / 20)` never reads the last row or column, where the brush clips.

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

- [x] Exhaustive test that the resize's sampling equals floor division and stays inside the ink box for every box shape
- [x] Byte-level tests: C, Python and the native harness produce identical 196 bytes, including tall, wide, one-pixel and full-canvas boxes
- [x] `digit_model.json` and `digit_model.predictions` regenerated; `make accuracy-rv32-digit` meets both targets
- [x] `make waves-rv32-digit` sink literal (`bench 00000ecf` by then, now `bench 00001b54`) re-pinned
- [x] Menu hashes re-pinned with `tools/rv32_capstone_native.py --write`, never from an emulator run, including any S1 session checkpoint that includes a classification
- [x] Cost re-measured: instructions and RTL cycles per inference on both paths, since the resize adds CPU work
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

Until the fix landed, the advice was to draw digits about 18 to 22 cells tall.

## Outcome

Implemented 2026-09-29 on branch `digit-size-fix`, with one change to the design
and two findings the plan did not anticipate.

**Centre sampling instead of left-edge sampling.** Review found that
`floor(d * s / 20)` never reads source offset `s - 1` when it shrinks. The cursor
stops at row and column 27, where the 2x2 brush clips to one cell, so the base of
a full-canvas digit drawn along the bottom edge vanished before pooling, and a
drawing with ink only on the far edges prepared to 196 zeros. Each axis now reads
the source pixel under each destination pixel's centre,
`floor((2d + 1) * e / (2m))`, using that axis's own extent `e` and size `m`. That
reads both ends of the long side for every size up to 28, never skips a two-cell
stroke, and keeps every sample inside the ink box by construction, which also
removed the reciprocal constant: the guest carries the quotient from one `d` to
the next by subtraction. The short side keeps `n = (t * 20 + s / 2) / s`. The
first model, trained with left-edge sampling, was discarded and the model
retrained once on the final rule.

**Clean MNIST did not drop.** The prototype binarized each digit before resizing,
which is what cost clean digits 3 points. The contract copies values, and 9,997
of the 10,000 test digits already have a 20-pixel box, so for them the resize is
the identity: the N1 model still scored 96.16% under the new contract before any
retraining.

**A stale build input.** `ensure_headers`, the fallback that lets a test run
without make, regenerated the four digit headers but not `digit_weights.c`, where
the N1 review had moved the tables. After a retrain the native tests compiled the
old weights beside current headers and failed. It now regenerates that file too,
and a test checks every file the Makefile lists as generated.

The keyboard-style drawings are generated in the standard library
(`tools/digit_drawn.py`: binarize, integer nearest-neighbour scale, Zhang-Suen
thinning with lookup tables, 2x2 brush), so the acceptance test needs no numpy.
Its thinning matched the numpy prototype's on 6,000 cases. Each drawing is seeded
by its height and index, so a partial run measures a true prefix of the pinned
set. The 50,000-canvas set (10,000 digits at five heights, seed 20260929) is
pinned by SHA-256 `a499728a…57f817` and generates in about 16 seconds.

Accuracy of the integer model, targets locked before measuring:

| Set | Before the fix | Retrained on the final contract | Target |
| --- | --- | --- | --- |
| Clean MNIST, 10,000 | 96.16% | **96.53%** | ≥ 95% |
| Keyboard-style, 10 px | 19.4% | **87.75%** | ≥ 85% |
| Keyboard-style, 14 px | 46.9% | **91.45%** | ≥ 85% |
| Keyboard-style, 20 px | 90.4% | **94.44%** | ≥ 85% |
| Keyboard-style, 24 px | 84.5% | **93.73%** | ≥ 85% |
| Keyboard-style, 28 px | 58.1% | **93.75%** | ≥ 85% |

The "before" column for drawings is the 2,000-digit table above; the retrained
column uses the full pinned set. Heights are nominal, before thinning trims the
stroke ends. The retrained model is the only configuration tried (the original
digits plus one drawing each at a random height from 8 to 28, 30 epochs), so the
test set tuned nothing. No drawing was blank after thinning. It keeps the
architecture, the K=49 layout and the no-wrap proof; the shift moved from 11 to
10 and the accumulator bounds to 6,417,622 and 1,045,517.

Cost of the resize per inference: about 11,800 emulator instructions and 50,000
RTL cycles on each path (CPU 145,552 instructions and 596,593 cycles, accelerated
104,040 and 335,395; N1 measured 133,607 / 546,288 and 92,302 / 285,906). The
waves still show the first launch at 1,008 cycles and 400 transfers. `digit_prepare`
uses 1,600 bytes of the 16 KiB stack and calls no software multiply or divide.

Re-pinned from the native build: `RV32_DIGIT_MENU_HEX` `b20bf8ad` and
`RV32_SOC_MENU_HEX` `c76cd363` with their checkpoint files; both sessions still
read what they drew (1 then 7, and 1). The waves sink is `bench 00001b54`
(class 7, margin 6,988), and a test now derives it from the oracle.
`RV32_CAPSTONE_HEX` and the G1 and G2 menu hashes are unchanged because those
sessions never classify.
