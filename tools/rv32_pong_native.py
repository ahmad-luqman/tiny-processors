#!/usr/bin/env python3
"""Drive Pong's C on the host: build it natively, run a script frame by frame, hash the frames.

programs/rv32/pong_game.c and gfx.c never touch a device, so the same source
compiles for the Mac (as tests/test_rv32_rt.py does for rt/muldiv.c). This
module wraps that build through ctypes, and its `run_script` is the guest
loop of programs/rv32/pong.c line for line: pop the events of the previous
frame, quit if asked, simulate one frame from the held keys, draw, present.
The checkpoints it produces are what the emulator and the RTL must reproduce;
`--write` regenerates programs/rv32/pong.expected from programs/rv32/pong.input
and prints the PASS word for the Makefile.

The logic is checked against hand-computed expectations in
tests/test_rv32_pong.py; the pixels are the same C run natively, so the
expected file is a record of this build, not an independent rendering.
"""

import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_devices import EVENT_PRESS, FB_SIZE, QUEUE_SIZE, frame_hash, parse_input_script  # noqa: E402

SOURCES = (ROOT / "programs/rv32/gfx.c", ROOT / "programs/rv32/pong_game.c")
HOST_DIR = ROOT / "build/rv32/host"
INPUT = ROOT / "programs/rv32/pong.input"
EXPECTED = ROOT / "programs/rv32/pong.expected"
WIDTH, HEIGHT = 320, 240
PHASES = ("serve", "play", "paused", "over")


class Surface(ctypes.Structure):
    _fields_ = [("pixels", ctypes.POINTER(ctypes.c_uint8)), ("width", ctypes.c_uint32), ("height", ctypes.c_uint32)]


def build(optimization):
    """Compile the game and the drawing routines into a shared library at -O0 or -O2."""
    HOST_DIR.mkdir(parents=True, exist_ok=True)
    library = HOST_DIR / f"librv32pong-{optimization}.dylib"
    # -fPIC: a shared library must be position independent; Mach-O always is, ELF only when asked.
    command = [os.environ.get("HOST_CC", "cc"), "-shared", "-fPIC", f"-{optimization}", "-std=c11", "-fno-builtin",
               "-Wall", "-Wextra", "-Werror", f"-I{ROOT / 'programs/rv32'}", "-o", str(library), *map(str, SOURCES)]
    subprocess.run(command, check=True)
    lib = ctypes.CDLL(str(library))
    void, u32, i32, u8, ptr = None, ctypes.c_uint32, ctypes.c_int32, ctypes.c_uint8, ctypes.c_void_p
    surface = ctypes.POINTER(Surface)
    for name, restype, argtypes in (
            ("pong_state_size", u32, []), ("pong_init", void, [ptr]), ("pong_event", void, [ptr, u32]),
            ("pong_frame", void, [ptr, u32]), ("pong_draw", void, [ptr, surface]), ("pong_checksum", u32, [ptr]),
            ("pong_quit", u32, [ptr]), ("pong_phase_of", u32, [ptr]), ("pong_score", u32, [ptr, u32]),
            ("pong_ball_x", i32, [ptr]), ("pong_ball_y", i32, [ptr]), ("pong_ball_vx", i32, [ptr]), ("pong_ball_vy", i32, [ptr]),
            ("pong_left_y", i32, [ptr]), ("pong_right_y", i32, [ptr]), ("pong_set_ball", void, [ptr, i32, i32, i32, i32]),
            ("gfx_clear", void, [surface, u8]), ("gfx_fill_rect", void, [surface, i32, i32, i32, i32, u8]),
            ("gfx_draw_digit", void, [surface, i32, i32, u32, u32, u8]), ("gfx_draw_number", void, [surface, i32, i32, u32, u32, u8]),
            ("gfx_glyph_row", u8, [u32, u32])):
        function = getattr(lib, name)
        function.restype = restype
        function.argtypes = argtypes
    return lib


def make_surface(width=WIDTH, height=HEIGHT):
    """A zeroed pixel buffer and the surface struct that points at it; keep both alive together."""
    buffer = (ctypes.c_uint8 * (width * height))()
    return buffer, Surface(ctypes.cast(buffer, ctypes.POINTER(ctypes.c_uint8)), width, height)


class Pong:
    """One game on one library: the state buffer, the surface, and the guest's reads of them."""

    def __init__(self, lib):
        self.lib = lib
        self.state = ctypes.create_string_buffer(lib.pong_state_size())
        self.buffer, self.surface = make_surface()
        lib.pong_init(self.state)

    def event(self, word):
        self.lib.pong_event(self.state, word)

    def frame(self, keys):
        self.lib.pong_frame(self.state, keys)

    def draw(self):
        self.lib.pong_draw(self.state, ctypes.byref(self.surface))

    def pixels(self):
        return bytes(self.buffer)

    def checksum(self):
        return self.lib.pong_checksum(self.state)

    @property
    def quit(self):
        return bool(self.lib.pong_quit(self.state))

    @property
    def phase(self):
        return PHASES[self.lib.pong_phase_of(self.state)]

    @property
    def scores(self):
        return self.lib.pong_score(self.state, 0), self.lib.pong_score(self.state, 1)

    @property
    def ball(self):
        return self.lib.pong_ball_x(self.state), self.lib.pong_ball_y(self.state)

    @property
    def velocity(self):
        return self.lib.pong_ball_vx(self.state), self.lib.pong_ball_vy(self.state)

    @property
    def paddles(self):
        return self.lib.pong_left_y(self.state), self.lib.pong_right_y(self.state)

    def set_ball(self, x, y, vx, vy):
        self.lib.pong_set_ball(self.state, x, y, vx, vy)


def run_script(lib, events, max_frames=100000, trace=None):
    """Run the guest loop of pong.c on `events` ([(frame, word)] from parse_input_script) and return
    (checkpoint lines, checksum, frames). The input device is modelled as the backends do: an event
    arrives when its frame is presented (frame 0 before the first iteration), KEYS follows arrivals,
    and a queue of more than 16 events is an error here rather than a drop, because a script that
    overflows the queue cannot be replayed exactly. Stops with ValueError if nothing quits."""
    game = Pong(lib)
    queue, keys, checkpoints = [], 0, []
    pending = list(events)  # parse_input_script keeps frames non-decreasing
    next_event = 0
    if len(game.buffer) != FB_SIZE:
        raise ValueError(f"the surface is {len(game.buffer)} bytes, not a {FB_SIZE}-byte frame")

    def deliver(frame):
        nonlocal next_event, keys
        while next_event < len(pending) and pending[next_event][0] <= frame:
            word = pending[next_event][1]
            if len(queue) == QUEUE_SIZE:
                raise ValueError(f"the input queue would overflow at frame {frame}")
            queue.append(word)
            bit = 1 << (word & 31)
            keys = (keys | bit) if word & EVENT_PRESS else (keys & ~bit)
            next_event += 1

    deliver(0)
    for iteration in range(1, max_frames + 1):
        while queue:
            game.event(queue.pop(0))
        if game.quit:
            if next_event < len(pending):
                raise ValueError(f"{len(pending) - next_event} event(s) come after the quit")
            return checkpoints, game.checksum(), iteration - 1
        game.frame(keys)
        game.draw()
        checkpoints.append(f"frame {iteration} {frame_hash(game.pixels()):08x}")
        if trace is not None:
            trace.append((iteration, game.phase, game.scores, game.ball, game.velocity, game.paddles))
        deliver(iteration)
    raise ValueError(f"no quit within {max_frames} frames")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--input", type=Path, default=INPUT, help="the script to run")
    parser.add_argument("--write", action="store_true", help=f"regenerate {EXPECTED.relative_to(ROOT)}")
    parser.add_argument("--trace", action="store_true", help="print phase, scores, ball, velocity, paddles per frame")
    parser.add_argument("-O", default="O2", choices=("O0", "O2"))
    args = parser.parse_args()
    lib = build(args.O)
    trace = [] if args.trace else None
    try:
        checkpoints, checksum, frames = run_script(lib, parse_input_script(args.input.read_text()), trace=trace)
    except ValueError as error:
        sys.exit(f"{args.input}: {error}")
    if trace is not None:
        for frame, phase, scores, ball, velocity, paddles in trace:
            print(f"frame {frame:4d} {phase:6s} {scores[0]}-{scores[1]} ball {ball} v {velocity} paddles {paddles}")
    if args.write:
        EXPECTED.write_text("".join(f"{line}\n" for line in checkpoints))
        print(f"wrote {len(checkpoints)} checkpoint(s) to {EXPECTED.relative_to(ROOT)}")
    print(f"{frames} frames, PASS {checksum:08x}")


if __name__ == "__main__":
    main()
