"""Host-side helpers for the RV32 machine's devices (docs/rv32.md, "Devices").

The frame hash, the event word, and the input-script grammar are each
implemented three times on purpose: here for the runner and the tests, in
tools/rv32emu.c, and in tests/rv32_tb.sv. The tests use this module as the
independent reference for the other two.
"""

FB_COLUMNS, FB_ROWS = 320, 240
FB_SIZE = FB_COLUMNS * FB_ROWS
FB_WORDS = FB_SIZE // 4
M = 0xFFFFFFFF

EVENT_VALID = 0x80000000
EVENT_PRESS = 0x100
QUEUE_SIZE = 16
# programs/rv32/board.h, RV32_KEY_*.
KEYS = {"LEFT": 1, "RIGHT": 2, "UP": 3, "DOWN": 4, "SPACE": 5, "ENTER": 6, "ESCAPE": 7,
        "A": 8, "D": 9, "W": 10, "S": 11, "P": 12, "Q": 13, "R": 14}


def frame_hash(pixels):
    """The checkpoint hash of a framebuffer's bytes: h = ((h << 5) + h) ^ word from 5381 over its
    little-endian words (shift, add, xor; no multiply)."""
    if len(pixels) % 4:
        raise ValueError(f"{len(pixels)} bytes is not a whole number of words")
    h = 5381
    for i in range(0, len(pixels), 4):
        h = (((h << 5) + h) ^ int.from_bytes(pixels[i:i + 4], "little")) & M
    return h


def event_word(press, code):
    """The word EVENT returns for a press or release of key `code` (0..31)."""
    if not 0 <= code < 32:
        raise ValueError(f"key code {code} is not 0..31")
    return EVENT_VALID | (EVENT_PRESS if press else 0) | code


def is_decimal(text):
    """The script's rule for a number: one to nine ASCII digits, so every reader (this one, C's
    strtoul, the testbench's 32-bit arithmetic) accepts exactly the same tokens."""
    return 1 <= len(text) <= 9 and text.isascii() and text.isdigit()


def key_code(text):
    """A key name from board.h (any case) or a number 0..31."""
    name = text.upper()
    if name in KEYS:
        return KEYS[name]
    if is_decimal(text) and int(text) < 32:
        return int(text)
    raise ValueError(f"unknown key {text!r}")


def parse_input_script(text):
    """`frame N down|up KEY` lines to a list of (frame, event word); frames must not decrease.

    Blank lines and lines starting with `#` are skipped. Every other line must
    be exactly four tokens; anything else raises ValueError naming the line.
    """
    events, last_frame = [], 0
    for number, line in enumerate(text.splitlines(), 1):
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        tokens = stripped.split()
        try:
            if len(tokens) != 4 or tokens[0] != "frame" or tokens[2] not in ("down", "up"):
                raise ValueError("expected `frame N down|up KEY`")
            if not is_decimal(tokens[1]):
                raise ValueError(f"frame {tokens[1]!r} is not one to nine digits")
            frame = int(tokens[1])
            if frame < last_frame:
                raise ValueError(f"frame {frame} comes after frame {last_frame}")
            events.append((frame, event_word(tokens[2] == "down", key_code(tokens[3]))))
        except ValueError as error:
            raise ValueError(f"input script line {number}: {error}: {line!r}") from None
        last_frame = frame
    return events


def render_diag_frame(frame):
    """The framebuffer the M5 diagnostic (programs/rv32/diag.c) presents as frame 1 or 2, drawn
    here independently: pixel (x, y) = (x ^ y) & 0xFF, a red box, a green row, then a blue box."""
    if frame not in (1, 2):
        raise ValueError(f"the diagnostic presents frames 1 and 2, not {frame}")
    pixels = bytearray((x ^ y) & 0xFF for y in range(FB_ROWS) for x in range(FB_COLUMNS))
    for y in range(40, 80):
        for x in range(100, 200):
            pixels[y * FB_COLUMNS + x] = 0xE0
    for x in range(FB_COLUMNS):
        pixels[120 * FB_COLUMNS + x] = 0x1C
    if frame == 2:
        for y in range(200, 220):
            for x in range(10, 30):
                pixels[y * FB_COLUMNS + x] = 0x03
    return bytes(pixels)


def fnv_fold(values):
    """The diagnostic's checksum: FNV-1a over 32-bit values, as selfcheck.c and diag.c fold them."""
    h = 2166136261
    for value in values:
        h = ((h ^ value) * 16777619) & M
    return h


# Every value programs/rv32/diag.c folds into its checksum, in order: the CHECK expectations and
# the folded values (the frame-1 hash and the four events). Timer readings are never folded.
DIAG_EXPECTED_VALUES = [
    1, 1,                                            # timer advanced; wrapped after the write
    4, 5, 0x50000000, 5, 0x20000000, 7, 0x20001008, 7, 0x30000000 + FB_SIZE,  # four faults
    320, 240, 0,                                     # WIDTH, HEIGHT, FRAMES before the first present
    "frame1",                                        # the readback hash of frame 1
    1,                                               # FRAMES after the first present
    3, 1 << KEYS["SPACE"],                           # COUNT and KEYS after frame 1's events
    EVENT_VALID | EVENT_PRESS | KEYS["LEFT"], EVENT_VALID | EVENT_PRESS | KEYS["SPACE"], EVENT_VALID | KEYS["LEFT"],
    3, 2, 1, 0,                                      # events so far, FRAMES, COUNT, KEYS after the second present
    EVENT_VALID | KEYS["SPACE"],
    4,                                               # events in all
]


def diag_checksum():
    """The `PASS <hex>` value the diagnostic prints, derived here rather than copied from a run."""
    frame1 = frame_hash(render_diag_frame(1))
    return fnv_fold(frame1 if value == "frame1" else value for value in DIAG_EXPECTED_VALUES)
