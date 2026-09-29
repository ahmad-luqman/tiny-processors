#!/usr/bin/env python3
"""Check our memory map against QEMU's virt board (Track 1, docs/rv32-platform.md).

Reads virt's own device tree (dumped by QEMU with `-M virt,dumpdtb=FILE`, or
given with --dtb) and our machine's tree from tools/rv32_dtb.py, then checks:

- every device the two machines share sits at the same address: the done
  register inside virt's `sifive,test0`, the console at its `ns16550a`, the
  CLINT window equal to virt's `riscv,clint0`, and memory at the same base
  with the same size;
- every other window of ours is disjoint from every `reg` and `ranges`
  entry virt describes, so an access to one of our devices on QEMU is an
  access fault rather than a read of flash, PCIe or a virtio slot.

The boot ROM at 0x0000_1000 is not in either tree: it plays the part of
virt's mask ROM at the same address and is reached only through a1.

    python3 tools/rv32_virt_map.py --qemu qemu-system-riscv32
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
import rv32_dtb  # noqa: E402

# Which of our nodes are shared with virt, and the compatible string virt uses for each.
SHARED = {"test": "sifive,test0", "console": "ns16550a", "clint": "riscv,clint0"}


def dump_virt(qemu: str, memory: str = "4M") -> bytes:
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "virt.dtb"
        subprocess.run([qemu, "-M", f"virt,dumpdtb={path}", "-bios", "none", "-m", memory,
                        "-nographic", "-monitor", "none"], check=True, capture_output=True, timeout=60)
        return path.read_bytes()


def find_compatible(root: rv32_dtb.Node, compatible: str) -> list[tuple[str, int, int]]:
    """Regions of the nodes whose compatible list names `compatible`."""
    paths = []

    def visit(n: rv32_dtb.Node, path: str) -> None:
        if compatible in rv32_dtb.strings_of(n.props.get("compatible", b"")):
            paths.append(path)
        for c in n.children:
            visit(c, path.rstrip("/") + "/" + c.name)

    visit(root, "/")
    return [r for r in rv32_dtb.regions(root) if r[0] in paths]


def check(virt: rv32_dtb.Node, ours: rv32_dtb.Node) -> list[str]:
    """The problems found; empty when the maps agree."""
    problems = []
    virt_regions = rv32_dtb.regions(virt)
    shared_paths = set()
    for path, base, size in rv32_dtb.regions(ours):
        leaf = path.rsplit("/", 1)[-1].split("@")[0]
        if leaf == "memory":
            shared_paths.add(path)
            if (base, size) not in [(b, s) for p, b, s in virt_regions if p.startswith("/memory@")]:
                problems.append(f"memory {base:#x}+{size:#x} differs from virt's")
            continue
        if leaf in SHARED:
            shared_paths.add(path)
            theirs = find_compatible(virt, SHARED[leaf])
            if leaf == "clint":
                ok = (base, size) in [(b, s) for _, b, s in theirs]
            else:
                ok = any(b <= base and base + size <= b + s for _, b, s in theirs)
            if not ok:
                problems.append(f"{path} {base:#x}+{size:#x} is not inside virt's {SHARED[leaf]}")
            continue
        for vpath, vbase, vsize in virt_regions:
            if base < vbase + vsize and vbase < base + size:
                problems.append(f"{path} {base:#x}+{size:#x} overlaps virt's {vpath} {vbase:#x}+{vsize:#x}")
    return problems


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    source = parser.add_mutually_exclusive_group()
    source.add_argument("--qemu", default="qemu-system-riscv32", help="QEMU binary to dump virt's tree with")
    source.add_argument("--dtb", type=Path, help="use this dumped virt tree instead of running QEMU")
    args = parser.parse_args(argv)
    blob = args.dtb.read_bytes() if args.dtb else dump_virt(args.qemu)
    virt = rv32_dtb.parse(blob)
    ours = rv32_dtb.parse(rv32_dtb.build(rv32_dtb.MACHINE))
    problems = check(virt, ours)
    for p in problems:
        print("rv32_virt_map: " + p, file=sys.stderr)
    if problems:
        return 1
    count = len(rv32_dtb.regions(ours))
    print(f"rv32_virt_map: {count} windows checked against virt's {len(rv32_dtb.regions(virt))} regions: "
          "shared devices agree, the rest are disjoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
