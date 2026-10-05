#!/usr/bin/env python3
"""Check our memory map against QEMU's virt board (Track 1, docs/rv32-platform.md).

Reads virt's own device tree (dumped by QEMU with `-M virt,dumpdtb=FILE`, or
given with --dtb) and our machine's tree from tools/rv32_dtb.py, then checks:

- every device the two machines share sits at the same address: the done
  register inside virt's `sifive,test0`, the console at its `ns16550a`, the
  CLINT window equal to virt's `riscv,clint0`, the PLIC's (O1) equal to its
  `riscv,plic0`, and memory at the same base with the same size;
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

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # run as a script, or imported as tools.rv32_virt_map
from tools import rv32_dtb  # noqa: E402

# The nodes our tree shares with virt: our node's name, virt's compatible string, and how our
# window must relate to virt's: "equal" to one of its windows, or "inside" one.
SHARED = (("test", "sifive,test0", "inside"), ("console", "ns16550a", "inside"), ("clint", "riscv,clint0", "equal"),
          ("plic", "riscv,plic0", "equal"), ("virtio_mmio", "virtio,mmio", "inside"))


def dump_virt(qemu: str, memory: int = rv32_dtb.RAM_SIZE) -> bytes:
    """virt's own tree, for a machine with the contract's RAM (16 MiB unless the contract changes)."""
    with tempfile.TemporaryDirectory() as directory:
        path = Path(directory) / "virt.dtb"
        try:
            subprocess.run([qemu, "-M", f"virt,dumpdtb={path}", "-bios", "none", "-m", f"{memory // 1024}K",
                            "-nographic", "-monitor", "none"], check=True, capture_output=True, text=True, timeout=60)
        except subprocess.CalledProcessError as error:
            raise RuntimeError(f"{qemu} could not dump virt's tree (status {error.returncode}):\n{error.stderr}") from error
        return path.read_bytes()


def paths_with(root: rv32_dtb.Node, compatible: str) -> set[str]:
    """The paths of the nodes whose compatible list names `compatible`, spelled as regions() spells them."""
    paths = set()

    def visit(n: rv32_dtb.Node, path: str) -> None:
        if compatible in rv32_dtb.strings_of(n.props.get("compatible", b"")):
            paths.add(path)
        for c in n.children:
            visit(c, path.rstrip("/") + "/" + c.name)

    visit(root, "/")
    return paths


def check(virt: rv32_dtb.Node, ours: rv32_dtb.Node) -> list[str]:
    """The problems found; empty when the maps agree. Every shared node and memory must be in our
    tree: a tree that lost one would otherwise pass for want of anything to compare."""
    problems = []
    virt_regions = rv32_dtb.regions(virt)
    virt_memory = [(b, s) for p, b, s in virt_regions if p.startswith("/memory@")]
    shared = {name: (compatible, mode) for name, compatible, mode in SHARED}
    seen = set()
    for path, base, size in rv32_dtb.regions(ours):
        leaf = path.rsplit("/", 1)[-1].split("@")[0]
        if leaf == "memory":
            seen.add(leaf)
            if (base, size) not in virt_memory:
                problems.append(f"memory {base:#x}+{size:#x} differs from virt's")
            continue
        if leaf in shared:
            seen.add(leaf)
            compatible, mode = shared[leaf]
            theirs = [(b, s) for p, b, s in virt_regions if p in paths_with(virt, compatible)]
            if mode == "equal":
                ok = (base, size) in theirs
            else:
                ok = any(b <= base and base + size <= b + s for b, s in theirs)
            if not ok:
                problems.append(f"{path} {base:#x}+{size:#x} is not {'equal to' if mode == 'equal' else 'inside'} "
                                f"virt's {compatible}")
            continue
        for vpath, vbase, vsize in virt_regions:
            if base < vbase + vsize and vbase < base + size:
                problems.append(f"{path} {base:#x}+{size:#x} overlaps virt's {vpath} {vbase:#x}+{vsize:#x}")
    for name in ["memory", *shared]:
        if name not in seen:
            problems.append(f"our tree has no {name} node")
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
    print(f"rv32_virt_map: {len(rv32_dtb.regions(ours))} windows checked against virt's "
          f"{len(rv32_dtb.regions(virt))} regions: "
          "shared devices agree, the rest are disjoint")
    return 0


if __name__ == "__main__":
    sys.exit(main())
