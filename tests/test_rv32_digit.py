"""N1: dataset provenance, the preprocessing contract, and the integer model oracle."""
import hashlib
import json
from pathlib import Path
import struct
import tempfile
import unittest

from tools import digit_data, digit_ref

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


if __name__ == '__main__':
    unittest.main()
