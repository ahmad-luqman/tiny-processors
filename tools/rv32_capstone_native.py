#!/usr/bin/env python3
"""Run the M7 C runtime natively; checkpoint records are regression data, not independent game oracles."""
import argparse
import ctypes
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_pong_native import Surface, make_surface, run_script  # noqa: E402
from tools.rv32_devices import parse_input_script  # noqa: E402

INPUT = ROOT / "programs/rv32/capstone.input"
EXPECTED = ROOT / "programs/rv32/capstone.expected"


def build(optimization="O2"):
    output = ROOT / f"build/rv32/host/librv32capstone-{optimization}.dylib"
    output.parent.mkdir(parents=True, exist_ok=True)
    sources = [ROOT / f"programs/rv32/{name}.c" for name in ("gpu_demo", "gpu_ref", "runtime", "tetris_game", "pong_game", "gfx", "gfx_text", "digit_ui", "digit_model")]
    sources.append(ROOT / "tests/rv32_capstone_native.c")
    subprocess.run([os.environ.get("HOST_CC", "cc"), "-shared", "-fPIC", f"-{optimization}", "-std=c11",
                    "-Wall", "-Wextra", "-Werror", "-fno-builtin", "-I" + str(ROOT / "programs/rv32"),
                    "-I" + str(ROOT / "build/rv32"),
                    "-o", str(output), *map(str, sources)], check=True)
    lib = ctypes.CDLL(str(output))
    for name, restype, args in (
        ("native_size", ctypes.c_uint32, []),
        ("runtime_init", None, [ctypes.c_void_p]),
        ("native_attach_digit", None, [ctypes.c_void_p]),
        ("runtime_event", None, [ctypes.c_void_p, ctypes.c_uint32]),
        ("runtime_frame", None, [ctypes.c_void_p, ctypes.c_uint32]),
        ("runtime_draw", None, [ctypes.c_void_p, ctypes.POINTER(Surface)]),
        *((name, ctypes.c_uint32, [ctypes.c_void_p]) for name in
          ("runtime_checksum", "native_quit", "native_screen", "native_tetris_score", "native_tetris_lines",
           "native_digit_runs", "native_digit_predicted", "native_digit_status")),
    ):
        fn = getattr(lib, name)
        fn.restype, fn.argtypes = restype, args
    return lib


class Capstone:
    def __init__(self, lib):
        self.lib = lib
        self.state = ctypes.create_string_buffer(lib.native_size())
        self.buffer, self.surface = make_surface()
        lib.runtime_init(self.state)
        # The runtime has no classifier of its own: the firmware installs one that
        # also runs the accelerator, and the native model installs the software
        # path alone, so these checkpoints depend only on the CPU result.
        lib.native_attach_digit(self.state)

    def event(self, word): self.lib.runtime_event(self.state, word)
    def frame(self, keys): self.lib.runtime_frame(self.state, keys)
    def draw(self): self.lib.runtime_draw(self.state, ctypes.byref(self.surface))
    def pixels(self): return bytes(self.buffer)
    def checksum(self): return self.lib.runtime_checksum(self.state)
    @property
    def quit(self): return bool(self.lib.native_quit(self.state))


def run_session(lib, events, max_frames=100000):
    return run_script(lib, events, max_frames=max_frames, game_factory=Capstone)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--input", type=Path, default=INPUT)
    parser.add_argument("--write", action="store_true")
    args = parser.parse_args()
    try:
        events = parse_input_script(args.input.read_text())
        checkpoints, checksum, frames = run_session(build(), events)
    except (OSError, ValueError) as error:
        sys.exit(f"{args.input}: {error}")
    if args.write:
        EXPECTED.write_text("\n".join(checkpoints) + "\n")
        print(f"wrote {len(checkpoints)} checkpoint(s) to {EXPECTED.relative_to(ROOT)}")
    print(f"{frames} frames, PASS {checksum:08x}")


if __name__ == "__main__":
    main()
