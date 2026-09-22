#!/usr/bin/env python3
"""Build the device-free digit C on the host and drive it through ctypes.

programs/rv32/digit_model.c never touches a device, so the same source that runs
on the guest compiles for the Mac, exactly as tools/rv32_pong_native.py does for
Pong. The tests run it at -O0 and -O2 and compare it with the standard-library
oracle, so a difference is the compiler's or the code's, not the harness's.
"""

import ctypes
import os
from pathlib import Path
import subprocess
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_digit_model import ensure_headers  # noqa: E402

SOURCES = (ROOT / 'programs/rv32/digit_model.c',)
GENERATED = ROOT / 'build/rv32'
HOST_DIR = ROOT / 'build/rv32/host'


def build(optimization):
    """Compile the model into a shared library at the requested optimization level."""
    ensure_headers(GENERATED)
    HOST_DIR.mkdir(parents=True, exist_ok=True)
    library = HOST_DIR / f'librv32digit-{optimization}.dylib'
    command = [os.environ.get('HOST_CC', 'cc'), '-shared', '-fPIC', f'-{optimization}',
               '-std=c11', '-fno-builtin', '-Wall', '-Wextra', '-Werror',
               '-DRV32_DIGIT_NATIVE', f'-I{ROOT / "programs/rv32"}', f'-I{GENERATED}',
               '-o', str(library), *map(str, SOURCES)]
    subprocess.run(command, check=True)
    library_handle = ctypes.CDLL(str(library))
    void, u32, i32 = None, ctypes.c_uint32, ctypes.c_int32
    bytes_pointer = ctypes.POINTER(ctypes.c_uint8)
    ints_pointer = ctypes.POINTER(ctypes.c_int32)
    for name, restype, argtypes in (
            ('digit_prepare', void, [bytes_pointer, bytes_pointer]),
            ('digit_requantize', void, [ctypes.POINTER(ctypes.c_uint32), bytes_pointer]),
            ('digit_infer_cpu', void, [bytes_pointer, ints_pointer]),
            ('digit_argmax', u32, [ints_pointer, ctypes.POINTER(ctypes.c_uint32)]),
            ('digit_native_inputs', u32, []), ('digit_native_pixels', u32, []),
            ('digit_native_classes', u32, []), ('digit_native_hidden', u32, []),
            ('digit_native_shift', u32, []), ('digit_native_hidden_max', u32, []),
            ('digit_native_hidden_vector', void, [bytes_pointer, bytes_pointer])):
        function = getattr(library_handle, name)
        function.restype = restype
        function.argtypes = argtypes
    return library_handle


class Model:
    """A small wrapper so tests read like the guest code rather than like ctypes."""

    def __init__(self, library):
        self.library = library
        self.pixels = library.digit_native_pixels()
        self.inputs = library.digit_native_inputs()
        self.classes = library.digit_native_classes()
        self.hidden_size = library.digit_native_hidden()

    def prepare(self, canvas):
        source = (ctypes.c_uint8 * self.pixels)(*canvas)
        out = (ctypes.c_uint8 * self.inputs)()
        self.library.digit_prepare(source, out)
        return bytes(out)

    def hidden(self, x):
        source = (ctypes.c_uint8 * self.inputs)(*x)
        out = (ctypes.c_uint8 * self.hidden_size)()
        self.library.digit_native_hidden_vector(source, out)
        return list(out)

    def infer(self, x):
        source = (ctypes.c_uint8 * self.inputs)(*x)
        out = (ctypes.c_int32 * self.classes)()
        self.library.digit_infer_cpu(source, out)
        return list(out)

    def argmax(self, logits):
        values = (ctypes.c_int32 * self.classes)(*logits)
        margin = ctypes.c_uint32()
        best = self.library.digit_argmax(values, ctypes.byref(margin))
        return best, margin.value
