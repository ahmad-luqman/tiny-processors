"""One launch of a dense layer: four outputs, one per lane, over a depth-K input chunk.

N1 needs C[n] = sum over k of W[n][k] * x[k] for a 196 -> 32 -> 10 network, which is
far more multiply-accumulate work than the 256-slot data memory can hold at once.
This builder emits the largest piece that does fit: the four lanes share one input
chunk of K values and each owns one output's weights, so a launch retires 4*K
products and leaves four exact 32-bit accumulators.

Data slots, chosen so the largest useful chunk fits the 256-slot memory:

    0        .. K-1        x, the input chunk, read by every lane
    K        .. K+4K-1     W, lane j's K weights contiguous at K + j*K
    5K       .. 5K+3       the low half of each lane's accumulator
    5K+4     .. 5K+7       the high half

so the memory bound is 5K + 8 <= 256 and K = 49 is exactly the ceiling. That is why
the first layer splits 196 inputs into four chunks of 49 rather than any other shape.

The accumulator is 32 bits but a lane register is 16, so the result comes back as
two RDA reads, shift 0 and shift 16, and the CPU rebuilds lo | hi << 16. Nothing is
rounded or saturated here: the engine stays exact and the CPU requantizes, which is
the N1 half of the saturation-and-rounding decision A1 deferred.

A launch retires 6K + 10 instructions and performs 8K + 8 transfers; with no memory
waits that is 20K + 28 cycles by the engine's 2*instructions + transfers + stalls
rule. `expected_counts` is the only place those formulas are written down, so tests
and drivers read them from here rather than copying literals.
"""

from tools.simd4_model import (ADD, ADDI, HLT, LANE, LDI, LOAD, LOOP, MAC, MUL, RDA,
                               SETLOOP, STORE, image, signed, word)

LANES = 4                 # the A2 integration instantiates four lanes
SETUP_WORDS = 4           # LANE, LDI, MUL, SETLOOP
BODY_WORDS = 6            # LOAD, ADD, LOAD, MAC, ADDI, LOOP
TAIL_WORDS = 6            # LANE, RDA, STORE, RDA, STORE, HLT
PROGRAM_WORDS = SETUP_WORDS + BODY_WORDS + TAIL_WORDS      # 16, independent of K


def slots(k):
    """Where the input chunk, the weights and the two result halves live."""
    return {'x': 0, 'w': k, 'lo': 5 * k, 'hi': 5 * k + LANES}


def data_words(k):
    """The highest slot index the kernel touches, plus one."""
    return 5 * k + 2 * LANES


def check(k, base=0):
    """Every bound the kernel depends on, in one place."""
    if not isinstance(k, int) or k < 1:
        raise ValueError('depth K must be a positive integer')
    if data_words(k) > 256:
        raise ValueError(f'depth {k} needs {data_words(k)} data slots; the memory holds 256')
    if not isinstance(base, int) or not 0 <= base:
        raise ValueError('base must be a non-negative program address')
    if base + PROGRAM_WORDS > 256:
        raise ValueError(f'a kernel at {base} would run past the 256-word program memory')
    # Every address the engine forms is (register + immediate) mod 256; none may wrap,
    # or a lane would silently read another lane's weights.
    highest = max(k - 1, k + (LANES - 1) * k + k - 1, data_words(k) - 1)
    if highest > 255:
        raise ValueError(f'depth {k} forms address {highest}, which would wrap')


def program(k, base=0):
    """The 16 words of one dense kernel, placed at `base` in the program image.

    `base` matters: LOOP jumps to an absolute program address, so packing a second
    kernel behind the first means its branch target moves with it.
    """
    check(k, base)
    where = slots(k)
    body = base + SETUP_WORDS
    return [
        word(LANE, rd=3),                              # r3 = lane id
        word(LDI, rd=2, imm=k),                        # r2 = K
        word(MUL, rd=3, ra=3, rb=2),                   # r3 = lane * K, this lane's weight base
        word(SETLOOP, imm=k),                          # the shared counter runs the K products
        # r0 counts inputs and starts at zero: an accepted START clears every lane register.
        word(LOAD, rd=1, ra=0, imm=where['x']),        # r1 = x[j], the same slot in all lanes
        word(ADD, rd=2, ra=0, rb=3),                   # r2 = j + lane * K
        word(LOAD, rd=2, ra=2, imm=where['w']),        # r2 = W[lane][j]
        word(MAC, ra=1, rb=2),                         # acc += sext(r1) * sext(r2), exact
        word(ADDI, rd=0, ra=0, imm=1),                 # next input
        word(LOOP, imm=body),                          # all lanes branch together
        word(LANE, rd=0),                              # r0 = lane, the result slot offset
        word(RDA, rd=1, imm=0),                        # low 16 bits of the accumulator
        word(STORE, rd=1, ra=0, imm=where['lo']),
        word(RDA, rd=1, imm=16),                       # high 16 bits, sign-filled above bit 31
        word(STORE, rd=1, ra=0, imm=where['hi']),
        word(HLT),
    ]


def bank(shapes):
    """Pack several kernels into one 256-word image; returns the image and each entry point."""
    words, entries = [], {}
    for name, k in shapes.items():
        entries[name] = len(words)
        words += program(k, len(words))
    return image(words), entries


def expected_counts(k):
    """Retired instructions and accepted transfers for one launch, checked by the tests."""
    check(k)
    instructions = SETUP_WORDS + BODY_WORDS * k + TAIL_WORDS       # 6K + 10, HLT included
    transfers = LANES * (2 * k + 2)                                # two loads per product, two stores
    return instructions, transfers


def base_cycles(k):
    """Engine cycles with no memory waits: 2 * instructions + transfers."""
    instructions, transfers = expected_counts(k)
    return 2 * instructions + transfers


def memory(x, weights, k):
    """Lay out one launch's data image: the input chunk and four lanes' weights."""
    check(k)
    if len(x) != k or any(not 0 <= value <= 0xffff for value in x):
        raise ValueError(f'expected {k} input words in 0..65535')
    if len(weights) != LANES or any(len(row) != k for row in weights):
        raise ValueError(f'expected {LANES} weight rows of {k}')
    data = [0] * 256
    where = slots(k)
    for j, value in enumerate(x):
        data[where['x'] + j] = value
    for lane, row in enumerate(weights):
        for j, value in enumerate(row):
            data[where['w'] + lane * k + j] = value & 0xffff
    return data


def reference(x, weights, k):
    """The four exact 32-bit accumulators a launch leaves, as the CPU reassembles them."""
    check(k)
    return [sum(signed(value) * signed(w & 0xffff) for value, w in zip(x, row)) % 2**32
            for row in weights]


def results(data, k):
    """Rebuild the four 32-bit accumulators from the stored halves."""
    where = slots(k)
    return [data[where['lo'] + lane] | (data[where['hi'] + lane] << 16) for lane in range(LANES)]
