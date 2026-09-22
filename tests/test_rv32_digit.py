"""N1: dataset provenance, the preprocessing contract, and the integer model oracle."""
import hashlib
import json
import random
from pathlib import Path
import struct
import tempfile
import unittest

from programs.simd4 import dense4
from tools import digit_data, digit_ref
from tools.rv32_digit_kernels import LAYER1_DEPTH, LAYER2_DEPTH, kernels
from tools.rv32_digit_model import launch_order_weights
from tools.rv32_digit_native import Model, build
from tools.simd4_model import execute, signed

ROOT = Path(__file__).resolve().parents[1]


def canvas(pixels):
    """A 784-byte canvas from {(row, column): value} entries."""
    out = bytearray(digit_data.PIXELS)
    for (row, column), value in pixels.items():
        out[row * digit_data.SIDE + column] = value
    return bytes(out)


class DatasetTest(unittest.TestCase):
    """The vendored files are what the manifest says, and the manifest is complete."""

    def test_vendored_fingerprints(self):
        digit_data.verify_dataset()

    def test_manifest_rejects_empty_missing_extra_and_changed_files(self):
        with tempfile.TemporaryDirectory() as work:
            directory = Path(work)
            (directory / 'data.gz').write_bytes(b'reference')
            manifest = directory / 'SHA256SUMS.json'
            manifest.write_text('{}')
            with self.assertRaisesRegex(ValueError, 'must list every'):
                digit_data.verify_dataset(directory)
            digest = hashlib.sha256(b'reference').hexdigest()
            manifest.write_text(json.dumps({'data.gz': digest}))
            digit_data.verify_dataset(directory)
            (directory / 'README.md').write_text('excluded from the manifest by name')
            digit_data.verify_dataset(directory)
            (directory / 'stray.gz').write_bytes(b'extra')
            with self.assertRaisesRegex(ValueError, 'must list every'):
                digit_data.verify_dataset(directory)
            (directory / 'stray.gz').unlink()
            (directory / 'data.gz').write_bytes(b'changed')
            with self.assertRaisesRegex(ValueError, 'fingerprint mismatch: data.gz'):
                digit_data.verify_dataset(directory)
            (directory / 'data.gz').unlink()
            with self.assertRaisesRegex(ValueError, 'must list every'):
                digit_data.verify_dataset(directory)

    def test_idx_headers_and_counts(self):
        images, labels = digit_data.load_test_set()
        self.assertEqual((len(images), len(labels)), (10000, 10000))
        self.assertTrue(all(len(image) == digit_data.PIXELS for image in images))
        self.assertTrue(all(0 <= label <= 9 for label in labels))
        # The header fields the parser insists on, read independently here.
        import gzip
        with gzip.open(digit_data.DATASET / 't10k-images-idx3-ubyte.gz', 'rb') as stream:
            magic, count, rows, columns = struct.unpack('>IIII', stream.read(16))
        self.assertEqual((magic, count, rows, columns), (0x803, 10000, 28, 28))
        with gzip.open(digit_data.DATASET / 't10k-labels-idx1-ubyte.gz', 'rb') as stream:
            magic, count = struct.unpack('>II', stream.read(8))
        self.assertEqual((magic, count), (0x801, 10000))

    def test_truncated_and_mistyped_files_are_refused(self):
        import gzip
        with tempfile.TemporaryDirectory() as work:
            short = Path(work) / 'short.gz'
            with gzip.open(short, 'wb') as stream:
                stream.write(struct.pack('>IIII', 0x803, 2, 28, 28) + bytes(digit_data.PIXELS))
            with self.assertRaisesRegex(ValueError, 'header promises'):
                digit_data.read_images(short)
            wrong = Path(work) / 'wrong.gz'
            with gzip.open(wrong, 'wb') as stream:
                stream.write(struct.pack('>IIII', 0x803, 1, 20, 20) + bytes(400))
            with self.assertRaisesRegex(ValueError, 'expected'):
                digit_data.read_images(wrong)
            labels = Path(work) / 'labels.gz'
            with gzip.open(labels, 'wb') as stream:
                stream.write(struct.pack('>II', 0x801, 1) + bytes([10]))
            with self.assertRaisesRegex(ValueError, 'outside 0..9'):
                digit_data.read_labels(labels)


class PreprocessingTest(unittest.TestCase):
    """`prepare` is one contract: centre by bounding box, then average 2x2 blocks.

    These check the 196 output bytes directly. A logit comparison cannot see
    preprocessing that every inference path skipped alike.
    """

    def test_blank_canvas_is_all_zero(self):
        self.assertEqual(digit_data.prepare(bytes(digit_data.PIXELS)), bytes(digit_data.INPUTS))

    def test_single_pixel_centres_regardless_of_where_it_started(self):
        middle = (digit_data.SIDE - 1) // 2      # a 1-wide box leaves 27 columns, floor to 13
        for position in ((0, 0), (27, 27), (0, 27), (13, 6)):
            centred = digit_data.centre(canvas({position: 255}))
            self.assertEqual(centred.index(255), middle * digit_data.SIDE + middle, position)
            self.assertEqual(sum(centred), 255)

    def test_odd_bounding_box_floors_the_shift(self):
        # A 3-wide, 2-tall box: columns floor to (28-3)//2 = 12, rows to (28-2)//2 = 13.
        ink = {(5, 1): 255, (5, 2): 200, (5, 3): 150, (6, 2): 100}
        centred = digit_data.centre(canvas(ink))
        self.assertEqual(centred[13 * digit_data.SIDE + 12], 255)
        self.assertEqual(centred[13 * digit_data.SIDE + 13], 200)
        self.assertEqual(centred[13 * digit_data.SIDE + 14], 150)
        self.assertEqual(centred[14 * digit_data.SIDE + 13], 100)
        self.assertEqual(sum(centred), 705)

    def test_already_centred_canvas_is_unchanged(self):
        ink = {(13, 13): 255, (13, 14): 255, (14, 13): 255, (14, 14): 255}
        image = canvas(ink)
        self.assertEqual(digit_data.centre(image), image)

    def test_pooling_averages_each_block_with_truncation(self):
        # One 2x2 block at the top left: (255+254+1+0)>>2 = 127, everything else zero.
        image = canvas({(0, 0): 255, (0, 1): 254, (1, 0): 1})
        pooled = digit_data.pool(image)          # pool only, so the ink stays in place
        self.assertEqual(pooled[0], (255 + 254 + 1) >> 2)
        self.assertEqual(sum(pooled), (255 + 254 + 1) >> 2)

    def test_prepare_is_centre_then_pool_and_rejects_bad_sizes(self):
        image = canvas({(2, 3): 255, (4, 9): 128})
        self.assertEqual(digit_data.prepare(image), digit_data.pool(digit_data.centre(image)))
        with self.assertRaisesRegex(ValueError, 'must be 784 bytes'):
            digit_data.prepare(bytes(100))

    def test_prepared_inputs_stay_in_the_legal_mac_range(self):
        images, _ = digit_data.load_test_set()
        for image in images[:200]:
            prepared = digit_data.prepare(image)
            self.assertEqual(len(prepared), digit_data.INPUTS)
            self.assertLessEqual(max(prepared), 255)


class ModelTest(unittest.TestCase):
    """Shape, range, wrap safety and hand-computed arithmetic for the integer model."""

    @classmethod
    def setUpClass(cls):
        cls.model = digit_ref.load_model()

    def test_shapes_and_ranges(self):
        shape = self.model['architecture']
        self.assertEqual(shape['inputs'], digit_data.INPUTS)
        self.assertEqual((len(self.model['w1']), len(self.model['w1'][0])), (shape['hidden'], shape['inputs']))
        self.assertEqual((len(self.model['w2']), len(self.model['w2'][0])), (shape['classes'], shape['hidden']))
        weights = [value for row in self.model['w1'] + self.model['w2'] for value in row]
        self.assertGreaterEqual(min(weights), -127)
        self.assertLessEqual(max(weights), 127)
        self.assertTrue(0 <= self.model['shift'] <= 31)
        self.assertTrue(0 < self.model['hidden_max'] <= 32767)

    def test_loader_refuses_a_damaged_model(self):
        for damage, message in (
                ({'shift': 32}, 'shift must be'),
                ({'hidden_max': 40000}, 'legal 16-bit'),
                ({'w1': [[0] * digit_data.INPUTS] * 3}, 'layer 1 shape'),
                ({'w2': [[0] * 4] * 10}, 'layer 2 shape')):
            broken = dict(self.model, **damage)
            with tempfile.TemporaryDirectory() as work:
                path = Path(work) / 'model.json'
                path.write_text(json.dumps(broken))
                with self.assertRaisesRegex(ValueError, message):
                    digit_ref.load_model(path)
        weights = [row[:] for row in self.model['w1']]
        weights[0][0] = -128
        with tempfile.TemporaryDirectory() as work:
            path = Path(work) / 'model.json'
            path.write_text(json.dumps(dict(self.model, w1=weights)))
            with self.assertRaisesRegex(ValueError, 'int8 and symmetric'):
                digit_ref.load_model(path)

    def test_accumulators_cannot_wrap(self):
        """The bound the exporter proved, recomputed here from the committed weights."""
        shift = self.model['shift']
        roundterm = (1 << (shift - 1)) if shift else 0
        first = digit_data.INPUTS * 255 * 128 + max(abs(b) for b in self.model['b1']) + roundterm
        second = (self.model['architecture']['hidden'] * self.model['hidden_max'] * 128 +
                  max(abs(b) for b in self.model['b2']))
        self.assertLess(first, 2 ** 31)
        self.assertLess(second, 2 ** 31)
        self.assertEqual([first, second], self.model['metadata']['accumulator_bounds'])

    def test_hidden_neuron_arithmetic_by_hand(self):
        """Bias, round-half-up, ReLU and saturation on a model built for this test."""
        def hidden(weights, inputs, bias, shift, hmax=255):
            toy = {'w1': [weights], 'b1': [bias], 'shift': shift, 'hidden_max': hmax}
            return digit_ref.hidden_layer(inputs, toy)[0]

        # 2*3 + 4*5 = 26, +6 bias = 32, shift 2 with round term 2 -> 34>>2 = 8.
        self.assertEqual(hidden([2, 4], [3, 5], 6, 2), 8)
        # Exactly halfway: 7 + round 4 = 11, 11>>3 = 1; round-half-up crosses at 4.
        self.assertEqual(hidden([7], [1], 0, 3), 1)
        self.assertEqual(hidden([3], [1], 0, 3), 0)
        # shift 0 adds no round term and returns the sum itself, saturated.
        self.assertEqual(hidden([5], [7], 3, 0), 38)
        self.assertEqual(hidden([100], [100], 0, 0), 255)
        # A negative pre-activation is ReLU'd to zero, never shifted.
        self.assertEqual(hidden([-100], [100], 0, 4), 0)
        self.assertEqual(hidden([-1], [1], 0, 0), 0)
        # Saturation clamps at the declared maximum, both at 255 and at a smaller cap.
        self.assertEqual(hidden([1000], [1000], 0, 4, hmax=255), 255)
        self.assertEqual(hidden([1000], [1000], 0, 4, hmax=100), 100)

    def test_argmax_takes_the_lowest_index_on_a_tie(self):
        self.assertEqual(digit_ref.argmax([5, 9, 9, 1]), (1, 0))
        self.assertEqual(digit_ref.argmax([3, 3, 3]), (0, 0))
        self.assertEqual(digit_ref.argmax([-10, -4, -7]), (1, 3))
        self.assertEqual(digit_ref.argmax([0] * 9 + [1]), (9, 1))

    def test_synthetic_inputs_are_classified_without_error(self):
        """Extremes must produce well-formed logits, including a saturating input."""
        for name, x in (('zero', bytes(digit_data.INPUTS)),
                        ('full', bytes([255] * digit_data.INPUTS)),
                        ('checker', bytes((255 if i % 2 else 0) for i in range(digit_data.INPUTS)))):
            logits = digit_ref.infer(x, self.model)
            self.assertEqual(len(logits), 10, name)
            self.assertTrue(all(abs(value) < 2 ** 31 for value in logits), name)
            best, margin = digit_ref.argmax(logits)
            self.assertTrue(0 <= best <= 9, name)
            self.assertGreaterEqual(margin, 0, name)

    def test_an_engineered_input_reaches_the_saturation_clamp(self):
        """The clamp is unreachable on real digits, so construct the input that hits it.

        Driving every positively weighted input of one neuron to 255 maximizes that
        neuron's accumulator. An all-white canvas does not saturate anything: the
        negative weights cancel most of the sum, and the shift was calibrated on real
        activations, which peak near half the cap.
        """
        for neuron, weights in enumerate(self.model['w1']):
            x = bytes(255 if value > 0 else 0 for value in weights)
            if digit_ref.hidden_layer(x, self.model)[neuron] == self.model['hidden_max']:
                break
        else:
            self.fail('no input saturated any hidden unit; the clamp would be dead code')
        self.assertEqual(digit_ref.hidden_layer(x, self.model)[neuron], self.model['hidden_max'])
        # The same canvas still classifies cleanly, clamp and all.
        best, margin = digit_ref.argmax(digit_ref.infer(x, self.model))
        self.assertTrue(0 <= best <= 9)
        self.assertGreaterEqual(margin, 0)

    def test_real_digits_stay_below_the_clamp(self):
        """Recorded as a property of this model: saturation is a guard, not routine."""
        images, _ = digit_data.load_test_set()
        peak = max(max(digit_ref.hidden_layer(digit_data.prepare(image), self.model))
                   for image in images[:500])
        self.assertLess(peak, self.model['hidden_max'])

    def test_predictions_match_the_pinned_file(self):
        """The stdlib oracle reproduces the exporter's numpy arithmetic exactly."""
        pinned = [int(value) for value in digit_ref.PREDICTIONS.read_text().split()]
        images, labels = digit_data.load_test_set()
        self.assertEqual(len(pinned), len(images))
        model = self.model
        mine = [digit_ref.predict(digit_data.prepare(image), model) for image in images[:1000]]
        self.assertEqual(mine, pinned[:1000])
        correct = sum(p == label for p, label in zip(pinned, labels))
        self.assertEqual(correct / len(labels), self.model['metadata']['int_test_accuracy'])
        self.assertGreaterEqual(correct / len(labels), 0.95)


class KernelTest(unittest.TestCase):
    """The dense kernel's shape, its guards, and its arithmetic on the engine."""

    def test_counts_follow_the_documented_formulas(self):
        for depth in (1, 7, 32, 49):
            instructions, transfers = dense4.expected_counts(depth)
            self.assertEqual(instructions, 6 * depth + 10)      # HLT included
            self.assertEqual(transfers, 8 * depth + 8)
            self.assertEqual(dense4.base_cycles(depth), 20 * depth + 28)
            self.assertEqual(len(dense4.program(depth)), dense4.PROGRAM_WORDS)

    def test_guards_refuse_shapes_that_would_not_fit(self):
        with self.assertRaisesRegex(ValueError, 'data slots'):
            dense4.program(50)                       # 5*50+8 = 258 slots
        for bad in (0, -1):
            with self.assertRaisesRegex(ValueError, 'positive integer'):
                dense4.program(bad)
        with self.assertRaisesRegex(ValueError, 'program memory'):
            dense4.program(8, 241)                   # 241+16 > 256
        dense4.program(49)                           # the exact ceiling is allowed
        dense4.program(8, 240)

    def test_the_loop_target_moves_with_the_base(self):
        """A packed kernel branches to its own body, not the first kernel's."""
        for base in (0, 16, 100):
            words = dense4.program(32, base)
            target = words[dense4.SETUP_WORDS + dense4.BODY_WORDS - 1] & 0xffff
            self.assertEqual(target, base + dense4.SETUP_WORDS)

    def test_bank_packs_both_kernels_with_distinct_entries(self):
        image, entries = kernels()
        self.assertEqual(entries, {'layer1': 0, 'layer2': dense4.PROGRAM_WORDS})
        self.assertEqual(len(image), 256)
        self.assertTrue(all(word == 0 for word in image[2 * dense4.PROGRAM_WORDS:]))

    def test_engine_reproduces_the_dot_products_including_sign_extremes(self):
        for depth, base in ((LAYER1_DEPTH, 0), (LAYER2_DEPTH, dense4.PROGRAM_WORDS)):
            program = [0] * 256
            for offset, word in enumerate(dense4.program(depth, base)):
                program[base + offset] = word
            rng = random.Random(4096 + depth)
            for trial in range(60):
                if trial == 0:
                    x, weights = [255] * depth, [[127] * depth] * dense4.LANES
                elif trial == 1:
                    x, weights = [255] * depth, [[-127 & 0xffff] * depth] * dense4.LANES
                elif trial == 2:
                    x, weights = [0] * depth, [[0] * depth] * dense4.LANES
                else:
                    x = [rng.randrange(256) for _ in range(depth)]
                    weights = [[rng.randrange(-127, 128) & 0xffff for _ in range(depth)]
                               for _ in range(dense4.LANES)]
                run = execute(program, dense4.memory(x, weights, depth), entry=base)
                self.assertFalse(run.fault)
                self.assertEqual(dense4.results(run.memory, depth), dense4.reference(x, weights, depth))
                instructions, transfers = dense4.expected_counts(depth)
                self.assertEqual(len(run.retirements), instructions)
                self.assertEqual(len(run.transfers), transfers)

    def test_a_whole_inference_on_the_engine_matches_the_oracle(self):
        """Every launch the guest driver makes, run through the interpreter."""
        model = digit_ref.load_model()
        image, entries = kernels()
        first, second = launch_order_weights(model)
        hidden_count = model['architecture']['hidden']
        images, _ = digit_data.load_test_set()
        for index in (0, 1, 7):
            x = digit_data.prepare(images[index])
            accumulators = [0] * hidden_count
            launches = 0
            for chunk in range(len(x) // LAYER1_DEPTH):
                piece = list(x[chunk * LAYER1_DEPTH:(chunk + 1) * LAYER1_DEPTH])
                for group in range(hidden_count // dense4.LANES):
                    block = first[chunk * (hidden_count // dense4.LANES) + group]
                    weights = [[value & 0xffff for value in
                                block[lane * LAYER1_DEPTH:(lane + 1) * LAYER1_DEPTH]]
                               for lane in range(dense4.LANES)]
                    run = execute(image, dense4.memory(piece, weights, LAYER1_DEPTH),
                                  entry=entries['layer1'])
                    self.assertFalse(run.fault)
                    for lane, value in enumerate(dense4.results(run.memory, LAYER1_DEPTH)):
                        neuron = group * dense4.LANES + lane
                        accumulators[neuron] = (accumulators[neuron] + value) % 2 ** 32
                    launches += 1
            shift, cap = model['shift'], model['hidden_max']
            roundterm = (1 << (shift - 1)) if shift else 0
            hidden = []
            for neuron in range(hidden_count):
                total = signed((accumulators[neuron] + model['b1'][neuron] + roundterm) % 2 ** 32, 32)
                hidden.append(0 if total < 0 else min(total >> shift, cap))
            self.assertEqual(hidden, digit_ref.hidden_layer(x, model))
            logits = []
            for launch, block in enumerate(second):
                weights = [[value & 0xffff for value in
                            block[lane * hidden_count:(lane + 1) * hidden_count]]
                           for lane in range(dense4.LANES)]
                run = execute(image, dense4.memory(hidden, weights, LAYER2_DEPTH),
                              entry=entries['layer2'])
                self.assertFalse(run.fault)
                for lane, value in enumerate(dense4.results(run.memory, LAYER2_DEPTH)):
                    class_index = launch * dense4.LANES + lane
                    if class_index < model['architecture']['classes']:
                        logits.append(signed(value, 32) + model['b2'][class_index])
                launches += 1
            self.assertEqual(logits, digit_ref.infer(x, model))
            self.assertEqual(launches, 35)

    def test_launch_order_weights_reindex_the_matrices(self):
        model = digit_ref.load_model()
        first, second = launch_order_weights(model)
        groups = model['architecture']['hidden'] // dense4.LANES
        self.assertEqual(len(first), 32)
        self.assertEqual(len(second), 3)
        for chunk in range(4):
            for group in range(groups):
                block = first[chunk * groups + group]
                for lane in range(dense4.LANES):
                    row = model['w1'][group * dense4.LANES + lane]
                    self.assertEqual(block[lane * LAYER1_DEPTH:(lane + 1) * LAYER1_DEPTH],
                                     row[chunk * LAYER1_DEPTH:(chunk + 1) * LAYER1_DEPTH])
        # The two padding classes are zero, so their lanes contribute nothing.
        self.assertEqual(second[2][2 * LAYER2_DEPTH:], [0] * (2 * LAYER2_DEPTH))


class NativeModelTest(unittest.TestCase):
    """The guest C, compiled for the host, against the standard-library oracle."""

    @classmethod
    def setUpClass(cls):
        cls.builds = {level: Model(build(level)) for level in ('O0', 'O2')}
        cls.model = digit_ref.load_model()
        cls.images, cls.labels = digit_data.load_test_set()

    def each(self, check):
        for level, model in self.builds.items():
            with self.subTest(level=level):
                check(model)

    def test_constants_agree_with_the_python_side(self):
        def check(guest):
            self.assertEqual(guest.inputs, digit_data.INPUTS)
            self.assertEqual(guest.pixels, digit_data.PIXELS)
            self.assertEqual(guest.classes, self.model['architecture']['classes'])
            self.assertEqual(guest.hidden_size, self.model['architecture']['hidden'])
            self.assertEqual(guest.library.digit_native_shift(), self.model['shift'])
            self.assertEqual(guest.library.digit_native_hidden_max(), self.model['hidden_max'])
        self.each(check)

    def test_preprocessing_is_byte_identical(self):
        """The 196 inputs themselves, not just the logits they lead to."""
        canvases = [bytes(digit_data.PIXELS), bytes([255] * digit_data.PIXELS)]
        for position in ((0, 0), (27, 27), (0, 27), (13, 13), (5, 20)):
            canvases.append(canvas({position: 255}))
        canvases.append(canvas({(5, 1): 255, (5, 2): 200, (5, 3): 150, (6, 2): 100}))
        canvases.append(canvas({(0, 0): 3, (27, 27): 1}))       # ink at both corners
        canvases += [self.images[index] for index in range(200)]

        def check(guest):
            for index, image in enumerate(canvases):
                self.assertEqual(guest.prepare(image), digit_data.prepare(image), index)
        self.each(check)

    def test_hidden_and_logits_are_exact(self):
        prepared = [digit_data.prepare(image) for image in self.images[:200]]

        def check(guest):
            for index, x in enumerate(prepared):
                self.assertEqual(guest.hidden(x), digit_ref.hidden_layer(x, self.model), index)
                self.assertEqual(guest.infer(x), digit_ref.infer(x, self.model), index)
        self.each(check)

    def test_synthetic_and_saturating_inputs_are_exact(self):
        inputs = [bytes(digit_data.INPUTS), bytes([255] * digit_data.INPUTS),
                  bytes((255 if i % 2 else 0) for i in range(digit_data.INPUTS))]
        for weights in self.model['w1']:
            inputs.append(bytes(255 if value > 0 else 0 for value in weights))

        def check(guest):
            for x in inputs:
                self.assertEqual(guest.infer(x), digit_ref.infer(x, self.model))
                self.assertEqual(guest.hidden(x), digit_ref.hidden_layer(x, self.model))
        self.each(check)

    def test_argmax_matches_including_signed_overflow_cases(self):
        """The margin is unsigned because these differences overflow int32."""
        cases = [[5, 9, 9, 1, 0, 0, 0, 0, 0, 0], [3] * 10, [-10, -4, -7] + [0] * 7,
                 [0] * 9 + [1], [2147483647] + [-2147483648] * 9,
                 [-2147483648] * 9 + [2147483647]]
        rng = random.Random(11)
        cases += [[rng.randrange(-2 ** 31, 2 ** 31) for _ in range(10)] for _ in range(300)]

        def check(guest):
            for logits in cases:
                best, margin = guest.argmax(logits)
                want_best, want_margin = digit_ref.argmax(logits)
                self.assertEqual(best, want_best, logits[:4])
                self.assertEqual(margin, want_margin % 2 ** 32, logits[:4])
        self.each(check)

    def test_classification_agrees_with_the_pinned_predictions(self):
        pinned = [int(value) for value in digit_ref.PREDICTIONS.read_text().split()]

        def check(guest):
            for index in range(200):
                logits = guest.infer(guest.prepare(self.images[index]))
                self.assertEqual(guest.argmax(logits)[0], pinned[index], index)
        self.each(check)


if __name__ == '__main__':
    unittest.main()
