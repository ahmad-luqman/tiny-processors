# MNIST handwritten digit test set

The N1 milestone's accuracy measurement and firmware diagnostic read the MNIST
**test set** from this directory. The 60,000-image training set is not vendored:
`tools/digit_train.py` fetches it into the ignored `build/mnist/` directory and
checks it against the digests recorded below.

## Files

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `t10k-images-idx3-ubyte.gz` | 1648877 | `8d422c7b0a1c1c79245a5bcf07fe86e33eeafee792b84584aec276f5a2dbc4e6` |
| `t10k-labels-idx1-ubyte.gz` | 4542 | `f7ae60f92e00ec6debd23a6088c31dbd2371eca3ffa0defaefb259924204aec6` |

Not vendored, fetched by the training script only:

| File | Bytes | SHA-256 |
| --- | --- | --- |
| `train-images-idx3-ubyte.gz` | 9912422 | `440fcabf73cc546fa21475e81ea370265605f56be210a4024d2ca8f203523609` |
| `train-labels-idx1-ubyte.gz` | 28881 | `3552534a0a558bbed6aed32b30c495cca23d567ec52cac8be1a0730e8010255c` |

`SHA256SUMS.json` records the digests of the vendored files and is checked by
`tools/digit_data.verify_dataset()`, which also asserts that the manifest lists
exactly the files present. This mirrors the SoftFloat manifest check in
`tools/fp32_vectors.verify_reference_sources()`.

## Provenance, as verified on 2026-09-22

The original distribution page, `http://yann.lecun.com/exdb/mnist/`, **no longer
serves the dataset**: on 2026-09-22 that URL returned an empty Apache directory
index, with the description page and the four `.gz` files gone. The content was
read instead from the Internet Archive's capture of that page
(`https://web.archive.org/web/2023id_/http://yann.lecun.com/exdb/mnist/`), which
is the authority quoted throughout this file.

The two vendored files were downloaded from two independent mirrors:

- `https://ossci-datasets.s3.amazonaws.com/mnist/` (the mirror used by PyTorch)
- `https://storage.googleapis.com/cvdf-datasets/mnist/` (the Common Visual Data
  Foundation mirror, `https://github.com/cvdfoundation/mnist`, which describes
  itself as a mirror of the original site and carries no license file of its own)

All four files fetched from the two mirrors are byte-identical (the SHA-256
digests above are from both), and each file's size matches the size the archived
original page documents for it. Those three agreements are the authenticity
evidence for this copy.

## License

**The source imposes no license terms, and none is asserted here.** The archived
original page contains no license, copyright or terms-of-use statement of any
kind: it describes the dataset's construction, documents the IDX file format,
names its authors, and ends with "Happy hacking." Searching that page for
"licen", "copyright", "Creative", "public domain" and "terms" returns nothing.
The CVDF mirror repository likewise has no `LICENSE` file.

Third-party catalogues sometimes label MNIST "CC BY-SA 3.0". That claim could not
be traced to any statement by the dataset's authors and is therefore **not**
repeated as fact here.

What the authors do ask for is documented on the archived page: if digits are
pre-processed by bounding-box centring rather than the centre-of-mass centring
used to build the distributed images, "you should report it in your
publications." N1 centres a guest-drawn canvas before pooling and records that in
`docs/rv32-digit.md`.

MNIST is itself derived from NIST Special Database 1 and Special Database 3, works
produced by a United States federal agency.

## Attribution

Yann LeCun (Courant Institute, NYU), Corinna Cortes (Google Labs, New York) and
Christopher J.C. Burges (Microsoft Research, Redmond), "The MNIST database of
handwritten digits", as published at `http://yann.lecun.com/exdb/mnist/`.

The canonical paper the archived page cites for the database is Y. LeCun,
L. Bottou, Y. Bengio and P. Haffner, "Gradient-based learning applied to document
recognition", Proceedings of the IEEE, 86(11):2278-2324, November 1998.

## Format

Both files are gzip-compressed IDX. The image file has a 16-byte header (magic
`0x00000803`, count, rows, columns, each a big-endian 32-bit integer) followed by
one unsigned byte per pixel, row-major; the label file has an 8-byte header
(magic `0x00000801`, count) followed by one byte per label. The archived page
states that pixel value 0 means background and 255 means foreground, that the
digits were size-normalised into a 20x20 box preserving aspect ratio, and that
they were then centred in the 28x28 field by the centre of mass of the pixels.
`tools/digit_data.py` parses both files with the standard library only.
