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
    """The checkpoint hash of a framebuffer's bytes: shift-add over its little-endian words."""
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


def key_code(text):
    """A key name from board.h (any case) or a number 0..31."""
    name = text.upper()
    if name in KEYS:
        return KEYS[name]
    if text.isdigit() and int(text) < 32:
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
            if not tokens[1].isdigit():
                raise ValueError(f"frame {tokens[1]!r} is not a number")
            frame = int(tokens[1])
            if frame < last_frame:
                raise ValueError(f"frame {frame} comes after frame {last_frame}")
            events.append((frame, event_word(tokens[2] == "down", key_code(tokens[3]))))
        except ValueError as error:
            raise ValueError(f"input script line {number}: {error}: {line!r}") from None
        last_frame = frame
    return events
