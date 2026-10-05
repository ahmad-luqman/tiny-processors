#!/usr/bin/env python3
"""The RV32 machine's device tree (docs/rv32.md, "Boot convention"; Track 1).

At reset a0 holds the hart id (0) and a1 the address of a flattened device
tree (FDT), as on QEMU's virt board. On our backends that tree is MACHINE
below, written as a blob into the boot ROM at 0x0000_1000. This module is the
one source of it: it writes the blob, the C array the emulator compiles in
(tools/rv32_dtb.h) and the ROM module the RTL synthesizes
(rtl/rv32/rv32_bootrom.v). `--check` fails when either committed copy is
stale. It also parses any FDT, which tools/rv32_virt_map.py uses to read
QEMU's own tree.

The format is the Devicetree Specification's flattened form, version 17:
a 40-byte header, an empty memory-reservation block, the structure block
(tokens BEGIN_NODE, PROP, END_NODE, END, all big-endian 32-bit), and the
strings block holding property names. Standard library only.

    python3 tools/rv32_dtb.py --dtb build/rv32/machine.dtb   # the blob
    python3 tools/rv32_dtb.py --write                        # refresh the committed copies
    python3 tools/rv32_dtb.py --check                        # fail if they are stale
    python3 tools/rv32_dtb.py --dts                          # print the tree as text
"""
from __future__ import annotations

import argparse
import struct
import sys
from dataclasses import dataclass, field
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))  # run as a script, or imported with tools/ on the path

from tools import rv32_asm  # noqa: E402  (the addresses; board.h, the bus and the emulator are pinned to it)

C_HEADER = ROOT / "tools" / "rv32_dtb.h"
VERILOG = ROOT / "rtl" / "rv32" / "rv32_bootrom.v"

MAGIC = 0xD00DFEED
VERSION, LAST_COMPATIBLE = 17, 16
BEGIN_NODE, END_NODE, PROP, NOP, END = 1, 2, 3, 4, 9

# The contract's addresses come from tools/rv32_asm.py, the Python copy that
# tests/test_rv32_tools.py pins to board.h, rv32emu_core.h and rv32_bus.v. The
# window sizes are this module's: the same test checks the bus decoder's widths
# and the emulator's region table against regions(MACHINE).
ROM_BASE, ROM_SIZE = rv32_asm.BOOTROM, 0x1000
RAM_BASE, RAM_SIZE = rv32_asm.RAM, rv32_asm.RAM_SIZE
DONE_BASE = rv32_asm.DONE
CONSOLE_BASE = rv32_asm.CONSOLE
CLINT_BASE, CLINT_SIZE = rv32_asm.CLINT, 0x1_0000
PLIC_BASE, PLIC_SIZE = rv32_asm.PLIC, rv32_asm.PLIC_SIZE
PLIC_SOURCES = 31                                   # riscv,ndev: sources 1..31
INPUT_IRQ = rv32_asm.PLIC_SOURCE_INPUT
VIRTIO_BASE, VIRTIO_SIZE, VIRTIO_IRQ = rv32_asm.VIRTIO, rv32_asm.VIRTIO_SIZE, rv32_asm.PLIC_SOURCE_VIRTIO
# Interrupt wiring (O1): phandles of the hart's local interrupt controller and of the PLIC, and the
# mip bit numbers each connection raises.
CPU_INTC, PLIC_PHANDLE, DONE_PHANDLE = 1, 2, 3  # DONE_PHANDLE: Linux's tree (issue #36)
IRQ_MSI, IRQ_MTI, IRQ_MEI = 3, 7, 11
INPUT_BASE = rv32_asm.INPUT
DISPLAY_BASE = rv32_asm.DISPLAY
PALETTE_BASE, PALETTE_SIZE = rv32_asm.PALETTE, rv32_asm.PALETTE_SIZE
SIMD4_BASE, SIMD4_PROGRAM, SIMD4_DATA = rv32_asm.SIMD_BASE, rv32_asm.SIMD_PROGRAM, rv32_asm.SIMD_DATA
GPU_BASE = rv32_asm.GPU_BASE
G3D_BASE, G3D_SIZE = rv32_asm.G3D_BASE, rv32_asm.G3D_SIZE
DMA_WINDOW_BASE = rv32_asm.DMA_WINDOW
FB_BASE, FB_SIZE = rv32_asm.FB, rv32_asm.FB_SIZE


@dataclass
class Node:
    """One node. Mutable so tests can derive variants, which they do on a private copy
    (`parse(build(MACHINE))`); MACHINE itself must never be edited."""

    name: str
    props: dict[str, bytes] = field(default_factory=dict)
    children: list["Node"] = field(default_factory=list)

    def child(self, name: str) -> "Node | None":
        return next((c for c in self.children if c.name == name), None)


def u32(*values: int) -> bytes:
    return b"".join(struct.pack(">I", v) for v in values)


def string(*values: str) -> bytes:
    return b"".join(v.encode() + b"\0" for v in values)


def node(name: str, props: dict[str, bytes], *children: Node) -> Node:
    return Node(name, dict(props), list(children))


def device(name: str, base: int, compatible: tuple[str, ...], regs: tuple[tuple[int, int], ...],
           extra: dict[str, bytes] | None = None) -> Node:
    return node(f"{name}@{base:x}", {"compatible": string(*compatible),
                                     "reg": b"".join(u32(a, s) for a, s in regs), **(extra or {})})


# Root and soc use one address cell and one size cell: every address is 32-bit.
# Compatible strings name our own device first. A generic name follows only
# where our device implements everything a driver for it may touch: the done
# register is a sifive,test0 without the reset word; the console was a 16550's
# transmit and line-status registers only, so a 16550 driver's initialization
# would fault and "ns16550a" is not claimed. Issue #36 gave the console the
# rest of the subset Linux's 8250 driver drives, but this tree's bytes stay as
# they were (platcheck and irqcheck hash them); Linux compiles in its own tree,
# and the "a" missing from riscv,isa waits for a change that may move those
# words. Since O1 the hart has a
# riscv,cpu-intc; the CLINT raises its software and timer interrupts, the PLIC
# (a full SiFive PLIC for one context, so it claims the generic names) its
# external interrupt, and the input queue is PLIC source 12. Since O3 a
# virtio-mmio block device sits in virt's first virtio slot, PLIC source 1.
MACHINE = node("", {
    "#address-cells": u32(1), "#size-cells": u32(1),
    "compatible": string("tiny-processors,rv32-machine"),
    "model": string("tiny-processors RV32 machine"),
},
    node("chosen", {"stdout-path": string(f"/soc/console@{CONSOLE_BASE:x}")}),
    node("cpus", {"#address-cells": u32(1), "#size-cells": u32(0)},
         node("cpu@0", {"device_type": string("cpu"), "reg": u32(0), "compatible": string("riscv"),
                        "riscv,isa": string("rv32imf_zicsr_zicntr"), "status": string("okay")},
              node("interrupt-controller", {"#interrupt-cells": u32(1), "interrupt-controller": b"",
                                            "compatible": string("riscv,cpu-intc"), "phandle": u32(CPU_INTC)}))),
    node(f"memory@{RAM_BASE:x}", {"device_type": string("memory"), "reg": u32(RAM_BASE, RAM_SIZE)}),
    node("soc", {"#address-cells": u32(1), "#size-cells": u32(1),
                 "compatible": string("simple-bus"), "ranges": b""},
         device("test", DONE_BASE, ("tiny-processors,done", "sifive,test0"), ((DONE_BASE, 4),)),
         device("clint", CLINT_BASE, ("tiny-processors,clint", "riscv,clint0"), ((CLINT_BASE, CLINT_SIZE),),
                {"interrupts-extended": u32(CPU_INTC, IRQ_MSI, CPU_INTC, IRQ_MTI)}),
         device("plic", PLIC_BASE, ("tiny-processors,plic", "sifive,plic-1.0.0", "riscv,plic0"), ((PLIC_BASE, PLIC_SIZE),),
                {"#address-cells": u32(0), "#interrupt-cells": u32(1), "interrupt-controller": b"",
                 "interrupts-extended": u32(CPU_INTC, IRQ_MEI), "riscv,ndev": u32(PLIC_SOURCES),
                 "phandle": u32(PLIC_PHANDLE)}),
         node(f"virtio_mmio@{VIRTIO_BASE:x}", {"compatible": string("virtio,mmio"),
                                               "reg": u32(VIRTIO_BASE, VIRTIO_SIZE),
                                               "interrupt-parent": u32(PLIC_PHANDLE), "interrupts": u32(VIRTIO_IRQ)}),
         device("console", CONSOLE_BASE, ("tiny-processors,console",), ((CONSOLE_BASE, 8),)),
         device("input", INPUT_BASE, ("tiny-processors,input",), ((INPUT_BASE, 16),),
                {"interrupt-parent": u32(PLIC_PHANDLE), "interrupts": u32(INPUT_IRQ)}),
         device("display", DISPLAY_BASE, ("tiny-processors,display",), ((DISPLAY_BASE, 16), (FB_BASE, FB_SIZE), (PALETTE_BASE, PALETTE_SIZE))),
         device("simd4", SIMD4_BASE, ("tiny-processors,simd4",),
                ((SIMD4_BASE, 32), (SIMD4_PROGRAM, 1024), (SIMD4_DATA, 1024))),
         device("gpu", GPU_BASE, ("tiny-processors,g1",), ((GPU_BASE, 128),)),
         device("g3d", G3D_BASE, ("tiny-processors,g2",), ((G3D_BASE, G3D_SIZE),)),
         device("dma-window", DMA_WINDOW_BASE, ("tiny-processors,dma-window",), ((DMA_WINDOW_BASE, 8),))),
)


# Linux's tree (issue #36, docs/rv32-linux.md). The kernel compiles it in (CONFIG_BUILTIN_DTB) and
# ignores a1, so one image boots with the same tree on QEMU virt, the emulator and the RTL, and the
# boot ROM's MACHINE stays byte-identical. It names only what Linux drives and every backend has at
# the same address: RAM, the CLINT (the timer), the console as an ns16550a, polled because it has
# no `interrupts`, and the done register as a syscon that syscon-poweroff writes 0x5555 to. The
# timebase is QEMU's CLINT rate; on our backends a tick is an instruction (the emulator, and the
# RTL's step ticks), so it only sets how fast guest time runs. riscv,isa-base and
# riscv,isa-extensions are what Linux reads (its fallback to riscv,isa is configured out).
LINUX_TIMEBASE = 10_000_000
LINUX_UART_CLOCK = 3_686_400   # a common 16550 crystal; the divisor it implies sets no rate here
LINUX_ISA_EXTENSIONS = ("i", "m", "a", "zicsr", "zicntr", "zifencei")
# riscv,isa spells the same list: the single letters after "rv32", then the rest joined by "_".
LINUX_ISA = "rv32" + "".join(e for e in LINUX_ISA_EXTENSIONS if len(e) == 1) + \
    "".join("_" + e for e in LINUX_ISA_EXTENSIONS if len(e) > 1)
LINUX_POWEROFF_WORD = 0x5555  # the done register's pass word (docs/rv32.md, "Done register")
LINUX = node("", {
    "#address-cells": u32(1), "#size-cells": u32(1),
    "compatible": string("tiny-processors,rv32-machine"),
    "model": string("tiny-processors RV32 machine"),
},
    node("chosen", {"stdout-path": string(f"/soc/serial@{CONSOLE_BASE:x}")}),
    node("cpus", {"#address-cells": u32(1), "#size-cells": u32(0), "timebase-frequency": u32(LINUX_TIMEBASE)},
         node("cpu@0", {"device_type": string("cpu"), "reg": u32(0), "compatible": string("riscv"),
                        "riscv,isa": string(LINUX_ISA),
                        "riscv,isa-base": string("rv32i"),
                        "riscv,isa-extensions": string(*LINUX_ISA_EXTENSIONS), "status": string("okay")},
              node("interrupt-controller", {"#interrupt-cells": u32(1), "interrupt-controller": b"",
                                            "compatible": string("riscv,cpu-intc"), "phandle": u32(CPU_INTC)}))),
    node(f"memory@{RAM_BASE:x}", {"device_type": string("memory"), "reg": u32(RAM_BASE, RAM_SIZE)}),
    node("poweroff", {"compatible": string("syscon-poweroff"), "regmap": u32(DONE_PHANDLE),
                      "offset": u32(0), "value": u32(LINUX_POWEROFF_WORD)}),
    node("soc", {"#address-cells": u32(1), "#size-cells": u32(1),
                 "compatible": string("simple-bus"), "ranges": b""},
         device("test", DONE_BASE, ("sifive,test0", "syscon"), ((DONE_BASE, 4),),
                {"phandle": u32(DONE_PHANDLE)}),
         device("clint", CLINT_BASE, ("riscv,clint0",), ((CLINT_BASE, CLINT_SIZE),),
                {"interrupts-extended": u32(CPU_INTC, IRQ_MSI, CPU_INTC, IRQ_MTI)}),
         device("serial", CONSOLE_BASE, ("ns16550a",), ((CONSOLE_BASE, 8),),
                {"clock-frequency": u32(LINUX_UART_CLOCK)})),
)

def build(root: Node) -> bytes:
    """Flatten a tree. Property names are stored once each, in first-use order."""
    strings = bytearray()
    offsets: dict[str, int] = {}
    body = bytearray()

    def emit(n: Node) -> None:
        name = n.name.encode() + b"\0"
        body.extend(u32(BEGIN_NODE) + name + b"\0" * (-len(name) % 4))
        for key, value in n.props.items():
            if key not in offsets:
                offsets[key] = len(strings)
                strings.extend(key.encode() + b"\0")
            body.extend(u32(PROP, len(value), offsets[key]) + value + b"\0" * (-len(value) % 4))
        for c in n.children:
            emit(c)
        body.extend(u32(END_NODE))

    emit(root)
    body.extend(u32(END))
    header_size, rsvmap_size = 40, 16
    off_struct = header_size + rsvmap_size
    off_strings = off_struct + len(body)
    total = off_strings + len(strings)
    total += -total % 4
    header = u32(MAGIC, total, off_struct, off_strings, header_size, VERSION, LAST_COMPATIBLE,
                 0, len(strings), len(body))
    blob = header + bytes(rsvmap_size) + bytes(body) + bytes(strings)
    return blob + bytes(total - len(blob))


def parse(blob: bytes) -> Node:
    """Parse a flattened tree; raises ValueError on anything malformed, including a block,
    node name, property value or property name that lies outside the blob."""
    if len(blob) < 40:
        raise ValueError("shorter than an FDT header")
    magic, total, off_struct, off_strings, _, version, last, _, size_strings, size_struct = \
        struct.unpack(">10I", blob[:40])
    if magic != MAGIC:
        raise ValueError(f"bad magic {magic:08x}")
    if last > VERSION or version < LAST_COMPATIBLE or total > len(blob):
        raise ValueError("unsupported version or truncated blob")
    if not (40 <= off_struct and off_struct % 4 == 0 and off_struct + size_struct <= total
            and 40 <= off_strings and off_strings + size_strings <= total):
        raise ValueError("a block lies outside the blob")
    strings = blob[off_strings:off_strings + size_strings]
    pos, end = off_struct, off_struct + size_struct
    stack: list[Node] = []
    root: Node | None = None

    def word() -> int:
        nonlocal pos
        if pos + 4 > end:
            raise ValueError("structure block ends early")
        (v,) = struct.unpack(">I", blob[pos:pos + 4])
        pos += 4
        return v

    while True:
        token = word()
        if token == BEGIN_NODE:
            stop = blob.find(b"\0", pos, end)
            if stop < 0:
                raise ValueError("node name runs past the structure block")
            n = Node(blob[pos:stop].decode())
            pos = (stop + 4) & ~3
            if stack:
                stack[-1].children.append(n)
            elif root is None:
                root = n
            else:
                raise ValueError("two root nodes")
            stack.append(n)
        elif token == PROP:
            length, name_off = word(), word()
            if not stack:
                raise ValueError("property outside a node")
            if pos + length > end:
                raise ValueError("property value runs past the structure block")
            stop = strings.find(b"\0", name_off)
            if name_off >= len(strings) or stop < 0:
                raise ValueError("property name lies outside the strings block")
            stack[-1].props[strings[name_off:stop].decode()] = bytes(blob[pos:pos + length])
            pos += (length + 3) & ~3
        elif token == END_NODE:
            if not stack:
                raise ValueError("unbalanced END_NODE")
            stack.pop()
        elif token == NOP:
            continue
        elif token == END:
            if stack or root is None:
                raise ValueError("END inside a node")
            return root
        else:
            raise ValueError(f"unknown token {token}")


def cells(value: bytes) -> list[int]:
    return [v for (v,) in struct.iter_unpack(">I", value)]


def strings_of(value: bytes) -> list[str]:
    return [s.decode() for s in value.split(b"\0")[:-1]]


def regions(root: Node) -> list[tuple[str, int, int]]:
    """Every (path, base, size) a `reg` or a non-empty `ranges` describes, with each
    node's address and size cells taken from its parent (defaults 2 and 1). A `ranges`
    entry contributes its parent-bus address and size. A reg under #size-cells = <0> (a cpu's)
    describes no address range and is skipped; a reg or ranges that is not a whole number of
    entries raises ValueError."""
    out: list[tuple[str, int, int]] = []

    def number(words: list[int]) -> int:
        v = 0
        for w in words:
            v = v << 32 | w
        return v

    def visit(n: Node, path: str, ac: int, sc: int) -> None:
        my_ac = cells(n.props["#address-cells"])[0] if "#address-cells" in n.props else 2
        my_sc = cells(n.props["#size-cells"])[0] if "#size-cells" in n.props else 1
        if "reg" in n.props and sc > 0:
            w = cells(n.props["reg"])
            if len(n.props["reg"]) % 4 or len(w) % (ac + sc):
                raise ValueError(f"{path}: reg is not a whole number of {ac}+{sc}-cell entries")
            for i in range(0, len(w), ac + sc):
                out.append((path, number(w[i:i + ac]), number(w[i + ac:i + ac + sc])))
        if n.props.get("ranges"):
            w = cells(n.props["ranges"])
            step = my_ac + ac + my_sc
            if len(n.props["ranges"]) % 4 or len(w) % step:
                raise ValueError(f"{path}: ranges is not a whole number of {step}-cell entries")
            for i in range(0, len(w), step):
                parent = number(w[i + my_ac:i + my_ac + ac])
                out.append((path + " ranges", parent, number(w[i + my_ac + ac:i + step])))
        for c in n.children:
            visit(c, (path.rstrip("/") + "/" + c.name), my_ac, my_sc)

    visit(root, "/", 2, 1)
    return out


def dts(n: Node, depth: int = 0) -> str:
    """A readable rendering (not full DTS syntax for every property type)."""
    pad = "    " * depth
    lines = [f"{pad}{n.name or '/'} {{"]
    for key, value in n.props.items():
        if value and value[-1:] == b"\0" and all(32 <= b < 127 or b == 0 for b in value):
            text = ", ".join(f'"{s}"' for s in strings_of(value))
        elif value and len(value) % 4 == 0:
            text = "<" + " ".join(f"0x{v:x}" for v in cells(value)) + ">"
        else:
            text = "[" + value.hex() + "]" if value else ""
        lines.append(f"{pad}    {key}{' = ' + text if text else ''};")
    for c in n.children:
        lines.append(dts(c, depth + 1))
    lines.append(f"{pad}}};")
    return "\n".join(lines)


def words(blob: bytes) -> list[int]:
    """The blob as the little-endian words the bus reads: byte A of the ROM is blob[A]."""
    padded = blob + bytes(-len(blob) % 4)
    return [int.from_bytes(padded[i:i + 4], "little") for i in range(0, len(padded), 4)]


GENERATED = "Generated by tools/rv32_dtb.py from its MACHINE tree; do not edit. `make check-rv32-dtb` fails when stale."


def c_header(blob: bytes) -> str:
    rows = [", ".join(f"0x{b:02x}" for b in blob[i:i + 12]) for i in range(0, len(blob), 12)]
    body = ",\n    ".join(rows)
    return (f"/* The machine's device tree blob, served by the boot ROM at 0x{ROM_BASE:08x}\n"
            f" * (docs/rv32.md, \"Boot convention\"). {GENERATED} */\n"
            "#ifndef RV32_DTB_H\n#define RV32_DTB_H\n\n"
            f"#define RV32_DTB_ROM_BASE 0x{ROM_BASE:08x}u\n#define RV32_DTB_ROM_SIZE 0x{ROM_SIZE:x}u\n"
            f"#define RV32_DTB_SIZE {len(blob)}u\n\n"
            f"static const unsigned char rv32_dtb[RV32_DTB_SIZE] = {{\n    {body}\n}};\n\n#endif\n")


def verilog(blob: bytes) -> str:
    # The case index is the word address inside the window: bits [top:2] of the byte address.
    assert ROM_SIZE & (ROM_SIZE - 1) == 0 and ROM_SIZE >= 8, "the ROM window must be a power of two"
    top = ROM_SIZE.bit_length() - 2
    width = top - 1
    cases = "\n".join(f"            {width}'d{i}: word = 32'h{w:08x};" for i, w in enumerate(words(blob)))
    return f"""`timescale 1ns/1ps

// Boot ROM (docs/rv32.md, "Boot convention"): the machine's device tree blob,
// {len(blob)} bytes, at 0x{ROM_BASE >> 16:04x}_{ROM_BASE & 0xffff:04x} in a {ROM_SIZE // 1024} KiB window. Reads of any width
// return the aligned word (the CPU selects the strobed lanes); bytes past the
// blob read 0; every write is refused, and the bus refuses fetches.
// {GENERATED}
module rv32_bootrom (
    input  wire        valid,
    input  wire        we,
    input  wire [31:0] addr,
    output wire [31:0] rdata,
    output wire        ready,
    output wire        error
);
    reg [31:0] word;
    always @* begin
        case (addr[{top}:2])
{cases}
            default: word = 32'd0;
        endcase
    end

    assign rdata = word;
    assign ready = valid;
    assign error = we;

    wire unused_ok = &{{1'b0, addr[31:{top + 1}], addr[1:0]}};
endmodule
"""


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--dtb", type=Path, help="write the blob here")
    parser.add_argument("--write", action="store_true", help="refresh tools/rv32_dtb.h and rtl/rv32/rv32_bootrom.v")
    parser.add_argument("--check", action="store_true", help="fail when the committed copies are stale")
    parser.add_argument("--dts", action="store_true", help="print the tree")
    parser.add_argument("--linux-dts", action="store_true", help="print Linux's tree")
    args = parser.parse_args(argv)
    blob = build(MACHINE)
    if len(blob) > ROM_SIZE:
        print(f"rv32_dtb: blob is {len(blob)} bytes, the ROM holds {ROM_SIZE}", file=sys.stderr)
        return 1
    if args.dtb:
        args.dtb.parent.mkdir(parents=True, exist_ok=True)
        args.dtb.write_bytes(blob)
    wanted = {C_HEADER: c_header(blob), VERILOG: verilog(blob)}
    if args.write:
        for path, text in wanted.items():
            path.write_text(text)
    if args.check:
        stale = [str(p.relative_to(ROOT)) for p, t in wanted.items() if not p.exists() or p.read_text() != t]
        if stale:
            print("rv32_dtb: stale, run `python3 tools/rv32_dtb.py --write`: " + ", ".join(stale), file=sys.stderr)
            return 1
        print(f"rv32_dtb: {len(blob)}-byte blob; {C_HEADER.name} and {VERILOG.name} are up to date")
    if args.dts:
        print(dts(parse(blob)))
    if args.linux_dts:
        print(dts(parse(build(LINUX))))
    return 0


if __name__ == "__main__":
    sys.exit(main())
