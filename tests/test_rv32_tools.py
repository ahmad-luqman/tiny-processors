"""Unit tests for the RV32 image checker and QEMU driver; no cross toolchain needed."""

import json
from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest

from tools.rv32_asm import (ADDI, BOOTROM, CLINT, CONSOLE, CSRRC, CSRRS, CSRRWI, DISPLAY, DONE, FB, INPUT, LW, MSTATUS,
                            PLIC, RAM, VIRTIO, i_type)
from tools.rv32_f_asm import arithmetic, flw, fsw
from tools import rv32_asm
from tools.rv32_rtl import floating_word, trap_records_by_region
from tools.rv32_devices import (DIAG_EXPECTED_VALUES, EVENT_PRESS, EVENT_VALID, FB_SIZE, KEYS, QUEUE_SIZE, diag_checksum,
                                event_word, frame_hash, is_decimal, key_code, parse_input_script, render_diag_frame)
from tools.rv32_image import (ImageError, check_a_build, check_image, check_listing, check_m_build, flatten, parse_elf,
                              to_hex_words)
from tools.rv32_run_qemu import classify, qemu_command
from tools import rv32_vendor_libc


ROOT = Path(__file__).resolve().parents[1]
SHT_PROGBITS, SHT_SYMTAB, SHT_STRTAB, SHT_NOBITS = 1, 2, 3, 8
SHF_WRITE, SHF_ALLOC, SHF_EXECINSTR = 1, 2, 4
GOOD_SECTIONS = [(".text", SHT_PROGBITS, SHF_ALLOC | SHF_EXECINSTR, RAM, bytes(range(256))),
                 (".rodata", SHT_PROGBITS, SHF_ALLOC, RAM + 0x100, b"R" * 0x20),
                 (".data", SHT_PROGBITS, SHF_ALLOC | SHF_WRITE, RAM + 0x120, b"D" * 0x10),
                 (".bss", SHT_NOBITS, SHF_ALLOC | SHF_WRITE, RAM + 0x130, 0x40)]
GOOD_SYMBOLS = {"_start": RAM, "main": RAM + 0x10, "__bss_start": RAM + 0x130, "__bss_end": RAM + 0x170,
                "_end": RAM + 0x170, "_stack_bottom": RAM + 0x3C000, "_stack_top": RAM + 0x40000}
# Independently re-derived expectations of programs/rv32/selfcheck.c, in check order.
SELFCHECK_EXPECTED_VALUES = [
    0x12345678, 0, 77, 360, 610, 21, 0xF8000000, 0x08000000, 1, 0xFFFFFFFE, 0x7FFFFFFF,
    97406784, 0xFFFFFFFD, 0xFFFFFFFD, 0xFFFFFFFF, 0xFFFFFFFD, 0x80000000, 0xFFFFFFFF, 42,
    0xFFFFFFFF, 42, 0, 0xFFFFFFFB, 0xFB, 0xFFFFFB2E, 0xBEEF, 0x44, 0xFFFF8001]


def build_elf(sections=GOOD_SECTIONS, symbols=GOOD_SYMBOLS, segments=None, entry=RAM, flags=0,
              machine=243, etype=2, ei_class=1, ei_data=1, undefined=()):
    """Assemble a minimal ELF32 executable from section and symbol descriptions."""
    if segments is None:
        segments = [(RAM, RAM, 0x130, 0x170, 7)]
    ehsize, phentsize, shentsize = 52, 32, 40
    offset = ehsize + phentsize * len(segments)
    blobs, headers, names, offsets = [], [(0,) * 10], b"\0", {}
    for name, sh_type, sh_flags, addr, payload in sections:
        size = payload if isinstance(payload, int) else len(payload)
        headers.append((len(names), sh_type, sh_flags, addr, offset, size, 0, 0, 4, 0))
        names += name.encode() + b"\0"
        if not isinstance(payload, int):
            offsets[addr] = offset
            blobs.append(payload)
            offset += size
    strtab, symtab = b"\0", struct.pack("<IIIBBH", 0, 0, 0, 0, 0, 0)
    for name, value in symbols.items():
        symtab += struct.pack("<IIIBBH", len(strtab), value, 0, 0x12, 0, 1)
        strtab += name.encode() + b"\0"
    for name in undefined:
        symtab += struct.pack("<IIIBBH", len(strtab), 0, 0, 0x12, 0, 0)
        strtab += name.encode() + b"\0"
    symtab_index = len(headers)
    headers.append((len(names), SHT_SYMTAB, 0, 0, offset, len(symtab), symtab_index + 1, 1, 4, 16))
    names += b".symtab\0"
    blobs.append(symtab)
    offset += len(symtab)
    headers.append((len(names), SHT_STRTAB, 0, 0, offset, len(strtab), 0, 0, 1, 0))
    names += b".strtab\0"
    blobs.append(strtab)
    offset += len(strtab)
    shstr_index = len(headers)
    shstr_name = len(names)
    names += b".shstrtab\0"
    headers.append((shstr_name, SHT_STRTAB, 0, 0, offset, len(names), 0, 0, 1, 0))
    blobs.append(names)
    offset += len(names)
    shoff = offset
    ident = b"\x7fELF" + bytes([ei_class, ei_data, 1, 0]) + bytes(8)
    header = ident + struct.pack("<HHIIIIIHHHHHH", etype, machine, 1, entry, ehsize, shoff, flags,
                                 ehsize, phentsize, len(segments), shentsize, len(headers), shstr_index)
    program_headers = b""
    for paddr, vaddr, filesz, memsz, p_flags in segments:
        file_offset = offsets.get(paddr, ehsize + phentsize * len(segments))
        program_headers += struct.pack("<8I", 1, file_offset, vaddr, paddr, filesz, memsz, p_flags, 4)
    return header + program_headers + b"".join(blobs) + b"".join(struct.pack("<10I", *h) for h in headers)


class ImageCheckerTests(unittest.TestCase):
    def problems(self, **overrides):
        return check_image(parse_elf(build_elf(**overrides)))

    def test_good_image_passes(self):
        elf = parse_elf(build_elf())
        self.assertEqual(check_image(elf), [])
        self.assertEqual(elf.entry, RAM)
        self.assertEqual(elf.symbols["_stack_top"], RAM + 0x40000)
        self.assertEqual([s.name for s in elf.sections if s.name.startswith(".")][:4], list(dict(
            (name, None) for name, *_ in GOOD_SECTIONS)))

    def test_rejects_wrong_class_endianness_machine_type(self):
        with self.assertRaisesRegex(ImageError, "ELFCLASS32"):
            parse_elf(build_elf(ei_class=2))
        with self.assertRaisesRegex(ImageError, "little-endian"):
            parse_elf(build_elf(ei_data=2))
        with self.assertRaisesRegex(ImageError, "not an ELF"):
            parse_elf(b"MZ" + bytes(64))
        self.assertTrue(any("EM_RISCV" in p for p in self.problems(machine=62)))
        self.assertTrue(any("ET_EXEC" in p for p in self.problems(etype=3)))

    def test_floating_point_only_in_named_functions(self):
        """Issue #33: the kernel may hold F instructions only in its FPU save and load."""
        listing = ("80000000 <fpu_save>:\n80000000: 00a52027     \tfsw\tft0, 0x0(a0)\n"
                   "80000004: 003022f3     \tfrcsr\tt0\n"
                   "80000008 <kernel_trap>:\n80000008: 00a52027     \tfsw\tft0, 0x0(a0)\n"
                   "8000000c: 003022f3     \tfrcsr\tt0\n")
        problems = check_listing(listing, f_functions={"fpu_save", "fpu_load"})
        self.assertEqual([p.split(":")[0] for p in problems], ["listing line 5", "listing line 6"])
        self.assertEqual(check_listing(listing, allow_f=True), [])
        self.assertEqual(len(check_listing(listing)), 4)

    def test_rejects_flags_and_entry(self):
        self.assertTrue(any("RVC" in p for p in self.problems(flags=0x1)))
        self.assertTrue(any("float ABI" in p for p in self.problems(flags=0x4)))
        self.assertTrue(any("entry point" in p for p in self.problems(entry=RAM + 4)))
        # Issue #33: the single-float ABI (ilp32f, 0x2) only when asked for, and then only it.
        self.assertTrue(any("expected soft float" in p for p in self.problems(flags=0x2)))
        self.assertEqual(check_image(parse_elf(build_elf(flags=0x2)), hard_float=True, allow_f=True), [])
        for flags in (0x0, 0x4, 0x3):
            with self.subTest(flags=flags):
                self.assertTrue(check_image(parse_elf(build_elf(flags=flags)), hard_float=True, allow_f=True))
        self.assertTrue(any("needs F" in p for p in check_image(parse_elf(build_elf(flags=0x2)), hard_float=True,
                                                                 allow_f=False)))
        symbols = dict(GOOD_SYMBOLS, _start=RAM + 8)
        self.assertTrue(any("_start is" in p for p in self.problems(symbols=symbols)))

    def test_rejects_bad_segments(self):
        self.assertTrue(any("lowest loadable" in p for p in self.problems(
            segments=[(RAM + 0x1000, RAM + 0x1000, 0x130, 0x170, 7)])))
        self.assertTrue(any("load address differs" in p for p in self.problems(
            segments=[(RAM, RAM + 0x100, 0x130, 0x170, 7)])))
        self.assertTrue(any("leave RAM" in p for p in self.problems(
            segments=[(RAM, RAM, 0x130, 0x40001, 7)])))
        self.assertTrue(any("word aligned" in p for p in self.problems(
            segments=[(RAM, RAM, 0x130, 0x170, 7), (RAM + 0x172, RAM + 0x172, 0, 4, 6)])))
        self.assertTrue(any("no PT_LOAD" in p for p in self.problems(segments=[])))

    def test_rejects_missing_sections_symbols_and_stack_overlap(self):
        without_bss = [s for s in GOOD_SECTIONS if s[0] != ".bss"]
        self.assertTrue(any("missing section .bss" in p for p in self.problems(sections=without_bss)))
        extra = GOOD_SECTIONS + [(".eh_frame", SHT_PROGBITS, SHF_ALLOC, RAM + 0x170, b"E" * 8)]
        self.assertTrue(any("unexpected allocated section .eh_frame" in p for p in self.problems(sections=extra)))
        symbols = {name: value for name, value in GOOD_SYMBOLS.items() if name != "_end"}
        self.assertTrue(any("missing symbol _end" in p for p in self.problems(symbols=symbols)))
        symbols = dict(GOOD_SYMBOLS, _stack_bottom=RAM + 0x100)
        self.assertTrue(any("stack" in p for p in self.problems(symbols=symbols)))
        symbols = dict(GOOD_SYMBOLS, _stack_top=RAM + 0x3FFF8)
        self.assertTrue(any("16-byte" in p for p in self.problems(symbols=symbols)))
        symbols = dict(GOOD_SYMBOLS, __bss_end=RAM + 0x160)
        self.assertTrue(any("__bss_start/__bss_end" in p for p in self.problems(symbols=symbols)))
        self.assertTrue(any("undefined symbols: memcpy" in p for p in self.problems(undefined=["memcpy"])))

    def test_page_tables_only_where_asked_and_well_formed(self):
        """Issue #25: the kernel's .pagetables is admitted with page_tables=True, and then only as
        whole NOBITS pages between __pagetables_start and __pagetables_end, below the stack."""
        def tables(sh_type=SHT_NOBITS, addr=RAM + 0x1000, payload=0x2000):
            return GOOD_SECTIONS + [(".pagetables", sh_type, SHF_ALLOC | SHF_WRITE, addr, payload)]
        bounds = dict(GOOD_SYMBOLS, __pagetables_start=RAM + 0x1000, __pagetables_end=RAM + 0x3000)
        check = lambda **overrides: check_image(parse_elf(build_elf(**overrides)), page_tables=True)  # noqa: E731
        self.assertEqual(check(sections=tables(), symbols=bounds), [])
        self.assertTrue(any("unexpected allocated section .pagetables" in p
                            for p in self.problems(sections=tables(), symbols=bounds)), "a program may not carry one")
        self.assertTrue(any("must be NOBITS" in p for p in check(sections=tables(SHT_PROGBITS, payload=b"P" * 0x2000),
                                                                 symbols=bounds)))
        self.assertTrue(any("whole pages" in p for p in check(sections=tables(payload=0x1800), symbols=bounds)))
        self.assertTrue(any("__pagetables_start/__pagetables_end" in p for p in check(sections=tables(), symbols=GOOD_SYMBOLS)))
        low_stack = dict(bounds, _stack_bottom=RAM + 0x2000, _end=RAM + 0x170)
        self.assertTrue(any("overlaps the stack" in p for p in check(sections=tables(), symbols=low_stack)))

    def test_flatten_fills_gaps_and_hex_words_are_little_endian(self):
        sections = GOOD_SECTIONS + [(".data2", SHT_PROGBITS, SHF_ALLOC, RAM + 0x200, b"\x17\x01\x04\x00")]
        segments = [(RAM, RAM, 0x130, 0x130, 7), (RAM + 0x200, RAM + 0x200, 4, 4, 6)]
        elf = parse_elf(build_elf(sections=sections, segments=segments))
        image = flatten(elf)
        self.assertEqual(len(image), 0x204)
        self.assertEqual(image[:4], bytes([0, 1, 2, 3]))
        self.assertEqual(image[0x130:0x200], bytes(0xD0))
        self.assertEqual(to_hex_words(image)[0x80], "00040117")
        self.assertEqual(to_hex_words(b"\x01\x02\x03"), ["00030201"])
        self.assertEqual(to_hex_words(b""), [])

    def test_listing_rejects_unknown_and_forbidden_instructions(self):
        good = "80000000 <_start>:\n80000000: 00040117     \tauipc\tsp, 0x40\n80000004: 00010113     \taddi\tsp, sp, 0\n"
        self.assertEqual(check_listing(good), [])
        self.assertEqual(check_listing("80000008: 02b50533     \tmul\ta0, a0, a1\n")[0][:15], "listing line 1:")
        for text in ("80000008: 02b5c533     \tdiv\ta0, a1, a2", "80000008: 30047073     \tcsrci\tmstatus, 8",
                     "80000008: 0001         \tc.nop", "80000008: 00000073     \tecall",
                     "80000008: 0000         \t<unknown>", "80000008: 00 00 00 00  \tmul\ta0, a0, a1"):
            with self.subTest(text=text):
                self.assertEqual(len(check_listing(text)), 1)
        self.assertEqual(check_listing(good + "  /* mul by hand */\n; div in a comment\nadd a0, a0, a1\n"), [])
        # A listing with nothing in it would pass every rule; that is what a failed objdump leaves behind.
        for empty in ("", "  /* mul by hand */\n; div in a comment\nadd a0, a0, a1\n"):
            with self.subTest(empty=empty):
                self.assertEqual(check_listing(empty), ["listing has no instruction lines"])
                self.assertEqual(check_listing(empty, allow_privileged=True), ["listing has no instruction lines"])

    def test_system_gate_admits_the_interrupt_csrs_wfi_and_ecall(self):
        """Track 2: --allow-system admits mstatus, mie, mip, mscratch, wfi and ecall (O1, O2) and
        mcounteren and the PMP CSRs (O5) on top of the trap handler's CSRs; --allow-privileged alone
        still refuses them."""
        base = "80000000: 00040117     \tauipc\tsp, 0x40\n"
        system = ["80000004: 30047073     \tcsrci\tmstatus, 8", "80000004: 30451073     \tcsrw\tmie, a0",
                  "80000004: 34402573     \tcsrr\ta0, mip", "80000004: 34051073     \tcsrw\tmscratch, a0",
                  "80000004: 10500073     \twfi", "80000004: 00000073     \tecall",
                  "80000004: 30679073     \tcsrw\tmcounteren, a5", "80000004: 3a051073     \tcsrw\tpmpcfg0, a0",
                  "80000004: 3b751073     \tcsrw\tpmpaddr7, a0",
                  # issue #20: S-mode and Sv32
                  "80000004: 10200073     \tsret", "80000004: 12000073     \tsfence.vma",
                  "80000004: 18051073     \tcsrw\tsatp, a0", "80000004: 30251073     \tcsrw\tmedeleg, a0",
                  "80000004: 10551073     \tcsrw\tstvec, a0", "80000004: 14202573     \tcsrr\ta0, scause"]
        for line in system:
            with self.subTest(line=line):
                self.assertEqual(len(check_listing(base + line, allow_privileged=True)), 1)
                self.assertEqual(check_listing(base + line, allow_system=True), [])
        self.assertEqual(check_listing(base + "80000004: 30200073     \tmret", allow_system=True), [])
        unimp = base + "80000004: c0001073     \tunimp"  # the canonical illegal instruction, for a program that traps on purpose
        self.assertEqual(len(check_listing(unimp, allow_counters=True)), 1)
        self.assertEqual(check_listing(unimp, allow_system=True), [])
        for line in ("80000004: 3b851073     \tcsrw\tpmpaddr8, a0", "80000004: 3a251073     \tcsrw\tpmpcfg2, a0",
                     "80000004: 60051073     \tcsrw\thstatus, a0"):
            with self.subTest(line=line):
                self.assertEqual(len(check_listing(base + line, allow_system=True)), 1)

    def test_user_gate_admits_ecall_unimp_and_counter_reads_only(self):
        """O5: --allow-user admits what a user-mode program may run, and none of the machine's
        instructions or CSRs."""
        base = "80000000: 00040117     \tauipc\tsp, 0x40\n"
        for line in ("80000004: 00000073     \tecall", "80000004: c0001073     \tunimp",
                     "80000004: c0102573     \trdtime\ta0"):
            with self.subTest(line=line):
                self.assertEqual(check_listing(base + line, allow_user=True), [])
        for line in ("80000004: 30002573     \tcsrr\ta0, mstatus", "80000004: 30200073     \tmret",
                     "80000004: 10500073     \twfi", "80000004: 34051073     \tcsrw\tmscratch, a0",
                     "80000004: c0101073     \tcsrw\ttime, zero"):
            with self.subTest(line=line):
                self.assertEqual(len(check_listing(base + line, allow_user=True)), 1)

    def test_m_and_counter_gates_admit_exactly_their_instructions(self):
        base = "80000000: 00040117     \tauipc\tsp, 0x40\n"
        m_lines = ["80000004: 02b50533     \tmul\ta0, a0, a1", "80000004: 02b51533     \tmulh\ta0, a0, a1",
                   "80000004: 02b52533     \tmulhsu\ta0, a0, a1", "80000004: 02b53533     \tmulhu\ta0, a0, a1",
                   "80000004: 02c5c533     \tdiv\ta0, a1, a2", "80000004: 02c5d533     \tdivu\ta0, a1, a2",
                   "80000004: 02c5e533     \trem\ta0, a1, a2", "80000004: 02c5f533     \tremu\ta0, a1, a2"]
        for line in m_lines:
            with self.subTest(line=line):
                self.assertEqual(len(check_listing(base + line)), 1, "RV32I listings still reject M")
                self.assertEqual(check_listing(base + line, allow_m=True), [])
        # The gate reads the word too: a mnemonic that says mul on a word that is not M is refused.
        self.assertEqual(len(check_listing(base + "80000004: 00b50533     \tmul\ta0, a0, a1", allow_m=True)), 1)
        self.assertEqual(len(check_listing(base + "80000004: 30047073     \tcsrci\tmstatus, 8", allow_m=True)), 1)
        # Counter reads print as rdcycle/rdtime/rdinstret, which no csr* rule matches: the word decides.
        reads = ["80000004: c0002573     \trdcycle\ta0", "80000004: c0102573     \trdtime\ta0",
                 "80000004: c0202573     \trdinstret\ta0", "80000004: c8002573     \trdcycleh\ta0",
                 "80000004: c0206573     \tcsrrsi\ta0, instret, 0"]
        for line in reads:
            with self.subTest(line=line):
                self.assertEqual(len(check_listing(base + line)), 1, "without the gate a counter read is refused")
                self.assertEqual(check_listing(base + line, allow_counters=True), [])
        for write in ("80000004: c0029073     \tcsrw\tcycle, t0", "80000004: c022a573     \tcsrrs\ta0, instret, t0"):
            with self.subTest(write=write):
                self.assertEqual(len(check_listing(base + write, allow_counters=True)), 1, "counters are read-only")

    def test_a_gate_admits_exactly_the_valid_atomic_words(self):
        """Issue #34: an opcode-0x2f word needs allow_a and a valid encoding, whatever objdump calls it."""
        base = "80000000: 00040117     \tauipc\tsp, 0x40\n"
        def line(word, text="amo"):
            return f"{base}80000004: {word:08x}     \t{text}"
        valid = [rv32_asm.LR_W(10, 11), rv32_asm.LR_W(10, 11, aq=1, rl=1), rv32_asm.SC_W(10, 12, 11),
                 *(op(10, 12, 11) for op in rv32_asm.AMO_OPS), rv32_asm.AMOADD_W(0, 12, 11, aq=1)]
        for word in valid:
            with self.subTest(word=f"{word:08x}"):
                self.assertEqual(len(check_listing(line(word))), 1, "RV32I listings reject A")
                self.assertEqual(check_listing(line(word), allow_a=True), [])
        invalid = [rv32_asm.LR_W(10, 11) | (5 << 20),          # LR.W with rs2 != 0
                   rv32_asm.AMOADD_W(10, 12, 11) ^ (1 << 12),  # funct3 3: a doubleword
                   rv32_asm.r_type(0x2F, 10, 2, 11, 12, 0b00101 << 2),  # an undefined funct5 (AMOCAS.W is Zacas)
                   rv32_asm.AMOADD_W(10, 12, 11) ^ (2 << 12)]  # funct3 0
        for word in invalid:
            with self.subTest(word=f"{word:08x}"):
                self.assertEqual(len(check_listing(line(word), allow_a=True)), 1)
        self.assertEqual(check_a_build(line(rv32_asm.SC_W(10, 12, 11))), [])
        self.assertEqual(check_a_build(base), ["RV32A image has no A-extension instruction"])
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            elf, listing, plain = path / "a.elf", path / "a.lst", path / "plain.lst"
            elf.write_bytes(build_elf())
            listing.write_text(line(rv32_asm.AMOSWAP_W(10, 12, 11)) + "\n")
            plain.write_text(base)

            def check(*options):
                return subprocess.run([sys.executable, str(ROOT / "tools/rv32_image.py"), str(elf), *options],
                                      capture_output=True, text=True)
            self.assertEqual(check("--listing", str(listing)).returncode, 1, "without a flag the AMO is refused")
            self.assertEqual(check("--listing", str(listing), "--allow-a").returncode, 0)
            self.assertEqual(check("--listing", str(listing), "--require-a").returncode, 0, "--require-a implies --allow-a")
            result = check("--listing", str(plain), "--require-a")
            self.assertEqual(result.returncode, 1)
            self.assertIn("no A-extension instruction", result.stderr)
            self.assertEqual(check("--allow-a").returncode, 2, "--allow-a needs a listing")

    def test_require_m_needs_hardware_multiply_and_no_software_routines(self):
        with_m = "80000000: 00040117     \tauipc\tsp, 0x40\n80000004: 02b50533     \tmul\ta0, a0, a1\n"
        without_m = "80000000: 00040117     \tauipc\tsp, 0x40\n80000004: 00b50533     \tadd\ta0, a0, a1\n"
        self.assertEqual(check_m_build(parse_elf(build_elf()), with_m), [])
        for routine in ("__mulsi3", "__divsi3", "rv32_divu"):
            with self.subTest(routine=routine):
                still_linked = parse_elf(build_elf(symbols={**GOOD_SYMBOLS, routine: RAM + 0x20}))
                self.assertEqual(check_m_build(still_linked, with_m), [f"RV32IM image still links {routine}"])
        self.assertEqual(check_m_build(parse_elf(build_elf()), without_m), ["RV32IM image has no M-extension instruction"])
        both = parse_elf(build_elf(symbols={**GOOD_SYMBOLS, "__mulsi3": RAM + 0x20, "__divsi3": RAM + 0x40}))
        self.assertEqual(len(check_m_build(both, without_m)), 3)

    def test_require_m_alone_implies_allow_m(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            good, soft, listing = path / "good.elf", path / "soft.elf", path / "good.lst"
            good.write_bytes(build_elf())
            soft.write_bytes(build_elf(symbols={**GOOD_SYMBOLS, "__mulsi3": RAM + 0x20}))
            listing.write_text("80000000: 00040117     \tauipc\tsp, 0x40\n80000004: 02b50533     \tmul\ta0, a0, a1\n")

            def check(elf, *options):
                return subprocess.run([sys.executable, str(ROOT / "tools/rv32_image.py"), str(elf), *options],
                                      capture_output=True, text=True)
            result = check(good, "--listing", str(listing), "--require-m")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(check(good, "--listing", str(listing), "--allow-m", "--require-m").returncode, 0)
            result = check(soft, "--listing", str(listing), "--require-m")
            self.assertEqual(result.returncode, 1)
            self.assertIn("still links __mulsi3", result.stderr)
            self.assertEqual(check(good, "--listing", str(listing)).returncode, 1, "without either flag mul is refused")
            result = check(good, "--require-m")
            self.assertEqual(result.returncode, 2, "--require-m needs a listing to inspect")
            self.assertIn("--allow-m, --allow-a, --allow-counters, --allow-privileged, --allow-system and --allow-user ", result.stderr)

    def test_selfcheck_expected_checksum_matches_source_and_makefile(self):
        checksum = 2166136261
        for value in SELFCHECK_EXPECTED_VALUES:
            checksum = ((checksum ^ value) * 16777619) & 0xFFFFFFFF
        source = (ROOT / "programs/rv32/selfcheck.c").read_text()
        pinned = re.search(r"#define SELFCHECK_EXPECTED 0x([0-9a-f]{8})u", source).group(1)
        self.assertEqual(pinned, f"{checksum:08x}")
        self.assertEqual(len(re.findall(r"^\s*CHECK\(", source, re.MULTILINE)), len(SELFCHECK_EXPECTED_VALUES))
        makefile = (ROOT / "Makefile").read_text()
        self.assertIn(f"RV32_SELFCHECK_HEX := {checksum:08x}", makefile)

    def test_cli_hard_float_needs_allow_f(self):
        """Issue #33: --hard-float without --allow-f is refused before anything is read."""
        with tempfile.TemporaryDirectory() as directory:
            elf = Path(directory) / "hf.elf"
            elf.write_bytes(build_elf(flags=0x2))
            listing = Path(directory) / "hf.lst"
            listing.write_text("80000000: 00000013     \tnop\n")
            tool = [sys.executable, str(ROOT / "tools/rv32_image.py"), str(elf), "--listing", str(listing)]
            result = subprocess.run(tool + ["--hard-float"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 2)
            self.assertIn("--hard-float requires --allow-f", result.stderr)
            result = subprocess.run(tool + ["--hard-float", "--allow-f"], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_cli_reports_problems_and_writes_hex(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory)
            good, bad, hexfile = path / "good.elf", path / "bad.elf", path / "out/good.hex"
            good.write_bytes(build_elf())
            bad.write_bytes(build_elf(entry=RAM + 4, flags=1))
            result = subprocess.run([sys.executable, str(ROOT / "tools/rv32_image.py"), str(good),
                                     "--hex", str(hexfile)], capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("entry 0x80000000", result.stdout)
            self.assertEqual(hexfile.read_text().splitlines()[0], "03020100")
            self.assertEqual(len(hexfile.read_text().splitlines()), 0x130 // 4)
            result = subprocess.run([sys.executable, str(ROOT / "tools/rv32_image.py"), str(bad),
                                     "--hex", str(path / "bad.hex")], capture_output=True, text=True)
            self.assertEqual(result.returncode, 1)
            self.assertIn("entry point", result.stderr)
            self.assertIn("RVC", result.stderr)
            self.assertFalse((path / "bad.hex").exists())


class QemuDriverTests(unittest.TestCase):
    def test_command_shape(self):
        command = qemu_command("qemu-system-riscv32", "fw.elf")
        self.assertEqual(command[:9], ["qemu-system-riscv32", "-M", "virt", "-cpu", "rv32i", "-bios", "none",
                                       "-kernel", "fw.elf"])
        self.assertIn("-no-reboot", command)
        self.assertNotIn("-no-shutdown", command)
        self.assertEqual(command[command.index("-m") + 1], "8M")
        self.assertEqual(command[command.index("-monitor") + 1], "none")
        self.assertNotIn("-d", command)
        self.assertIn("-D", qemu_command("q", "fw.elf", log="q.log"))
        self.assertNotIn("-icount", command)  # host time unless asked; the diff and arch-test runners don't ask
        for shift in (0, 3):  # 0 is a real shift, not "off"
            timed = qemu_command("q", "fw.elf", icount=shift)
            self.assertEqual(timed[timed.index("-icount") + 1], f"shift={shift},sleep=off")

    def test_os_sessions_run_on_instruction_counted_time(self):
        # A dropped --icount only shows in the jobs golden on a fast host, so pin the wiring here.
        makefile = (ROOT / "Makefile").read_text()
        command = makefile.split("\nRV32_OS_QEMU = ", 1)[1].split("\n", 1)[0]
        self.assertIn("rv32_run_qemu.py", command)
        self.assertIn("--icount $(RV32_OS_QEMU_ICOUNT)", command)
        for target in ("run-rv32-os-qemu", "run-rv32-os-qemu-reboot", "run-rv32-os-qemu-enter", "run-rv32-os-jobs-qemu"):
            lines = makefile.split(f"\n{target}:", 1)[1].splitlines()[1:]
            recipe = lines[:next((i for i, line in enumerate(lines) if not line.startswith("\t")), len(lines))]
            self.assertTrue(any("$(RV32_OS_QEMU) " in line for line in recipe), target)
            for line in recipe:
                if "rv32_run_qemu.py" in line:
                    self.assertIn("--icount $(RV32_OS_QEMU_ICOUNT)", line, target)

    def test_classify_pass_and_fail(self):
        self.assertEqual(classify(0, "PASS 807d9fad\n", False), (True, "pass", 0, "807d9fad"))
        self.assertTrue(classify(0, "PASS 807d9fad\r\n", False, expect_hex="807D9FAD").ok)
        self.assertFalse(classify(0, "PASS 807d9fad\n", False, expect_hex="00000000").ok)
        self.assertEqual(classify(3, "FAIL 3\n", False).code, 3)
        self.assertFalse(classify(3, "FAIL 3\n", False).ok)

    def test_classify_protocol_mismatches(self):
        self.assertIn("exit status was 1", classify(1, "PASS 807d9fad\n", False).reason)
        self.assertIn("exit status was 0", classify(0, "FAIL 3\n", False).reason)
        self.assertIn("timed out", classify(None, "", True).reason)
        self.assertIn("exactly one", classify(0, "boot\nPASS 807d9fad\n", False).reason)
        self.assertIn("exactly one", classify(0, "", False).reason)
        self.assertIn("unrecognized", classify(0, "PASS 807d9fa\n", False).reason)
        self.assertIn("unrecognized", classify(0, "FAIL 0\n", False).reason)
        self.assertIn("unrecognized", classify(0, "FAIL 1000\n", False).reason)


class DeviceHelperTests(unittest.TestCase):
    """tools/rv32_devices.py is the reference the emulator and testbench are compared against."""

    def test_frame_hash_is_the_documented_shift_add(self):
        # Two words by hand: h = 5381; h = h*33 ^ w1; h = h*33 ^ w2, all mod 2^32.
        h = 5381
        h = (h * 33 ^ 0x11223344) & 0xFFFFFFFF
        h = (h * 33 ^ 0xFFFFFFFF) & 0xFFFFFFFF
        self.assertEqual(frame_hash(bytes.fromhex("44332211ffffffff")), h)
        self.assertEqual(frame_hash(b""), 5381)
        self.assertNotEqual(frame_hash(bytes(FB_SIZE)), frame_hash(bytes(FB_SIZE - 4)), "length matters")
        with self.assertRaises(ValueError):
            frame_hash(b"abc")

    def test_event_words_and_key_codes(self):
        self.assertEqual(event_word(True, 1), EVENT_VALID | EVENT_PRESS | 1)
        self.assertEqual(event_word(False, 31), EVENT_VALID | 31)
        self.assertEqual((key_code("left"), key_code("LEFT"), key_code("7"), key_code("31")), (1, 1, 7, 31))
        self.assertEqual(key_code("000000001"), 1, "nine digits is the most a number may have")
        for bad in ("32", "-1", "shift", "", "0000000001", "٣"):  # ten digits; an Arabic-Indic three
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    key_code(bad)
        self.assertEqual([is_decimal(t) for t in ("0", "999999999", "0000000001", "", "1x", "٣", "-1")],
                         [True, True, False, False, False, False, False])
        with self.assertRaises(ValueError):
            event_word(True, 32)

    def test_input_script_grammar(self):
        text = "# a comment\n\nframe 0 down LEFT\nframe 0 up left\n  frame 3 down 5 \nframe 3 up Q\n"
        self.assertEqual(parse_input_script(text),
                         [(0, event_word(True, 1)), (0, event_word(False, 1)), (3, event_word(True, 5)),
                          (3, event_word(False, 13))])
        self.assertEqual(parse_input_script(""), [])
        self.assertEqual(parse_input_script("frame 0 down A\r\nframe 000000009 up 000000001\r\n"),
                         [(0, event_word(True, 8)), (9, event_word(False, 1))], "CRLF and nine-digit numbers")
        for bad in ("frame 1 down", "frame x down A", "frame 1 press A", "frame 2 down A\nframe 1 up A",
                    "frame 1 down NOPE", "frame -1 down A", "key 1 down A", "frame 1 down A extra",
                    "frame 0000000001 down A", "frame 0 down 0000000001", "frame ٣ down A"):
            with self.subTest(bad=bad):
                with self.assertRaisesRegex(ValueError, "input script line"):
                    parse_input_script(bad)

    def test_diag_frame_and_checksum_match_source_and_makefile(self):
        """The diagnostic's frame-1 hash and PASS value are derived here from the pattern and the
        checked values, independently of any run, and must match diag.c and the Makefile."""
        frame1 = render_diag_frame(1)
        self.assertEqual(len(frame1), FB_SIZE)
        self.assertEqual(frame1[0], 0, "(0 ^ 0)")
        self.assertEqual(frame1[3 * 320 + 5], 5 ^ 3)
        self.assertEqual(frame1[40 * 320 + 100], 0xE0, "the red box")
        self.assertEqual(frame1[120 * 320 + 319], 0x1C, "the green row")
        self.assertEqual(render_diag_frame(2)[200 * 320 + 10], 0x03, "the blue box in frame 2 only")
        self.assertEqual(frame1[200 * 320 + 10], (10 ^ 200) & 0xFF)
        makefile = (ROOT / "Makefile").read_text()
        self.assertIn(f"RV32_DIAG_FRAME1_HEX := {frame_hash(frame1):08x}", makefile)
        self.assertIn(f"RV32_DIAG_FRAME2_HEX := {frame_hash(render_diag_frame(2)):08x}", makefile)
        self.assertIn(f"RV32_DIAG_HEX := {diag_checksum():08x}", makefile)
        source = (ROOT / "programs/rv32/diag.c").read_text()
        pinned = re.search(r"#define DIAG_EXPECTED 0x([0-9a-f]{8})u", source).group(1)
        self.assertEqual(pinned, f"{diag_checksum():08x}")
        checks = len(re.findall(r"^\s*CHECK\(", source, re.MULTILINE))
        self.assertEqual(checks, sum(1 for value in DIAG_EXPECTED_VALUES if value != "frame1") - 4,
                         "one CHECK per expected value except the frame hash and the four folded events")

    def test_privileged_instructions_need_the_flag(self):
        listing = ("80000000 <f>:\n80000000: 30529073 csrw mtvec, t0\n80000004: 30200073 mret\n"
                   "80000008: 00000073 ecall\n8000000c: 30002573 csrr a0, mstatus\n")
        self.assertEqual(len(check_listing(listing)), 4)
        problems = check_listing(listing, allow_privileged=True)
        self.assertEqual(len(problems), 2)
        self.assertIn("ecall", problems[0])
        self.assertIn("CSR other than the four trap CSRs", problems[1])
        if (ROOT / "build/rv32/diag.lst").exists():
            self.assertEqual(check_listing((ROOT / "build/rv32/diag.lst").read_text(), allow_privileged=True), [])

    def test_pong_script_expected_file_and_makefile_agree(self):
        """The Pong session's script parses and ends with a Q press; the expected file has one
        well-formed checkpoint line per frame up to that Q; the Makefile's replay arguments name both
        files. The hashes themselves are checked by test_rv32_pong.py against the native build."""
        script = (ROOT / "programs/rv32/pong.input").read_text()
        events = parse_input_script(script)
        self.assertEqual(events[-1], (200, EVENT_VALID | EVENT_PRESS | KEYS["Q"]))
        self.assertLessEqual(max(sum(1 for f, _ in events if f == frame) for frame, _ in events), 2,
                             "at most two events a frame keeps the queue far from full")
        expected = (ROOT / "programs/rv32/pong.expected").read_text().splitlines()
        for line in expected:
            self.assertRegex(line, r"^frame \d+ [0-9a-f]{8}$")
        self.assertEqual([line.split(" ")[1] for line in expected], [str(n) for n in range(1, 201)], "one line per frame, in order")
        makefile = (ROOT / "Makefile").read_text()
        self.assertRegex(makefile, re.compile(r"^RV32_PONG_HEX := [0-9a-f]{8}$", re.M))
        self.assertIn("RV32_PONG_ARGS := --image build/rv32/pong.bin --input $(RV32_PONG_INPUT) "
                      "--expect-last-line \"PASS $(RV32_PONG_HEX)\" --expect-checkpoints $(RV32_PONG_EXPECTED)", makefile)
        self.assertIn("RV32_PONG_INPUT := programs/rv32/pong.input", makefile)
        self.assertIn("RV32_PONG_EXPECTED := programs/rv32/pong.expected", makefile)

    def test_simd_register_contract_constants(self):
        from tools import rv32_asm as asm
        expected = dict(COMMAND=0,STATUS=4,ENTRY=8,CYCLES=12,STALLS=16,TRANSFERS=20,INSTRUCTIONS=24,
                        BUSY=1,DONE=2,FAULT=4,START=1,RESET=2)
        for filename,prefix in [('programs/rv32/board.h','RV32_SIMD4_'),('tools/rv32_simd4.h','SIMD_')]:
            source = (ROOT/filename).read_text()
            for name,value in expected.items():
                match = re.search(rf'#define {prefix}{name}\s+0x([0-9a-f]+)\b',source)
                self.assertIsNotNone(match,(filename,name))
                self.assertEqual(int(match[1],16),value,(filename,name))
        rtl = (ROOT/'rtl/rv32/rv32_simd4.v').read_text()
        for name,value in expected.items():
            self.assertEqual(getattr(asm,'SIMD_'+name),value)
            match = re.search(rf"SIMD4_{name} = (?:5|32)'h([0-9a-f]+);",rtl)
            self.assertIsNotNone(match,name)
            self.assertEqual(int(match[1],16),value,name)
        self.assertEqual((asm.SIMD_BASE,asm.SIMD_PROGRAM,asm.SIMD_DATA),
                         (0x11004000,0x11005000,0x11006000))

    def test_gpu_register_contract_constants(self):
        from tools import rv32_asm as asm
        expected=dict(COMMAND=0,STATUS=4,ERROR=8,CYCLES=12,STALLS=16,READS=20,WRITES=24,
                      PARAMS=64,IDLE=0,BUSY=1,DONE=2,FAULT=4,START=1,RESET=2,
                      INVALID=1,INTERNAL=2,FILL=1,BLIT=2,LINE=3,TRIANGLE=4)
        header=(ROOT/'programs/rv32/gpu.h').read_text()
        rtl=(ROOT/'rtl/rv32/rv32_gpu.v').read_text()
        for name,value in expected.items():
            self.assertEqual(getattr(asm,'GPU_'+name),value,name)
            match=re.search(rf'#define GPU_{name} (0x[0-9a-f]+|[0-9]+)u',header)
            self.assertIsNotNone(match,name);self.assertEqual(int(match[1],0),value,name)
            match=re.search(rf"GPU_{name} = 32'h([0-9a-f]+);",rtl)
            self.assertIsNotNone(match,name);self.assertEqual(int(match[1],16),value,name)
        self.assertEqual(asm.GPU_BASE,0x11007000)

    def test_key_table_and_windows_agree_across_languages(self):
        """The key table lives in board.h, the emulator, the window, the testbench, and this module's KEYS; the
        window bases in board.h, the bus, the machine's memory instances, and the assembler. None of
        the copies is parsed by the others, so this test pins them to each other."""
        header = (ROOT / "programs/rv32/board.h").read_text()
        emulator = (ROOT / "tools/rv32emu_core.c").read_text()
        testbench = (ROOT / "tests/rv32_tb.sv").read_text()
        expected = {name: str(code) for name, code in KEYS.items()}
        self.assertEqual(dict(re.findall(r"#define RV32_KEY_(\w+)\s+(\d+)", header)), expected, "board.h")
        self.assertEqual(dict(re.findall(r'\{"(\w+)", (\d+)\}', emulator)), expected, "rv32emu_core.c")
        self.assertEqual(dict(re.findall(r'\(u == "(\w+)"\) key_code = (\d+);', testbench)), expected, "rv32_tb.sv")
        window = (ROOT / "tools/rv32win.c").read_text()
        host_names = {"RETURN": "ENTER"}  # SDL names the key by its keycap
        keymap = {host_names.get(name, name): code for name, code in re.findall(r"\{SDL_SCANCODE_(\w+), (\d+)\}", window)}
        self.assertEqual(keymap, expected, "rv32win.c")
        self.assertRegex(header, rf"#define RV32_INPUT_QUEUE\s+{QUEUE_SIZE}\b")
        self.assertRegex(header, rf"#define RV32_EVENT_VALID\s+{EVENT_VALID:#010x}\b")
        self.assertRegex(header, rf"#define RV32_EVENT_PRESS\s+{EVENT_PRESS:#010x}\b")
        self.assertIn(f"32'h{EVENT_VALID:08x} | ((token2 == \"down\") ? 32'h{EVENT_PRESS:x} : 32'h0)".replace("8000_0000", "80000000"),
                      testbench.replace("8000_0000", "80000000"))
        bases = {"RAM": RAM, "CONSOLE": CONSOLE, "DONE": DONE, "CLINT": CLINT, "PLIC": PLIC, "VIRTIO": VIRTIO, "BOOTROM": BOOTROM, "INPUT": INPUT, "DISPLAY": DISPLAY, "FB": FB,
                 "SIMD4": 0x11004000, "SIMD4_PROGRAM": 0x11005000, "SIMD4_DATA": 0x11006000, "GPU": 0x11007000,
                 "G3D": 0x11008000, "DMA_WINDOW": 0x1100A000}
        header_bases = {name: int(value, 16) for name, value in re.findall(r"#define RV32_(\w+)_BASE\s+0x([0-9a-fA-F]+)", header)}
        self.assertEqual(header_bases, {name: bases[name] for name in header_bases}, "board.h")
        self.assertLessEqual({"CLINT", "PLIC", "BOOTROM", "INPUT", "DISPLAY", "FB"}, set(header_bases))
        bus = (ROOT / "rtl/rv32/rv32_bus.v").read_text()
        bus_bases = {name.replace("_BASE", "").replace("_ADDR", ""): int(value.replace("_", ""), 16)
                     for name, value in re.findall(r"localparam \[31:0\] (\w+) = 32'h([0-9a-fA-F_]+);", bus)}
        self.assertEqual(bus_bases, bases, "rv32_bus.v")
        self.assertRegex((ROOT/"programs/rv32/gpu.h").read_text(), r"#define GPU_BASE 0x11007000u\b")
        wrapper = (ROOT / "rtl/rv32/rv32_simd4.v").read_text()
        simd_header = (ROOT / "tools/rv32_simd4.h").read_text()
        for name, value in (("BASE", 0x11004000), ("PROGRAM", 0x11005000), ("DATA", 0x11006000)):
            self.assertRegex(header, rf"#define RV32_SIMD4_{name}\s+0x{value:08x}\b")
            self.assertRegex(simd_header, rf"#define SIMD_{name}\s+0x{value:08x}u\b")
            self.assertIn(f"SIMD4_{name} = 32'h{value:08x}", wrapper.replace("1100_", "1100"))
        from tools import rv32_dtb
        tree = {"RAM": rv32_dtb.RAM_BASE, "CONSOLE": rv32_dtb.CONSOLE_BASE, "DONE": rv32_dtb.DONE_BASE,
                "CLINT": rv32_dtb.CLINT_BASE, "PLIC": rv32_dtb.PLIC_BASE, "VIRTIO": rv32_dtb.VIRTIO_BASE, "BOOTROM": rv32_dtb.ROM_BASE, "INPUT": rv32_dtb.INPUT_BASE,
                "DISPLAY": rv32_dtb.DISPLAY_BASE, "FB": rv32_dtb.FB_BASE, "SIMD4": rv32_dtb.SIMD4_BASE,
                "SIMD4_PROGRAM": rv32_dtb.SIMD4_PROGRAM, "SIMD4_DATA": rv32_dtb.SIMD4_DATA,
                "GPU": rv32_dtb.GPU_BASE, "G3D": rv32_dtb.G3D_BASE, "DMA_WINDOW": rv32_dtb.DMA_WINDOW_BASE}
        self.assertEqual(tree, bases, "rv32_dtb.py describes the same windows")
        machine = (ROOT / "rtl/rv32/rv32_soc.v").read_text()
        instances = re.findall(r"rv32_ram #\(\.WORDS\((\w+)\), \.BASE\((\w+)\)\)", machine)
        self.assertEqual(instances, [("RAM_WORDS", "RAM_BASE"), ("FB_WORDS", "FB_BASE")])
        soc_bases = {name.replace("_BASE", ""): int(value.replace("_", ""), 16)
                     for name, value in re.findall(r"localparam \[31:0\] (\w+_BASE) = 32'h([0-9a-fA-F_]+);", machine)}
        self.assertEqual(soc_bases, {name: bases[name] for name in ("RAM", "FB", "BOOTROM")},
                         "rv32_soc.v's memories and the core's reset a1 use the bus's bases")
        self.assertIn("rv32 #(.BOOT_A1(BOOTROM_BASE)) core (", machine)

    def test_window_bases_and_sizes_agree_everywhere(self):
        """Review on PR #18: the map check proves our windows avoid virt's using the device tree's
        sizes, so every other copy of a window must have the same base and size: the bus decoder's
        comparators, the emulator's region table and its headers, and the CLINT's register offsets
        in the RTL, the emulator, board.h and rv32_asm.py."""
        from tools import rv32_dtb
        expected = {base: size for _, base, size in rv32_dtb.regions(rv32_dtb.MACHINE)}
        expected[rv32_dtb.ROM_BASE] = rv32_dtb.ROM_SIZE

        # The bus: one select per window; its size is what the comparator leaves free.
        bus = (ROOT / "rtl/rv32/rv32_bus.v").read_text()
        params = {name: int(value.replace("_", ""), 16)
                  for name, value in re.findall(r"localparam \[31:0\] (\w+) = 32'h([0-9a-fA-F_]+);", bus)}
        words = {name: int(value) for name, value in re.findall(r"parameter integer (\w+) = (\d+)", bus)}
        decoded = {}
        selects = re.findall(r"wire (\w+)_sel = (.*?);", bus, re.S)
        self.assertEqual(len(selects), 15, "one select per window, plus none_sel")
        for select, expr in selects:
            if select == "none":
                continue
            for bits, name in re.findall(r"mem_addr\[31:(\d+)\]\s*==\s*(\w+)\[31:\1\]", expr):
                decoded[params[name]] = 1 << int(bits)
                # The PLIC's 6 MiB: the top quarter of the 8 MiB block is excluded (O1).
                for high, low in re.findall(r"mem_addr\[(\d+):(\d+)\] != 2'b11", expr):
                    self.assertEqual(int(high), int(bits) - 1)
                    decoded[params[name]] = 3 << int(low)
            for mask, name in re.findall(r"\(mem_addr & 32'h([0-9a-fA-F_]+)\)\s*==\s*(\w+)", expr):
                decoded[params[name]] = (~int(mask.replace("_", ""), 16) + 1) & 0xFFFFFFFF
            for name in re.findall(r"mem_addr\s*==\s*(\w+)\)", expr):
                decoded[params[name]] = 4
            for name, limit in re.findall(r"mem_addr >= (\w+)\) && \(\w+_offset < (\w+)\)", expr):
                decoded[params[name]] = {"RAM_BYTES": words["RAM_WORDS"], "FB_BYTES": words["FB_WORDS"]}[limit] * 4
        self.assertEqual(decoded, expected, "rv32_bus.v's comparators")

        # The emulator: its region table, with every macro resolved from the headers it includes.
        defines = {}
        for header in ("tools/rv32emu_core.h", "tools/rv32_simd4.h", "programs/rv32/gpu.h", "programs/rv32/g3d.h",
                       "tools/rv32_dtb.h"):
            for name, value in re.findall(r"^#define (\w+) +([^/\n]+?)\s*(?:/\*.*)?$", (ROOT / header).read_text(), re.M):
                defines[name] = value

        def evaluate(text):
            text = re.sub(r"\b(0x[0-9a-fA-F]+|\d+)u\b", r"\1", text)
            text = re.sub(r"\b[A-Z_][A-Z0-9_]*\b", lambda m: f"({evaluate(defines[m[0]])})", text)
            return eval(text, {"__builtins__": {}})  # noqa: S307 (constant arithmetic from our own headers)
        core = (ROOT / "tools/rv32emu_core.c").read_text()
        table = core[core.index("static const region REGIONS[] = {"):]
        table = table[:table.index("};")]
        regions = {evaluate(base): evaluate(size)
                   for _, base, size in re.findall(r'\{"(\w+)", (\w+), (\w+),', table)}
        self.assertEqual(regions, expected, "rv32emu_core.c's REGIONS")

        # The CLINT's registers.
        clint = (ROOT / "rtl/rv32/rv32_clint.v").read_text()
        offsets = {name: int(value, 16) for name, value in re.findall(r"(MSIP|MTIMECMP|MTIME) = 16'h([0-9a-f]+)", clint)}
        from tools import rv32_asm
        board = (ROOT / "programs/rv32/board.h").read_text()
        for name, offset in offsets.items():
            with self.subTest(name):
                self.assertRegex(board, rf"#define RV32_CLINT_{name}\s+0x0*{offset:x}\b")
                self.assertEqual(evaluate(f"CLINT_{name}"), offset, "rv32emu_core.h")
                self.assertEqual(getattr(rv32_asm, name) - rv32_asm.CLINT, offset, "rv32_asm.py")
        self.assertEqual(set(offsets), {"MSIP", "MTIMECMP", "MTIME"})

        # The PLIC's registers (O1): the RTL's offsets, board.h, the emulator's header and rv32_asm.py.
        plic = (ROOT / "rtl/rv32/rv32_plic.v").read_text()
        offsets = {name: int(value.replace("_", ""), 16)
                   for name, value in re.findall(r"(PENDING|ENABLE|THRESHOLD|CLAIM) = 23'h([0-9a-f_]+)", plic)}
        self.assertEqual(set(offsets), {"PENDING", "ENABLE", "THRESHOLD", "CLAIM"})
        for name, offset in offsets.items():
            with self.subTest(name):
                self.assertRegex(board, rf"#define RV32_PLIC_{name}\s+0x0*{offset:x}\b")
                self.assertEqual(evaluate(f"PLIC_{name}"), offset, "rv32emu_core.h")
                self.assertEqual(getattr(rv32_asm, f"PLIC_{name}") - rv32_asm.PLIC, offset, "rv32_asm.py")
        self.assertIn(f"32'h{1 << rv32_asm.PLIC_SOURCE_INPUT:08x}".replace("00001000", "0000_1000"), plic)
        wired = (1 << rv32_asm.PLIC_SOURCE_INPUT) | (1 << rv32_asm.PLIC_SOURCE_VIRTIO)
        self.assertIn(f"rv32_plic #(.WIRED(32'h{wired >> 16:04x}_{wired & 0xffff:04x}))", (ROOT / "rtl/rv32/rv32_soc.v").read_text())
        self.assertEqual(evaluate("PLIC_WIRED"), wired, "rv32emu_core.h")
        self.assertEqual(evaluate("VIRTIO_DISK_SIZE"), rv32_asm.VIRTIO_DISK_SIZE)
        self.assertIn(f"parameter integer DISK_WORDS = {rv32_asm.VIRTIO_DISK_SIZE // 4}", (ROOT / "rtl/rv32/rv32_soc.v").read_text())
        self.assertRegex(board, rf"#define RV32_PLIC_SOURCE_INPUT\s+{rv32_asm.PLIC_SOURCE_INPUT}\b")
        self.assertEqual(evaluate("PLIC_SOURCE_INPUT"), rv32_asm.PLIC_SOURCE_INPUT)
        self.assertEqual(rv32_dtb.INPUT_IRQ, rv32_asm.PLIC_SOURCE_INPUT)
        soc = (ROOT / "rtl/rv32/rv32_soc.v").read_text()
        self.assertIn(f".lines({{{31 - rv32_asm.PLIC_SOURCE_INPUT}'d0, input_nonempty, {rv32_asm.PLIC_SOURCE_INPUT - 2}'d0, virtio_irq, 1'b0}})", soc)



class TestHarnessTests(unittest.TestCase):
    """The timing SHELL and the shard runner of issue #26 (docs/rv32-testing.md)."""
    SHELL = [sys.executable, str(ROOT / "tools/rv32_recipe_shell.py")]
    SHARDS = [sys.executable, str(ROOT / "tools/rv32_unittest_shards.py")]

    def test_a_shell_call_without_a_target_runs_untouched(self):
        # $(shell ...) outside a recipe: make passes `-c COMMAND` alone, and the value must be exact.
        result = subprocess.run([*self.SHELL, "-c", "echo hi; exit 3"], capture_output=True, text=True)
        self.assertEqual((result.stdout, result.returncode), ("hi\n", 3))

    def test_a_recipe_line_is_framed_logged_and_keeps_its_status(self):
        with tempfile.TemporaryDirectory() as directory:
            log = Path(directory) / "build" / "timing.jsonl"  # its directory does not exist yet
            env = {"PATH": "/usr/bin:/bin", "RV32_TIMING": "1", "MAKEFLAGS": "s -- RV32_TIMING=1 X=2",
                   "RV32_TIMING_LOG": str(log)}
            command = 'echo "[$RV32_TIMING] [$MAKEFLAGS]"; exit 3'
            result = subprocess.run([*self.SHELL, "demo", "-c", command], capture_output=True, text=True, env=env)
            self.assertEqual(result.returncode, 3)
            # The line runs without RV32_TIMING, so a make it starts itself is not wrapped again.
            self.assertRegex(result.stdout, r"\A>> demo: .*\n\[\] \[s -- X=2\]\n<< demo: [\d.]+ s FAILED \(exit 3\)\n\Z")
            record = json.loads(log.read_text())
            self.assertEqual((record["target"], record["command"], record["status"]), ("demo", command, 3))
            self.assertLessEqual(record["start"], record["end"])

    def test_the_report_keys_targets_by_directory(self):
        # Verilator's generated makefiles reuse names like verilated.o in every build directory.
        with tempfile.TemporaryDirectory() as directory:
            here = Path(directory).resolve()
            log = here / "timing.jsonl"
            records = [("verilated.o", here, 0.0, 2.0, 0), ("verilated.o", here / "other", 10.0, 11.0, 0),
                       ("check", here, 2.0, 5.0, 1)]
            log.write_text("".join(json.dumps({"target": t, "cwd": str(c), "command": "x", "start": s, "end": e,
                                               "status": st}) + "\n" for t, c, s, e, st in records))
            result = subprocess.run([*self.SHELL, "--report", str(log)], capture_output=True, text=True, cwd=here,
                                    check=True)
        rows = [line for line in result.stdout.splitlines() if line.startswith("| `")]
        self.assertEqual(rows, ["| `check` (failed) | 3.0 | 1 |", "| `verilated.o` | 2.0 | 1 |",
                                "| `other/verilated.o` | 1.0 | 1 |"])
        # The nested line runs inside one of ours, so only the top-level lines count as recipe time.
        self.assertIn("3 targets; 5 s of recipe time in 11 s of wall time", result.stdout)

    def test_the_shard_verdict(self):
        from tools.rv32_unittest_shards import verdict
        self.assertIsNone(verdict(5, [(1, 0, 3), (2, 0, 2)]))
        self.assertEqual(verdict(5, [(1, 1, 3), (2, 0, 2), (3, 2, 0)]), "shard(s) 1, 3 failed")
        self.assertEqual(verdict(5, [(1, 0, 3), (2, 0, 1)]), "the shards ran 4 tests but discovery found 5")

    def test_shards_run_every_test_and_report_a_failure(self):
        with tempfile.TemporaryDirectory() as directory:
            module = Path(directory) / "test_sharded.py"
            body = "import unittest\nclass T(unittest.TestCase):\n" + "".join(
                f"    def test_{n}(self): pass\n" for n in range(5))
            module.write_text(body)
            passed = subprocess.run([*self.SHARDS, "--shards", "2", "-s", directory, "test_sharded.py"],
                                    capture_output=True, text=True)
            self.assertEqual(passed.returncode, 0, passed.stdout + passed.stderr)
            self.assertIn("all 5 tests passed in 2 shard(s)", passed.stdout)
            module.write_text(body + "    def test_bad(self): self.fail('no')\n")
            failed = subprocess.run([*self.SHARDS, "--shards", "2", "-s", directory, "test_sharded.py"],
                                    capture_output=True, text=True)
            self.assertNotEqual(failed.returncode, 0)
            self.assertRegex(failed.stderr, r"shard\(s\) \d failed")



class VendoredSourcesTest(unittest.TestCase):
    """Track 3: picolibc, compiler-rt and Lua match their SHA256SUMS.json manifests, as SoftFloat
    and MNIST are checked against theirs (docs/rv32-libc.md)."""

    DIRECTORIES = ("third_party/picolibc", "third_party/compiler-rt", "third_party/lua")

    def test_each_directory_matches_its_manifest(self):
        root = Path(__file__).resolve().parents[1]
        for directory in self.DIRECTORIES:
            with self.subTest(directory):
                self.assertEqual(rv32_vendor_libc.verify(str(root / directory)), [])

    def test_a_change_is_found(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "a.c").write_text("int a;\n")
            (root / "b.c").write_text("int b;\n")
            (root / "SOURCES").write_text("a.c\nb.c\nc.c\n")
            rv32_vendor_libc.write_sums(str(root), {"a.c", "b.c", "SOURCES"})
            self.assertEqual(rv32_vendor_libc.verify(str(root)), ["c.c: in SOURCES, not in SHA256SUMS.json"])
            (root / "a.c").write_text("int a = 1;\n")
            (root / "b.c").unlink()
            (root / "extra.h").write_text("")
            self.assertEqual(sorted(rv32_vendor_libc.verify(str(root))),
                             ["a.c: SHA-256 differs from SHA256SUMS.json", "b.c: missing",
                              "c.c: in SOURCES, not in SHA256SUMS.json", "extra.h: not in SHA256SUMS.json"])


class FloatingClaimTest(unittest.TestCase):
    """Issue #33: the results comparison leaves out exactly the illegal-instruction traps that a
    lazy FPU switch runs again: F instructions and the floating CSRs, not other illegal words."""

    def test_floating_words(self):
        for word in (flw(1, 2), fsw(1, 2), arithmetic(0, 0), arithmetic(3, 0), arithmetic(4, 0), arithmetic(5, 0),
                     arithmetic(6, 0), arithmetic(13, 0), CSRRS(5, 1, 0), CSRRWI(0, 2, 1), CSRRC(0, 3, 6)):
            self.assertTrue(floating_word(word), f"{word:08x}")
        for word in (CSRRS(5, MSTATUS, 0), CSRRS(5, 0xC00, 0), 0x00000000, 0xFFFFFFFF, ADDI(1, 1, 1), LW(1, 2, 0),
                     i_type(0x73, 0, 0, 0, 1), i_type(0x73, 0, 4, 0, 1)):  # ecall-shaped and the hypervisor funct3
            self.assertFalse(floating_word(word), f"{word:08x}")

    def test_only_a_claim_the_process_comes_back_from_is_left_out(self):
        fadd, flw_word, illegal = 0x00208053, 0x00012087, 0xC0001073  # fadd.s, flw, csrw cycle
        trace = [
            "1 80400000 00000013",
            f"2 80400004 {fadd:08x} trap 2 {fadd:08x}",    # a claim: the kernel runs it again
            "3 80000100 34202573",                           # the kernel, in its own region
            f"4 80400004 {fadd:08x} f1=00000000",
            f"5 80420010 {flw_word:08x} trap 2 {flw_word:08x}",  # a claim, then killed for the same word
            f"6 80420010 {flw_word:08x} trap 2 {flw_word:08x}",
            f"7 80440020 {illegal:08x} trap 2 {illegal:08x}",    # not an F instruction
        ]
        groups = trap_records_by_region(trace, faults_only=True)
        self.assertNotIn(0x80400000 >> 17, groups)
        self.assertEqual(groups[0x80420000 >> 17], [f"80420010 {flw_word:08x} trap 2 {flw_word:08x}"])
        self.assertEqual(groups[0x80440000 >> 17], [f"80440020 {illegal:08x} trap 2 {illegal:08x}"])
        self.assertEqual(len(trap_records_by_region(trace)[0x80420000 >> 17]), 2, "all of them without faults_only")


if __name__ == "__main__":
    unittest.main()
