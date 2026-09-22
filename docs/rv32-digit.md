# N1: quantized digit inference

The guest classifies a handwritten digit. A small integer neural network runs on
the RV32 CPU, with the SIMD4 accelerator performing the multiply-accumulate work,
and the boot menu gains a screen where a digit drawn with the keyboard is read
back. The host presents pixels and forwards keys; every arithmetic decision
belongs to the guest.

Non-goals: no training on the machine, no convolution, no floating point in the
model, no interrupts or DMA, and no change to the RTL. The engine stays exactly
what A1 and A2 built.

## Model and data

The classifier is a 196 -> 32 -> 10 multilayer perceptron with ReLU. Its input is
a 14x14 image obtained by centring a 28x28 canvas and averaging each 2x2 block.
`tools/digit_train.py` trains it once with numpy and writes the integer weights to
`programs/rv32/digit_model.json`. That script is the only file in the repository
that imports a third-party package or reaches the network, and no make target runs
it: the build, the tests and the firmware read the committed model.

The MNIST test set is vendored under `third_party/mnist` with a SHA-256 manifest
checked the way the SoftFloat manifest is, inventory included. Its README records
what was verified rather than what is usually assumed: the original distribution
page no longer serves the data, the files came from two independent mirrors and
are byte-identical, each matches the size the archived original documents, and the
original page carries **no license, copyright or terms statement at all**. The
"CC BY-SA 3.0" label some catalogues apply could not be traced to the authors and
is therefore not repeated as fact. The training set is fetched by the training
script into an ignored directory and is not committed.

| Measurement | Value |
| --- | --- |
| Integer accuracy, 10,000 vendored test images | 96.16% |
| Float accuracy, same images | 96.10% |
| Effect of quantization | the integer model scores 0.06 points **higher** than the float one, so quantization is not what limits this model; at this size the difference is noise, not an improvement to claim |
| Hidden shift `s1` | 11, calibrated on a 5,000-image holdout of the training set |

The committed test set tunes nothing: the shift and every other calibration use a
training holdout, and the test set is measured once at the end.
`make accuracy-rv32-digit` reproduces the number in about three seconds with the
standard library alone.

## Preprocessing is one contract

`digit_prepare` centres the ink by its bounding box and then pools, and it is the
only definition of how a canvas becomes model inputs. Training, calibration,
accuracy measurement, the diagnostic's inputs and both runtime paths all use it.

Centring matters because the distributed images were centred by their centre of
mass in a 20x20 box, while a digit drawn with the arrow keys sits wherever the
cursor was. Substituting bounding-box centring is the preprocessing change the
source page asks to have reported, and this is the report. A blank canvas is
defined to produce 196 zeros, and odd leftovers floor, so a 3-wide box in 28
columns starts at column 12.

The tests compare the 196 output bytes directly rather than only the logits they
lead to. Preprocessing that every inference path skipped alike would cancel out of
a logit comparison and hide itself.

## Numeric contract

| Stage | Definition |
| --- | --- |
| Input | `x` is 0..255 in a 16-bit slot, positive under the engine's sign extension |
| Weights | int8, per-tensor symmetric, −127..127; the exporter never emits −128 |
| Layer 1 | `acc1[n] = sum over k of W1[n][k] * x[k]`, exact, computed by the accelerator |
| Hidden | `t = acc1 + b1 + 2^(s1−1)`; `h = 0` if `t` is negative, else `min(t >> s1, 255)` |
| Layer 2 | `logit[c] = sum over n of W2[c][n] * h[n] + b2[c]`, exact |
| Output | argmax with the lowest index on a tie; the margin over the runner-up |

Three decisions are worth their own sentences.

**Saturation and rounding are the CPU's job.** A1 fixed 16x16 to 32-bit products
into a wrapping accumulator and deferred saturation and rounding to N1. N1 answers
them without touching the hardware: the engine stays exact and wrapping, the full
32-bit accumulator is read back through `RDA 0` and `RDA 16`, and the CPU adds the
bias, rounds half up, applies ReLU and clamps. A saturating read-back and a
rounded shift remain the exercises `docs/simd4-to-gates.md` describes.

**The arithmetic is unsigned.** The accumulator wraps modulo 2^32 exactly as the
engine's does, ReLU is a sign-bit test so a negative value is never shifted, and
the one signed result is converted explicitly. None of it depends on
implementation-defined signed shifts or conversions.

**The margin is unsigned too.** It is non-negative by construction, but the
difference of two valid int32 logits can overflow a signed subtraction, so it is
computed and stored as `uint32_t`. The tests include the int32 extremes where a
signed subtraction would be undefined.

**No accumulator can wrap.** The exporter proves it and refuses to export
otherwise, and the tests recompute the bound from the committed weights:

| Layer | Worst case | Limit |
| --- | --- | --- |
| 1 | 6,415,798 | 2,147,483,648 |
| 2 | 1,045,102 | 2,147,483,648 |

The hidden cap is 255 rather than 127. Any value up to 32,767 is a legal operand,
and 255 doubles the hidden precision for nothing. Real digits peak near half that
cap, so the clamp is a guard rather than a routine event; a test constructs the
input that does reach it, by driving every positively weighted input of one neuron
to 255, so the clamp is not dead code.

## One input, traced

Test image 0, whose label is 7. After centring and pooling, 39 of its 196 inputs
are non-zero and the largest is 254. Hidden neuron 0:

| Step | Value |
| --- | --- |
| `acc1[0]`, the accelerator's answer | −11,187 |
| plus bias 17,334 and round term 1,024 | 7,171 |
| shifted right by 11 | 3 |
| clamped to 0..255 | `h[0] = 3` |

That neuron reaches the winning logit as `W2[7][0] * h[0] = 41 * 3 = 123`. The ten
logits come out as −837, −2081, 3631, 3880, −5887, −429, −7564, **7663**, 222 and
−1340, so the prediction is 7 with a margin of 3,783 over the runner-up.

Input 0 belongs to the first of four chunks, so it is covered by launch 0, and
neuron 0 is lane 0 of the first output group. That launch leaves lane 0's
accumulator holding −7,402, or `0xffffe316`, which the CPU reads back as
`lo = 0xe316` and `hi = 0xffff`. The waveform section below shows the hardware
storing exactly those two halves.

## The dense kernel

`programs/simd4/dense4.py` emits the largest dense kernel the 256-slot data memory
holds. The four lanes share one input chunk of K values and each owns one output's
weights, so a launch performs 4K products and leaves four exact accumulators.

```
LANE r3 ; LDI r2,K ; MUL r3,r3,r2        # r3 = lane * K, this lane's weight base
SETLOOP K                                 # r0 counts inputs and starts at zero
L: LOAD r1,r0,0 ; ADD r2,r0,r3 ; LOAD r2,r2,K ; MAC r1,r2 ; ADDI r0,r0,1 ; LOOP L
LANE r0
RDA r1,0 ; STORE r1,r0,5K ; RDA r1,16 ; STORE r1,r0,5K+4 ; HLT
```

| Slots | Contents |
| --- | --- |
| `0 .. K−1` | the input chunk, read by every lane |
| `K .. 5K−1` | lane j's K weights, contiguous at `K + j*K` |
| `5K .. 5K+3` | the low half of each lane's accumulator |
| `5K+4 .. 5K+7` | the high half |

The memory bound is `5K + 8 <= 256`, so K = 49 is exactly the ceiling. That is why
196 inputs are split into four chunks of 49 and not into some rounder shape. The
second layer uses K = 32, the whole hidden vector at once, with the ten outputs
padded to twelve by two zero rows.

`LOOP` branches to an absolute program address, so the builder takes the base it
is packed at. Both kernels live in one 256-word bank, at words 0 and 16, loaded
once; `ENTRY` selects which one a launch runs. Program and data memory survive
`START` and both resets, so the bank is never rewritten.

| Kernel | Instructions | Transfers | Cycles with no waits |
| --- | --- | --- | --- |
| K = 49 | 304 | 400 | 1,008 |
| K = 32 | 202 | 264 | 668 |

`expected_counts` is the only place the formulas `6K + 10` and `8K + 8` are
written down; the driver and the tests read them from there rather than copying
literals, and the diagnostic asserts the device's own counters against them on
every backend.

## Driver and ownership

One inference is 35 launches: four input chunks times eight output groups for
layer 1, then three for layer 2. The order is chunk-major, so each 49-value input
chunk is written once for the eight launches that share it rather than once per
launch, and layer 2's hidden vector is written once for its three.

Weights reach the device as one sequential block because the exporter stores them
in launch order. The same ordering is what the software path walks with a moving
pointer, so a layout mistake cannot make the two paths disagree while both stay
self-consistent.

The driver gained three block writers that make one ownership check and then write
a whole block. `simd4_load` writes all 512 words including the program, which is
the wrong tool 35 times per inference.

Layer 2 reuses the slots layer 1 wrote. That is safe only because the layers run
in order, and the driver says so where it matters.

The poll budget is 2,048, about ten times the binding case. The emulator is that
case, not stalled RTL: a poll is five CPU instructions and the emulator advances
the device once per instruction, so a K = 49 launch needs at least 202 polls
there, while on RTL a poll costs roughly 21 clocks and even three wait cycles per
transfer leaves the requirement near 105. A timeout resets the device, so it is
reported as a failure and never retried silently.

Nothing touches the accelerator until a classification is asked for. Any
successful accelerator register access forces a session into results-mode
comparison, so initializing at boot would have cost the capstone replay its
trace-identical comparison. It is still trace-identical.

## Device time and comparison

The N1 run targets use `--compare results` and deliberately **not**
`--compare-stores`. The diagnostic checks the engine's cycle relation, so it
stores the `CYCLES` and `STALLS` counters, and those are device time: a K = 49
launch costs 1,008 cycles on the emulator and 1,808 on RTL with two wait cycles
per transfer. An ordered-store comparison failed on a correct run, which is
exactly the case the [A2 record](rv32-simd4.md) warned about. Console output,
checkpoints and ordered trap records still have to match, and the counters that
are deterministic, transfers and instructions, are asserted inside the guest on
every backend. Cycles and stalls are never printed, drawn or checksummed.

## Run and inspect

```sh
make test-rv32-digit                  # model, kernels, engine and native C against the oracle
make accuracy-rv32-digit              # 96.16% on the 10,000 vendored images
make run-rv32-digit-emu               # the diagnostic: PASS N1
make run-rv32-digit-rtl               # the same on Icarus
make run-rv32-digit-rtl-verilator     # and with CPU and engine stalls
make run-rv32-digit-menu-emu          # the drawing session from the boot menu
make run-rv32-digit-menu-rtl-verilator
make bench-rv32-digit                 # CPU against accelerator
make waves-rv32-digit                 # one inference, dumped
```

`digitcheck` classifies eight canvases, preprocessing each itself, and checks the
logits against the oracle's. Two of them also run the software model, so the
comparison covers the arithmetic rather than two readings of one computation. It
then recovers from an illegal-opcode fault and from a poll-budget timeout,
confirms that every buffer access is refused while the engine is busy, and ends
with `PASS N1`. All eight canvases are classified correctly.

## Measured costs

One inference, by the two paths, with the fixed startup subtracted by building
each variant at two workload sizes:

| Path | Emulator instructions | RTL cycles |
| --- | --- | --- |
| CPU only | 133,742 | 546,830 |
| Accelerated | 92,281 | 285,742 |

The accelerator roughly halves the RTL cycles and removes about a third of the CPU
instructions. It does not remove more because the work is memory-bound by
construction: each product needs two loads, every lane transfers separately, and
streaming 6,592 weights through the bus costs more than the arithmetic saved. That
is the serialized-memory lesson A1 measured, now visible at the application level.

Per inference the engine retires 10,334 instructions and performs 13,592
transfers across 35 launches. Comparing RTL cycles with emulator instructions
would be meaningless, so the two columns are never divided into one another.

| Run | Backend | Instructions | Cycles |
| --- | --- | --- | --- |
| `digitcheck` | emulator | 1,199,029 | — |
| `digitcheck` | Icarus | 937,329 | 3,983,687 |
| `digitcheck` | Verilator, CPU stall 1, engine stall 2 | 969,529 | 5,329,267 |
| 199-frame drawing session | emulator | 3,865,463 | — |
| 199-frame drawing session | Verilator, stalls as above | 3,819,563 | 20,334,067 |

RTL runs fewer CPU instructions than the emulator because the poll loop spins
fewer times, which is the device-time contract doing its job.

The diagnostic image is 30,036 bytes: 7,432 of text, 22,092 of read-only data
(mostly the 6,592 weights and the eight embedded canvases), 512 of data and 1,048
of bss. The capstone image is 45,808 bytes. Both sit well inside the 256 KiB
slice.

Synthesis is **unchanged**, as it must be: no RTL file differs from G1. The
machine is still 164,861 generic cells and 22,981 flip-flops with no latches.

## Waves

`make waves-rv32-digit` dumps a single inference rather than the diagnostic; the
full diagnostic produced a five-gigabyte VCD, and one inference already contains
every edge worth seeing. From `build/digit/waves/digitbench_hw_1.vcd`, first
launch, 10 ns clock:

1. **START to DONE spans exactly 1,008 clock cycles**, the `20K + 28` the builder
   predicts for K = 49, and 400 transfers are accepted, the predicted `8K + 8`.
   The prediction is not fitted to the hardware: it comes from the instruction
   shape and the testbench agrees.
2. **The first four transfers all read slot 0.** Every lane fetches the same input
   word separately, one transfer each, because the engine has no broadcast load.
   The next four read slots 49, 98, 147 and 196, which is `K + lane*K` for lanes 0
   to 3: the weight layout, visible in the address sequence.
3. **The last eight transfers are the two accumulator halves.** Lanes write their
   low halves to slots 245 to 248 and their high halves to 249 to 252. Lane 0
   stores `0xe316` then `0xffff`, which recombines to `0xffffe316`, or −7,402 —
   the same value the worked example above computes for neuron 0 of image 0.

## Exercises

1. Raise the hidden cap from 255 to 1023 and retrain. Which accumulator bound
   moves, and does accuracy follow?
2. The clamp never fires on real digits. Find the smallest shift `s1` that makes
   it fire on a test image, and explain what that costs.
3. A broadcast load would turn the first four transfers into one. Work out the new
   transfer count for K = 49 and how much of the measured gap it would close.
4. The CPU path multiplies with an 8-step shift-add over the input byte rather than
   the runtime's 32-step helper. Predict the instruction count if it used `*`, then
   measure it.
5. Layer 2 reuses layer 1's slots. Construct the ordering bug that would break it
   and say which existing test catches it.

## Acceptance evidence

The dataset manifest, the model shape, the wrap bound, the preprocessing edge
cases, hand-computed requantization, tie-breaking, the saturating input and the
pinned predictions are checked in Python. The kernel's counts, guards, packing and
arithmetic are checked against the instruction-level interpreter, including an
entire inference reassembled launch by launch, and the dense kernels joined the A2
replay corpus where they run on the C device model and the RTL fixtures. The guest
C is compiled for the host at -O0 and -O2 and compared with the oracle on the 196
preprocessed bytes, the hidden vector, the logits and the argmax, including int32
extremes. On the machine itself, `digitcheck` passes on the emulator, Icarus and
Verilator with identical console output, and the 199-frame drawing session
reproduces 199 identical checkpoints and `PASS badb5523` on the native model, the
emulator and Verilator.

The session is worth replaying because it reads: it draws a vertical stroke and
classifies it as a 1, clears, draws a horizontal-then-diagonal stroke and
classifies it as a 7. A test asserts both predictions, so the replay proves
something beyond reproducing its own hashes.

Preserved: the capstone session is still trace-identical for 2,400,389 lines on
Icarus, `RV32_CAPSTONE_HEX` is unchanged, the G1 menu replay passes with its
re-pinned hash, and the native checks run clean under AddressSanitizer and
UndefinedBehaviorSanitizer.

Counts describe this revision and this workload. They are not hardware speed
measurements, and no wall-clock speedup is claimed.
