"""Unit tests for the RV32 image checker and QEMU driver; no cross toolchain needed."""

from pathlib import Path
import re
import struct
import subprocess
import sys
import tempfile
import unittest

from tools.rv32_devices import (DIAG_EXPECTED_VALUES, EVENT_PRESS, EVENT_VALID, FB_SIZE, diag_checksum, event_word,
                                frame_hash, key_code, parse_input_script, render_diag_frame)
from tools.rv32_image import (ImageError, check_image, check_listing, flatten, parse_elf,
                              to_hex_words)
from tools.rv32_run_qemu import classify, qemu_command


ROOT = Path(__file__).resolve().parents[1]
RAM = 0x80000000
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

    def test_rejects_flags_and_entry(self):
        self.assertTrue(any("RVC" in p for p in self.problems(flags=0x1)))
        self.assertTrue(any("float ABI" in p for p in self.problems(flags=0x4)))
        self.assertTrue(any("entry point" in p for p in self.problems(entry=RAM + 4)))
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
        self.assertEqual(check_listing("  /* mul by hand */\n; div in a comment\nadd a0, a0, a1"), [])

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
        self.assertEqual(command[command.index("-m") + 1], "4M")
        self.assertEqual(command[command.index("-monitor") + 1], "none")
        self.assertNotIn("-d", command)
        self.assertIn("-D", qemu_command("q", "fw.elf", log="q.log"))

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


if __name__ == "__main__":
    unittest.main()


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
        for bad in ("32", "-1", "shift", ""):
            with self.assertRaises(ValueError):
                key_code(bad)
        with self.assertRaises(ValueError):
            event_word(True, 32)

    def test_input_script_grammar(self):
        text = "# a comment\n\nframe 0 down LEFT\nframe 0 up left\n  frame 3 down 5 \nframe 3 up Q\n"
        self.assertEqual(parse_input_script(text),
                         [(0, event_word(True, 1)), (0, event_word(False, 1)), (3, event_word(True, 5)),
                          (3, event_word(False, 13))])
        self.assertEqual(parse_input_script(""), [])
        for bad in ("frame 1 down", "frame x down A", "frame 1 press A", "frame 2 down A\nframe 1 up A",
                    "frame 1 down NOPE", "frame -1 down A", "key 1 down A", "frame 1 down A extra"):
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
