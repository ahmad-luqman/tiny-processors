#!/usr/bin/env python3
"""Check an RV32 firmware ELF against the machine contract and emit flat images.

The ELF is parsed here with the standard library so the check does not depend
on the cross toolchain and so the M2 emulator can reuse the loader. `--bin`
cross-checks our flattening against llvm-objcopy byte for byte.
"""

import argparse
from collections import namedtuple
from pathlib import Path
import re
import struct
import sys

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_asm import CYCLE, CYCLEH, INSTRET, INSTRETH, TIME, TIMEH  # noqa: E402

RAM_BASE = 0x80000000
RAM_SLICE_SIZE = 0x00040000
EM_RISCV = 243
ET_EXEC = 2
PT_LOAD = 1
SHT_NOBITS = 8
SHT_SYMTAB = 2
SHF_ALLOC = 0x2
EF_RISCV_RVC = 0x1
EF_RISCV_FLOAT_ABI = 0x6
EF_RISCV_RVE = 0x8
EF_RISCV_TSO = 0x10
REQUIRED_SECTIONS = (".text", ".rodata", ".data", ".bss")
# The kernel's page tables (issue #25): a section without file bytes (NOBITS) that startup does not
# clear, admitted only with page_tables=True, page-aligned, between the bounds symbols PMP uses and
# below the stack.
PAGE_TABLES_SECTION = ".pagetables"
PAGE_SIZE = 4096
REQUIRED_SYMBOLS = ("_start", "__bss_start", "__bss_end", "_end", "_stack_bottom", "_stack_top")
# Base RV32I only: no M, no CSRs, no compressed, no traps in the M1 slice.
FORBIDDEN_MNEMONIC = re.compile(r"\A(mul\w*|div\w*|rem\w*|csr\w*|fence\.i|c\.\w+|ecall|ebreak|wfi|[msu]ret|sfence\.vma)\Z")
# What an image with a trap handler may use in addition (the M5 diagnostic): the CSR instructions on the
# four trap CSRs (the operand is checked below), and mret.
PRIVILEGED_MNEMONIC = re.compile(r"\A(csr\w*|mret)\Z")
TRAP_CSR = re.compile(r"\b(mtvec|mepc|mcause|mtval)\b")
# What a system image may use in addition (Track 2): the interrupt CSRs, wfi and ecall (O1, O2), and
# the protection CSRs, mcounteren and PMP (O5); since issue #20 also S-mode and Sv32: delegation,
# the supervisor's CSRs, satp, sret and sfence.vma.
SYSTEM_CSR = re.compile(r"\b(mstatus|mie|mip|mscratch|mcounteren|scounteren|pmpcfg[01]|pmpaddr[0-7]|"
                        r"medeleg|mideleg|sstatus|sie|stvec|sscratch|sepc|scause|stval|sip|satp)\b")
SYSTEM_MNEMONIC = re.compile(r"\A(wfi|ecall|sret|sfence\.vma)\Z")
UNIMP = 0xC0001073  # `unimp`: csrrw x0, cycle, x0, illegal everywhere since cycle is read-only
# What an RV32IM image may use in addition (Track 0): exactly the eight M-extension instructions.
M_MNEMONIC = re.compile(r"\A(mul|mulh|mulhsu|mulhu|div|divu|rem|remu)\Z")
# The Zicntr counters (cycle, time, instret and their high halves). Only reads exist; objdump
# prints them as rdcycle/rdtime/rdinstret, which no csr* pattern would catch, so the check is
# on the instruction word.
COUNTER_CSRS = (CYCLE, TIME, INSTRET, CYCLEH, TIMEH, INSTRETH)
LISTING_LINE = re.compile(r"\A\s*([0-9a-f]+):\s+([0-9a-f]{2}(?: [0-9a-f]{2})*|[0-9a-f]{4,8})\s+(\S+)")

Elf = namedtuple("Elf", "etype machine flags entry segments sections symbols undefined")
Segment = namedtuple("Segment", "type vaddr paddr filesz memsz flags data")
Section = namedtuple("Section", "name type flags addr size")


class ImageError(ValueError):
    """The file is not a little-endian ELF32 object we can inspect."""


def cstring(blob, offset):
    end = blob.index(b"\0", offset)
    return blob[offset:end].decode("ascii", "replace")


def parse_elf(data):
    """Parse a little-endian ELF32 file into headers, sections, and symbols."""
    if data[:4] != b"\x7fELF":
        raise ImageError("not an ELF file")
    if data[4] != 1:
        raise ImageError("not ELFCLASS32 (32-bit)")
    if data[5] != 1:
        raise ImageError("not little-endian (ELFDATA2LSB)")
    (etype, machine, _, entry, phoff, shoff, flags, _, phentsize, phnum,
     shentsize, shnum, shstrndx) = struct.unpack_from("<HHIIIIIHHHHHH", data, 16)
    segments = []
    for index in range(phnum):
        p_type, offset, vaddr, paddr, filesz, memsz, p_flags, _ = struct.unpack_from(
            "<8I", data, phoff + index * phentsize)
        segments.append(Segment(p_type, vaddr, paddr, filesz, memsz, p_flags,
                                bytes(data[offset:offset + filesz])))
    raw_sections = [struct.unpack_from("<10I", data, shoff + index * shentsize) for index in range(shnum)]
    names = b""
    if shnum:
        strtab = raw_sections[shstrndx]
        names = data[strtab[4]:strtab[4] + strtab[5]]
    sections = []
    for sh_name, sh_type, sh_flags, sh_addr, _, sh_size, *_ in raw_sections:
        sections.append(Section(cstring(names, sh_name) if names else "", sh_type, sh_flags, sh_addr, sh_size))
    symbols, undefined = {}, []
    for index, raw in enumerate(raw_sections):
        if raw[1] != SHT_SYMTAB:
            continue
        strings = raw_sections[raw[6]]
        table = data[strings[4]:strings[4] + strings[5]]
        for position in range(0, raw[5], 16):
            st_name, st_value, _, _, _, st_shndx = struct.unpack_from("<IIIBBH", data, raw[4] + position)
            name = cstring(table, st_name)
            if not name:
                continue
            if st_shndx == 0:
                undefined.append(name)
            else:
                symbols[name] = st_value
    return Elf(etype, machine, flags, entry, segments, sections, symbols, undefined)


F_OPCODES = {0x07, 0x27, 0x43, 0x47, 0x4b, 0x4f, 0x53}


def valid_f_word(word):
    """Static RV32F encoding validation; dynamic frm is runtime state."""
    op, f3, f7, rs2 = word & 127, (word >> 12) & 7, word >> 25, (word >> 20) & 31
    rm_ok = f3 <= 4 or f3 == 7
    if op in (0x07, 0x27): return f3 == 2
    if op in (0x43, 0x47, 0x4b, 0x4f): return (f7 & 3) == 0 and rm_ok
    if op != 0x53: return False
    if f7 in (0x00, 0x04, 0x08, 0x0c): return rm_ok
    if f7 == 0x2c: return rs2 == 0 and rm_ok
    if f7 in (0x60, 0x68): return rs2 <= 1 and rm_ok
    if f7 in (0x10, 0x50): return f3 <= 2
    if f7 == 0x14: return f3 <= 1
    if f7 == 0x70: return rs2 == 0 and f3 <= 1
    if f7 == 0x78: return rs2 == 0 and f3 == 0
    return False


def listing_word(encoded):
    """LLVM prints either little-endian bytes or a single instruction word."""
    if " " in encoded:
        raw = bytes.fromhex(encoded)
        return int.from_bytes(raw, "little") if len(raw) == 4 else None
    return int(encoded, 16) if len(encoded) == 8 else None


def check_listing(text, allow_privileged=False, allow_f=False, allow_m=False, allow_counters=False, allow_system=False,
                  allow_user=False):
    """Return problems found in an objdump disassembly listing; `allow_privileged` admits the CSR
    instructions and mret that a trap handler needs; `allow_f` admits only valid RV32F
    encodings and floating CSR accesses; `allow_m` admits the M extension's eight instructions;
    `allow_counters` admits reads (never writes) of the Zicntr counters; `allow_system` (which implies
    `allow_privileged`) admits the interrupt CSRs, wfi and ecall of Track 2; `allow_user` (which implies
    `allow_counters`) admits what a user-mode program may run (O5): ecall and `unimp`, and no machine
    CSR, mret or wfi. Every gate requires a listing to inspect."""
    allow_privileged = allow_privileged or allow_system
    allow_counters = allow_counters or allow_user
    problems = []
    instructions = 0
    for number, line in enumerate(text.splitlines(), 1):
        if "<unknown>" in line:
            problems.append(f"listing line {number}: undecodable instruction: {line.strip()}")
            instructions += 1
            continue
        match = LISTING_LINE.match(line)
        instructions += bool(match)
        word = listing_word(match.group(2)) if match else None
        if word is not None and word & 127 in F_OPCODES:
            if not allow_f or not valid_f_word(word):
                problems.append(f"listing line {number}: floating instruction outside selected ISA: {line.strip()}")
            continue
        if (allow_system or allow_user) and word == UNIMP:
            continue  # the canonical illegal instruction: a program that traps on purpose
        if word is not None and word & 127 == 0x73 and (word >> 12) & 7:
            csr = word >> 20
            if csr in (1, 2, 3):
                if not allow_f or (word >> 12) & 7 == 4:
                    problems.append(f"listing line {number}: floating CSR outside selected ISA: {line.strip()}")
                continue
            if csr in COUNTER_CSRS:
                reads_only = (word >> 12) & 3 != 1 and (word >> 15) & 31 == 0  # csrrs/csrrc with x0 or uimm 0
                if not allow_counters or not reads_only:
                    problems.append(f"listing line {number}: counter access outside selected ISA: {line.strip()}")
                continue
        if allow_m and match and M_MNEMONIC.match(match.group(3)) and word is not None \
                and word & 127 == 0x33 and word >> 25 == 1:
            continue
        if match and FORBIDDEN_MNEMONIC.match(match.group(3)):
            if allow_system and SYSTEM_MNEMONIC.match(match.group(3)):
                continue
            if allow_user and match.group(3) == "ecall":
                continue
            if allow_privileged and PRIVILEGED_MNEMONIC.match(match.group(3)):
                if match.group(3) == "mret" or TRAP_CSR.search(line) or (allow_system and SYSTEM_CSR.search(line)):
                    continue
                problems.append(f"listing line {number}: CSR other than the four trap CSRs: {line.strip()}")
                continue
            problems.append(f"listing line {number}: instruction outside the M1 contract: {line.strip()}")
    # A listing with nothing to check passes every rule vacuously: a failed objdump that left an
    # empty file would otherwise hollow out the whole check.
    if instructions == 0:
        problems.append("listing has no instruction lines")
    return problems


SOFTWARE_MULDIV = ("rv32_mul", "rv32_divu", "rv32_remu", "rv32_div", "rv32_rem",
                   "__mulsi3", "__udivsi3", "__umodsi3", "__divsi3", "__modsi3")


def check_m_build(elf, listing):
    """An RV32IM build retires the software routines: the listing multiplies or divides in
    hardware, and none of programs/rv32/rt/muldiv.c's symbols is linked in."""
    problems = [f"RV32IM image still links {name}" for name in SOFTWARE_MULDIV if name in elf.symbols]
    if not any((match := LISTING_LINE.match(line)) and M_MNEMONIC.match(match.group(3)) for line in listing.splitlines()):
        problems.append("RV32IM image has no M-extension instruction")
    return problems


def check_image(elf, listing=None, ram_base=RAM_BASE, ram_size=RAM_SLICE_SIZE, entry=None, allow_privileged=False, allow_f=False,
                allow_m=False, allow_counters=False, allow_system=False, allow_user=False, page_tables=False):
    """Return a list of contract violations; an empty list means the image is acceptable."""
    entry = ram_base if entry is None else entry
    ram_end = ram_base + ram_size
    problems = []
    if elf.etype != ET_EXEC:
        problems.append(f"e_type is {elf.etype}, expected ET_EXEC (2)")
    if elf.machine != EM_RISCV:
        problems.append(f"e_machine is {elf.machine}, expected EM_RISCV (243)")
    if elf.flags != 0:
        described = []
        if elf.flags & EF_RISCV_RVC:
            described.append("RVC (compressed instructions)")
        if elf.flags & EF_RISCV_FLOAT_ABI:
            described.append(f"float ABI {elf.flags & EF_RISCV_FLOAT_ABI:#x} (expected soft float)")
        if elf.flags & EF_RISCV_RVE:
            described.append("RVE")
        if elf.flags & EF_RISCV_TSO:
            described.append("TSO")
        problems.append(f"e_flags is {elf.flags:#x}, expected 0: " + ", ".join(described or ["unknown bits"]))
    if elf.entry != entry:
        problems.append(f"entry point is {elf.entry:#010x}, expected {entry:#010x}")
    for name in REQUIRED_SYMBOLS:
        if name not in elf.symbols:
            problems.append(f"missing symbol {name}")
    symbols = elf.symbols
    if symbols.get("_start", entry) != entry:
        problems.append(f"_start is {symbols['_start']:#010x}, expected {entry:#010x}")
    loads = [segment for segment in elf.segments if segment.type == PT_LOAD]
    if not loads:
        problems.append("no PT_LOAD segment")
    else:
        lowest = min(segment.paddr for segment in loads)
        if lowest != ram_base:
            problems.append(f"lowest loadable address is {lowest:#010x}, expected {ram_base:#010x}")
    for segment in loads:
        where = f"segment at {segment.paddr:#010x}"
        if segment.paddr != segment.vaddr:
            problems.append(f"{where}: load address differs from run address {segment.vaddr:#010x}")
        if segment.paddr % 4:
            problems.append(f"{where}: not word aligned")
        if segment.filesz > segment.memsz:
            problems.append(f"{where}: file size {segment.filesz} exceeds memory size {segment.memsz}")
        if segment.paddr < ram_base or segment.paddr + segment.memsz > ram_end:
            problems.append(f"{where}: {segment.memsz} bytes leave RAM [{ram_base:#010x}, {ram_end:#010x})")
    by_name = {section.name: section for section in elf.sections}
    for name in REQUIRED_SECTIONS:
        if name not in by_name:
            problems.append(f"missing section {name}")
        elif by_name[name].size == 0 and name == ".text":  # a program may have no data (Track 2's small ones)
            problems.append(f"section {name} is empty")
    for section in elf.sections:
        if page_tables and section.name == PAGE_TABLES_SECTION:
            problems.extend(check_page_tables(section, symbols))
        elif section.flags & SHF_ALLOC and section.size and section.name not in REQUIRED_SECTIONS:
            problems.append(f"unexpected allocated section {section.name} ({section.size} bytes)")
    bss = by_name.get(".bss")
    if bss and all(name in symbols for name in ("__bss_start", "__bss_end")):
        if (symbols["__bss_start"], symbols["__bss_end"]) != (bss.addr, bss.addr + bss.size):
            problems.append("__bss_start/__bss_end do not match the .bss section")
        if symbols["__bss_start"] % 4 or symbols["__bss_end"] % 4:
            problems.append(".bss bounds are not word aligned")
    if all(name in symbols for name in ("_end", "_stack_bottom", "_stack_top")):
        end, bottom, top = symbols["_end"], symbols["_stack_bottom"], symbols["_stack_top"]
        if not end <= bottom < top <= ram_end:
            problems.append(f"stack [{bottom:#010x}, {top:#010x}) must follow _end {end:#010x} inside RAM")
        if top % 16:
            problems.append(f"_stack_top {top:#010x} is not 16-byte aligned")
    if elf.undefined:
        problems.append("undefined symbols: " + ", ".join(sorted(elf.undefined)))
    if listing is not None:
        problems.extend(check_listing(listing, allow_privileged, allow_f, allow_m, allow_counters, allow_system, allow_user))
    return problems


def check_page_tables(section, symbols):
    """The kernel's page-table section (issue #25): no file bytes, whole pages, bounded by the
    symbols PMP entries 6 and 7 use, and clear of the stack."""
    problems = []
    if section.type != SHT_NOBITS:
        problems.append(f"section {section.name} must be NOBITS")
    if section.addr % PAGE_SIZE or section.size % PAGE_SIZE:
        problems.append(f"section {section.name} is not whole pages at a page boundary")
    bounds = (symbols.get("__pagetables_start"), symbols.get("__pagetables_end"))
    if bounds != (section.addr, section.addr + section.size):
        problems.append(f"__pagetables_start/__pagetables_end do not match the {section.name} section")
    if "_stack_bottom" in symbols and section.addr + section.size > symbols["_stack_bottom"]:
        problems.append(f"section {section.name} overlaps the stack")
    return problems


def flatten(elf, ram_base=RAM_BASE):
    """Return the loadable bytes from ram_base to the last file byte, zero-filled between."""
    loads = [segment for segment in elf.segments if segment.type == PT_LOAD and segment.filesz]
    if not loads:
        return b""
    image = bytearray(max(segment.paddr + segment.filesz for segment in loads) - ram_base)
    for segment in loads:
        start = segment.paddr - ram_base
        image[start:start + segment.filesz] = segment.data
    return bytes(image)


def to_hex_words(image):
    """Little-endian 32-bit words, one per line, for $readmemh; the base address is implied."""
    padded = image + bytes(-len(image) % 4)
    return [f"{word:08x}" for (word,) in struct.iter_unpack("<I", padded)]


def write_hex(path, image):
    """Write `image` to `path` as the $readmemh file: to_hex_words, one word per line."""
    Path(path).write_text("".join(f"{word}\n" for word in to_hex_words(image)))


def summary(elf, ram_base=RAM_BASE, ram_size=RAM_SLICE_SIZE):
    sizes = {section.name: section.size for section in elf.sections}
    image = flatten(elf, ram_base)
    lines = [f"entry {elf.entry:#010x}, e_flags {elf.flags:#x}, RAM slice [{ram_base:#010x}, {ram_base + ram_size:#010x})"]
    for segment in elf.segments:
        if segment.type == PT_LOAD:
            lines.append(f"load {segment.paddr:#010x}: {segment.filesz} file bytes, {segment.memsz} memory bytes, flags {segment.flags:#x}")
    lines.append("sections " + ", ".join(f"{name} {sizes.get(name, 0)}" for name in REQUIRED_SECTIONS))
    if all(name in elf.symbols for name in ("_end", "_stack_bottom", "_stack_top")):
        lines.append(f"_end {elf.symbols['_end']:#010x}, stack [{elf.symbols['_stack_bottom']:#010x}, {elf.symbols['_stack_top']:#010x})")
    lines.append(f"image {len(image)} bytes = {len(to_hex_words(image))} words")
    return lines


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("elf", type=Path)
    parser.add_argument("--listing", type=Path, help="objdump disassembly to scan for forbidden instructions")
    parser.add_argument("--bin", type=Path, help="objcopy -O binary output that must match our flattening")
    parser.add_argument("--hex", type=Path, help="write little-endian word image for $readmemh")
    parser.add_argument("--ram-base", type=lambda text: int(text, 0), default=RAM_BASE)
    parser.add_argument("--ram-size", type=lambda text: int(text, 0), default=RAM_SLICE_SIZE)
    parser.add_argument("--allow-privileged", action="store_true",
                        help="admit csr* and mret in the listing (an image with a trap handler)")
    parser.add_argument("--allow-f", action="store_true", help="admit RV32F and floating CSRs, retaining ILP32")
    parser.add_argument("--allow-m", action="store_true", help="admit the M extension's multiply and divide instructions")
    parser.add_argument("--allow-counters", action="store_true", help="admit reads of the Zicntr counters (cycle, time, instret)")
    parser.add_argument("--allow-system", action="store_true",
                        help="implies --allow-privileged: also the interrupt CSRs (mstatus, mie, mip, mscratch), "
                             "mcounteren, scounteren, pmpcfg0-1, pmpaddr0-7, wfi and ecall (Track 2); since issue #20 also "
                             "medeleg, mideleg, the supervisor CSRs, satp, sret and sfence.vma")
    parser.add_argument("--allow-user", action="store_true",
                        help="implies --allow-counters: also ecall and unimp, what a user-mode program runs (Track 2, O5)")
    parser.add_argument("--page-tables", action="store_true",
                        help="admit the kernel's .pagetables section, NOBITS and page-aligned (issue #25)")
    parser.add_argument("--require-m", action="store_true",
                        help="implies --allow-m: the listing must use M instructions and the image must not contain the "
                             "software multiply/divide routines (an RV32IM build that really retired rt/muldiv.c)")
    args = parser.parse_args()
    args.allow_m = args.allow_m or args.require_m
    if (args.allow_f or args.allow_privileged or args.allow_m or args.allow_counters or args.allow_system or args.allow_user) \
            and args.listing is None:
        parser.error("--allow-f, --allow-m, --allow-counters, --allow-privileged, --allow-system and --allow-user require --listing")
    try:
        elf = parse_elf(args.elf.read_bytes())
        listing = args.listing.read_text() if args.listing else None
        problems = check_image(elf, listing, args.ram_base, args.ram_size, allow_privileged=args.allow_privileged, allow_f=args.allow_f,
                               allow_m=args.allow_m, allow_counters=args.allow_counters, allow_system=args.allow_system,
                               allow_user=args.allow_user, page_tables=args.page_tables)
        if args.require_m:
            problems.extend(check_m_build(elf, listing))
        image = flatten(elf, args.ram_base)
        if args.bin and args.bin.read_bytes() != image:
            problems.append(f"{args.bin} differs from the flattened PT_LOAD contents ({len(image)} bytes)")
    except (ImageError, OSError) as error:
        parser.exit(1, f"{args.elf}: {error}\n")
    if problems:
        parser.exit(1, "".join(f"{args.elf}: {problem}\n" for problem in problems))
    if args.hex:
        args.hex.parent.mkdir(parents=True, exist_ok=True)
        write_hex(args.hex, image)
    print("\n".join(summary(elf, args.ram_base, args.ram_size)))


if __name__ == "__main__":
    main()
