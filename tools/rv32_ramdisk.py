#!/usr/bin/env python3
"""Pack programs into the kernel's RAM disk (Track 2, O2; docs/rv32-os.md).

Each program is an ELF linked for one or more 128 KiB slots above the kernel
(programs/rv32/os/user.ld, `--defsym SLOT_BASE=... --defsym SLOT_SPAN=...`);
its span is its `_stack_top` less its load address. The RAM disk the kernel
bundles is a 16-byte header and a table of 52-byte entries, then the programs'
bytes, all little-endian:

    header   "RDSK" (0x4b534452), count, 0, 0
    entry    name[24] (NUL-padded), load, entry, file_size, memory_size, offset, flags, span
    data     each program's flattened load segment, 4-byte aligned

`load` is the slot base, `entry` the program's _start (the slot base too),
`memory_size` includes .bss, and `offset` counts from the start of the RAM
disk. `flags` bit 0 (ACCELERATORS) says the program drives SIMD4, G1 and G2
itself: the kernel then waits for the engines before a present, and from O5
grants it their windows. Programs' spans must not overlap, and each must leave
its span's top 32 KiB to the stack.

    python3 tools/rv32_ramdisk.py --out build/rv32/os/ramdisk.img --accelerators menu build/rv32/os/sh.elf ...
    python3 tools/rv32_ramdisk.py --list build/rv32/os/ramdisk.img
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))
from tools.rv32_image import ImageError, flatten, parse_elf  # noqa: E402

MAGIC = 0x4B534452
HEADER = struct.Struct("<4I")
ENTRY = struct.Struct("<24s7I")
SLOT_BASE, SLOT_SIZE, SLOTS, STACK_SIZE = 0x80100000, 0x20000, 24, 0x8000
PT_LOAD = 1
ACCELERATORS = 1


class RamdiskError(ValueError):
    """A program that cannot go on the RAM disk, or a RAM disk that is malformed."""


def program(path: Path) -> tuple[str, int, int, bytes, int, int]:
    """(name, load, entry, bytes, memory size, span) of one program ELF, checked against the slot rules."""
    name = path.stem
    if not 0 < len(name.encode()) < 24:
        raise RamdiskError(f"{path}: a name must be 1 to 23 bytes")
    elf = parse_elf(path.read_bytes())
    loads = [s for s in elf.segments if s.type == PT_LOAD]
    if len(loads) != 1:
        raise RamdiskError(f"{path}: {len(loads)} load segments, expected 1")
    segment = loads[0]
    load = segment.vaddr
    if load < SLOT_BASE or (load - SLOT_BASE) % SLOT_SIZE or (load - SLOT_BASE) // SLOT_SIZE >= SLOTS:
        raise RamdiskError(f"{path}: loads at {load:#x}, not a slot base")
    if elf.entry != load:
        raise RamdiskError(f"{path}: entry {elf.entry:#x} is not the slot base {load:#x}")
    span = elf.symbols.get("_stack_top", load) - load
    if span <= 0 or span % SLOT_SIZE or load + span > SLOT_BASE + SLOTS * SLOT_SIZE:
        raise RamdiskError(f"{path}: a span of {span:#x} bytes is not whole slots inside the slot area")
    if segment.memsz > span - STACK_SIZE:
        raise RamdiskError(f"{path}: {segment.memsz} bytes leave no room for the stack")
    return name, load, elf.entry, flatten(elf, load), segment.memsz, span


def build(paths: list[Path], accelerators: frozenset[str] = frozenset()) -> bytes:
    programs = [program(p) for p in paths]
    names = [p[0] for p in programs]
    for index, (name, load, _, _, _, span) in enumerate(programs):
        if names.index(name) != index:
            raise RamdiskError(f"two programs are named {name}")
        for other, other_load, _, _, _, other_span in programs[:index]:
            if load < other_load + other_span and other_load < load + span:
                raise RamdiskError(f"{name} and {other} share slots at {max(load, other_load):#x}")
    offset = HEADER.size + ENTRY.size * len(programs)
    table, data = [], []
    unknown = set(accelerators) - set(names)
    if unknown:
        raise RamdiskError(f"--accelerators names no program: {', '.join(sorted(unknown))}")
    for name, load, entry, image, memory, span in programs:
        flags = ACCELERATORS if name in accelerators else 0
        table.append(ENTRY.pack(name.encode(), load, entry, len(image), memory, offset, flags, span))
        padded = image + bytes(-len(image) % 4)
        data.append(padded)
        offset += len(padded)
    return HEADER.pack(MAGIC, len(programs), 0, 0) + b"".join(table) + b"".join(data)


def parse(blob: bytes) -> list[dict]:
    """The entries of a RAM disk, each with its bytes; raises RamdiskError when malformed."""
    if len(blob) < HEADER.size:
        raise RamdiskError("shorter than the header")
    magic, count, _, _ = HEADER.unpack_from(blob)
    if magic != MAGIC or HEADER.size + ENTRY.size * count > len(blob):
        raise RamdiskError("bad magic or table past the end")
    entries = []
    for i in range(count):
        name, load, entry, size, memory, offset, flags, span = ENTRY.unpack_from(blob, HEADER.size + ENTRY.size * i)
        if offset + size > len(blob) or size > memory:
            raise RamdiskError(f"entry {i} lies outside the RAM disk")
        entries.append({"name": name.rstrip(b"\0").decode(), "load": load, "entry": entry, "size": size,
                        "memory": memory, "flags": flags, "span": span, "data": blob[offset:offset + size]})
    return entries


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("programs", nargs="*", type=Path)
    parser.add_argument("--out", type=Path, help="write the RAM disk here")
    parser.add_argument("--list", type=Path, help="print the entries of a RAM disk")
    parser.add_argument("--accelerators", action="append", default=[], metavar="NAME",
                        help="a program that drives the accelerators itself (repeatable)")
    args = parser.parse_args(argv)
    try:
        if args.out:
            blob = build(args.programs, frozenset(args.accelerators))
            args.out.parent.mkdir(parents=True, exist_ok=True)
            args.out.write_bytes(blob)
            print(f"rv32_ramdisk: {len(args.programs)} programs, {len(blob)} bytes")
        if args.list:
            for e in parse(args.list.read_bytes()):
                print(f"{e['name']:<12} slot {(e['load'] - SLOT_BASE) // SLOT_SIZE:>2}+{e['span'] // SLOT_SIZE} at {e['load']:#010x}, "
                      f"{e['size']} bytes, {e['memory']} in memory{', accelerators' if e['flags'] & ACCELERATORS else ''}")
    except (RamdiskError, ImageError, OSError) as error:
        print(f"rv32_ramdisk: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
