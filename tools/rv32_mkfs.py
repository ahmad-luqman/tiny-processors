#!/usr/bin/env python3
"""Make, list and read the kernel's file system on a disk image (Track 2, O3; docs/rv32-os.md).

The disk is 128 KiB of 512-byte sectors, the size of the RTL's virtio-blk memory and
the only size the emulator accepts. The file system, "tfs", is as small as a file
system can be and still be one:

    sector 0   superblock: "TFS1" (0x31534654), sectors, directory sector, first data sector,
               entries, then zeros
    sector 1   directory: 16 entries of 32 bytes: name[20] (NUL-padded, empty = free),
               first sector, capacity in sectors, size in bytes
    sector 2.. file data: each file one contiguous extent, allocated when it is created,
               after the last extent in use

Every number is a little-endian 32-bit word. A file is created with a fixed
capacity (8 sectors, 4 KiB, unless given) and never grows beyond it; writing
it replaces its contents from the start. programs/rv32/os/fs.c is the kernel's side.

    python3 tools/rv32_mkfs.py disk.img --add welcome=programs/rv32/os/welcome.txt
    python3 tools/rv32_mkfs.py disk.img --list
    python3 tools/rv32_mkfs.py disk.img --cat scores
"""
from __future__ import annotations

import argparse
import struct
import sys
from pathlib import Path

SECTOR = 512
DISK_SIZE = 0x20000
SECTORS = DISK_SIZE // SECTOR
MAGIC = 0x31534654
DIRECTORY, DATA = 1, 2
ENTRIES = 16
ENTRY = struct.Struct("<20s3I")
DEFAULT_CAPACITY = 8


class FsError(ValueError):
    """A disk that is not a tfs file system, or a request it cannot hold."""


def blank() -> bytearray:
    disk = bytearray(DISK_SIZE)
    struct.pack_into("<5I", disk, 0, MAGIC, SECTORS, DIRECTORY, DATA, ENTRIES)
    return disk


def check(disk: bytes) -> None:
    if len(disk) != DISK_SIZE:
        raise FsError(f"a disk is {DISK_SIZE} bytes, not {len(disk)}")
    if struct.unpack_from("<5I", disk, 0) != (MAGIC, SECTORS, DIRECTORY, DATA, ENTRIES):
        raise FsError("no tfs superblock")


def entries(disk: bytes) -> list[tuple[str, int, int, int]]:
    """(name, first sector, capacity, size) of every file, in directory order."""
    check(disk)
    out = []
    for i in range(ENTRIES):
        name, first, capacity, size = ENTRY.unpack_from(disk, DIRECTORY * SECTOR + ENTRY.size * i)
        try:
            name = name.rstrip(b"\0").decode()
        except UnicodeDecodeError:
            raise FsError(f"directory entry {i}: the name {name!r} is not UTF-8") from None
        if name:
            if first < DATA or first + capacity > SECTORS or size > capacity * SECTOR:
                raise FsError(f"{name}: extent {first}+{capacity} or size {size} does not fit the disk")
            out.append((name, first, capacity, size))
    return out


def read(disk: bytes, name: str) -> bytes:
    for entry, first, _, size in entries(disk):
        if entry == name:
            return bytes(disk[first * SECTOR:first * SECTOR + size])
    raise FsError(f"no file {name}")


def add(disk: bytearray, name: str, data: bytes, capacity: int = DEFAULT_CAPACITY) -> None:
    if not 0 < len(name.encode()) < 20:
        raise FsError(f"{name!r}: a name is 1 to 19 bytes")
    files = entries(disk)
    if any(entry == name for entry, *_ in files):
        raise FsError(f"{name} exists")
    capacity = max(capacity, -(-len(data) // SECTOR))
    first = max([DATA] + [f + c for _, f, c, _ in files])
    if first + capacity > SECTORS:
        raise FsError(f"{name}: no room for {capacity} sectors")
    for i in range(ENTRIES):
        at = DIRECTORY * SECTOR + ENTRY.size * i
        if not disk[at]:
            ENTRY.pack_into(disk, at, name.encode(), first, capacity, len(data))
            disk[first * SECTOR:first * SECTOR + len(data)] = data
            return
    raise FsError("the directory is full")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("disk", type=Path)
    parser.add_argument("--new", action="store_true", help="start from an empty file system")
    parser.add_argument("--add", action="append", default=[], metavar="NAME=FILE", help="add a file (repeatable)")
    parser.add_argument("--list", action="store_true", help="list the files")
    parser.add_argument("--cat", metavar="NAME", help="print a file's contents")
    args = parser.parse_args(argv)
    try:
        disk = blank() if args.new else bytearray(args.disk.read_bytes())
        for item in args.add:
            name, _, path = item.partition("=")
            add(disk, name, Path(path).read_bytes())
        if args.new or args.add:
            args.disk.parent.mkdir(parents=True, exist_ok=True)
            args.disk.write_bytes(disk)
        if args.list:
            for name, first, capacity, size in entries(disk):
                print(f"{name:<19} {size:>6} bytes, sectors {first}..{first + capacity - 1}")
        if args.cat:
            sys.stdout.write(read(disk, args.cat).decode("utf-8", errors="backslashreplace"))
    except (FsError, OSError) as error:
        print(f"rv32_mkfs: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
