"""Track 1: the virt-compatible platform (docs/rv32-platform.md).

The device-tree generator round-trips and its committed copies are current;
the virt map checker accepts our map and rejects the pre-Track-1 one; and the
firmware's FDT reader (programs/rv32/fdt.c, built natively under the address
sanitizer) finds the same devices as the Python parser on our tree and on
QEMU's, and refuses malformed blobs without reading past them.
"""
from pathlib import Path
import os
import shutil
import struct
import subprocess
import sys
import tempfile
import unittest

from tools import rv32_dtb, rv32_virt_map

ROOT = Path(__file__).resolve().parents[1]
QEMU = os.environ.get("QEMU_RV32", "qemu-system-riscv32")
# QEMU 8.2.2's own tree for `-M virt -bios none -m 4M`, dumped with `-M virt,dumpdtb=`, so the map
# checker and the reader's two-cell path are tested without QEMU installed.
VIRT_FIXTURE = ROOT / "tests" / "fixtures" / "qemu-8.2.2-virt-4M.dtb"
HEADER_FIELDS = ("magic", "totalsize", "off_dt_struct", "off_dt_strings", "off_mem_rsvmap", "version",
                 "last_comp_version", "boot_cpuid_phys", "size_dt_strings", "size_dt_struct")


def live_virt_blob():
    if not shutil.which(QEMU):
        raise unittest.SkipTest(f"{QEMU} not found")
    return rv32_virt_map.dump_virt(QEMU)


def machine_blob():
    return rv32_dtb.build(rv32_dtb.MACHINE)


def variant(edit):
    """Our blob rebuilt after `edit` has changed a private copy of the tree."""
    tree = rv32_dtb.parse(machine_blob())
    edit(tree)
    return rv32_dtb.build(tree)


def soc_node(tree, name):
    return next(n for n in tree.child("soc").children if n.name.split("@")[0] == name)


def with_header(blob, **fields):
    """The blob with header fields replaced by name."""
    header = list(struct.unpack(">10I", blob[:40]))
    for name, value in fields.items():
        header[HEADER_FIELDS.index(name)] = value & 0xFFFFFFFF
    return struct.pack(">10I", *header) + blob[40:]


def header(blob, name):
    return struct.unpack(">10I", blob[:40])[HEADER_FIELDS.index(name)]


def with_word(blob, offset, value):
    return blob[:offset] + struct.pack(">I", value & 0xFFFFFFFF) + blob[offset + 4:]


def first_prop(blob):
    """Offset of the first PROP token: the root's BEGIN_NODE and empty name take two words."""
    return header(blob, "off_dt_struct") + 8


def with_nop_after_first_prop(blob):
    """An FDT_NOP token inserted between the root's first and second properties."""
    at = first_prop(blob)
    length = struct.unpack(">I", blob[at + 4:at + 8])[0]
    after = at + 12 + (length + 3) // 4 * 4
    grown = blob[:after] + struct.pack(">I", 4) + blob[after:]
    return with_header(grown, totalsize=header(blob, "totalsize") + 4,
                       off_dt_strings=header(blob, "off_dt_strings") + 4,
                       size_dt_struct=header(blob, "size_dt_struct") + 4)


def nested(depth):
    """Our tree with a chain of `depth` empty nodes hung first under /soc (itself at depth 1), so a
    walk to any device passes through it."""
    def edit(tree):
        parent = tree.child("soc")
        for level in range(depth):
            child = rv32_dtb.Node(f"n{level}")
            if level == 0:
                parent.children.insert(0, child)
            else:
                parent.children.append(child)
            parent = child
    return variant(edit)


def unterminated_node_name():
    """A structure block that ends inside a node name: BEGIN_NODE, then "abcd" with no NUL."""
    body = struct.pack(">I", 1) + b"abcd"
    return struct.pack(">10I", 0xD00DFEED, 56 + len(body), 56, 56 + len(body), 40, 17, 16, 0, 0, len(body)) + \
        bytes(16) + body


def expected_answer(tree, compatible, index):
    """What fdt_find must print for the first node, in tree order, whose compatible list names
    `compatible`, decoded with its parent's cells, mirroring the contract in fdt.h."""
    def walk(node, parent_cells, is_root):
        if compatible in rv32_dtb.strings_of(node.props.get("compatible", b"")):
            return node, parent_cells, is_root
        cells = tuple(rv32_dtb.cells(node.props[k])[0] if k in node.props else d
                      for k, d in (("#address-cells", 2), ("#size-cells", 1)))
        for child in node.children:
            found = walk(child, cells, False)
            if found:
                return found
        return None

    found = walk(tree, (2, 1), True)
    if not found:
        return "error 4"
    node, (ac, sc), is_root = found
    if is_root:
        return "error 6"
    if ac > 2 or sc > 2:
        return "error 5"
    entry = ac + sc
    words = rv32_dtb.cells(node.props["reg"]) if "reg" in node.props else []
    if entry == 0 or not words or len(words) % entry:
        return "error 6"
    if index >= len(words) // entry:
        return "error 4"
    at = index * entry

    def number(cells_):
        value = 0
        for w in cells_:
            value = value << 32 | w
        return value
    base, size = number(words[at:at + ac]), number(words[at + ac:at + entry])
    if base >> 32 or size >> 32:
        return "error 5"
    return f"{base:08x} {size:08x}"


def all_compatibles(tree):
    out = []

    def visit(node):
        for name in rv32_dtb.strings_of(node.props.get("compatible", b"")):
            if name not in out:
                out.append(name)
        for child in node.children:
            visit(child)
    visit(tree)
    return out


class GeneratorTest(unittest.TestCase):
    def test_round_trip_and_regions(self):
        blob = rv32_dtb.build(rv32_dtb.MACHINE)
        self.assertLessEqual(len(blob), rv32_dtb.ROM_SIZE)
        self.assertEqual(rv32_dtb.parse(blob), rv32_dtb.MACHINE)
        regions = {(base, size) for _, base, size in rv32_dtb.regions(rv32_dtb.parse(blob))}
        for window in [(0x8000_0000, 0x40_0000), (0x0010_0000, 4), (0x1000_0000, 8), (0x0200_0000, 0x1_0000),
                       (0x1100_1000, 16), (0x1100_2000, 16), (0x1200_0000, 76800), (0x1100_4000, 32),
                       (0x1100_5000, 1024), (0x1100_6000, 1024), (0x1100_7000, 128), (0x1100_8000, 0x2000)]:
            self.assertIn(window, regions)

    def test_committed_copies_are_current(self):
        result = subprocess.run([sys.executable, "tools/rv32_dtb.py", "--check"], cwd=ROOT, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        rom = (ROOT / "rtl/rv32/rv32_bootrom.v").read_text()
        words = rv32_dtb.words(rv32_dtb.build(rv32_dtb.MACHINE))
        self.assertIn(f"10'd0: word = 32'h{words[0]:08x};", rom)
        self.assertEqual(words[0], 0xEDFE0DD0, "the magic d00dfeed, read as a little-endian word")

    def test_dtc_agrees(self):
        """An independent parser, when installed: dtc decompiles our blob without complaint."""
        if not shutil.which("dtc"):
            self.skipTest("dtc not found")
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "machine.dtb"
            path.write_bytes(rv32_dtb.build(rv32_dtb.MACHINE))
            result = subprocess.run(["dtc", "-I", "dtb", "-O", "dts", str(path)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("Warning", result.stderr)
        self.assertIn('compatible = "tiny-processors,rv32-machine";', result.stdout)
        self.assertIn("reg = <0x12000000 0x12c00>", result.stdout.replace("0x11002000 0x10 ", ""))

    def test_parser_rejects_malformed_blobs(self):
        blob = rv32_dtb.build(rv32_dtb.MACHINE)
        for name, bad in (("magic", b"\0" + blob[1:]), ("short", blob[:20]),
                          ("version", blob[:20] + struct.pack(">I", 15) + blob[24:]),
                          ("token", blob[:56] + struct.pack(">I", 7) + blob[60:]),
                          ("structure past the end", with_header(blob, size_dt_struct=len(blob))),
                          ("strings past the end", with_header(blob, size_dt_strings=len(blob))),
                          ("misaligned structure", with_header(blob, off_dt_struct=header(blob, "off_dt_struct") + 2)),
                          ("unterminated node name", unterminated_node_name()),
                          ("value past the structure", with_word(blob, first_prop(blob) + 4, 0xFFFFFFFC)),
                          ("name outside the strings", with_word(blob, first_prop(blob) + 8, 0x7FFFFFFF)),
                          ("unterminated name", unterminated_last_name(blob))):
            with self.subTest(name), self.assertRaises(ValueError):
                rv32_dtb.parse(bad)

    def test_regions_refuse_partial_entries(self):
        def ragged(tree):
            soc_node(tree, "input").props["reg"] = rv32_dtb.u32(0x11001000, 16, 0x11002000)
        with self.assertRaisesRegex(ValueError, "input@11001000: reg is not a whole number"):
            rv32_dtb.regions(rv32_dtb.parse(variant(ragged)))

    def test_rom_header_names_the_rom_address(self):
        """Review on PR #18: the generated header said 0x1000_1000, virt's first virtio slot."""
        rom = (ROOT / "rtl/rv32/rv32_bootrom.v").read_text()
        self.assertIn("at 0x0000_1000 in a 4 KiB window", rom)
        self.assertIn("case (addr[11:2])", rom)


def moved(tree, name, base):
    """A copy of our tree with node `name` of /soc moved to `base`."""
    blob = rv32_dtb.build(tree)
    copy = rv32_dtb.parse(blob)
    soc = copy.child("soc")
    for n in soc.children:
        if n.name.split("@")[0] == name:
            values = rv32_dtb.cells(n.props["reg"])
            values[0] = base
            n.props["reg"] = rv32_dtb.u32(*values)
    return copy


class VirtMapTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.virt = rv32_dtb.parse(VIRT_FIXTURE.read_bytes())

    def test_our_map_fits_virt(self):
        self.assertEqual(rv32_virt_map.check(self.virt, rv32_dtb.MACHINE), [])

    def test_our_map_fits_the_installed_qemu(self):
        """The fixture is one QEMU release; the installed one is checked too when present."""
        self.assertEqual(rv32_virt_map.check(rv32_dtb.parse(live_virt_blob()), rv32_dtb.MACHINE), [])

    def test_shared_devices_must_match_and_be_present(self):
        def resized_memory(tree):
            tree.child(f"memory@{rv32_dtb.RAM_BASE:x}").props["reg"] = rv32_dtb.u32(rv32_dtb.RAM_BASE, 0x80_0000)
        problems = rv32_virt_map.check(self.virt, rv32_dtb.parse(variant(resized_memory)))
        self.assertIn("memory 0x80000000+0x800000 differs from virt's", " ".join(problems))
        problems = rv32_virt_map.check(self.virt, moved(rv32_dtb.MACHINE, "test", 0x0010_2000))
        self.assertIn("is not inside virt's sifive,test0", " ".join(problems))

        def without_console(tree):
            tree.child("soc").children.remove(soc_node(tree, "console"))
        problems = rv32_virt_map.check(self.virt, rv32_dtb.parse(variant(without_console)))
        self.assertIn("our tree has no console node", " ".join(problems))

    def test_the_pre_track_1_map_does_not(self):
        """Before Track 1 the input window sat on virt's flash; the checker must see that."""
        problems = rv32_virt_map.check(self.virt, moved(rv32_dtb.MACHINE, "input", 0x2000_1000))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("overlaps virt's /flash@20000000", problems[0])
        problems = rv32_virt_map.check(self.virt, moved(rv32_dtb.MACHINE, "clint", 0x0200_1000))
        self.assertIn("is not equal to virt's riscv,clint0", " ".join(problems))
        problems = rv32_virt_map.check(self.virt, moved(rv32_dtb.MACHINE, "g3d", 0x1000_1000))
        self.assertIn("virtio_mmio@10001000", " ".join(problems))


class PlatcheckOnQemuTest(unittest.TestCase):
    """platcheck on QEMU virt given a tree through -dtb: only 'not found' may read as absent.
    Review on PR #18: a node of ours with an unusable reg used to print 'absent' and pass."""

    ELF = ROOT / "build" / "rv32" / "platcheck.elf"

    def run_with(self, tree):
        if not shutil.which(QEMU):
            self.skipTest(f"{QEMU} not found")
        if not self.ELF.exists():
            self.skipTest("build/rv32/platcheck.elf not built (make check-rv32-platcheck-image)")
        with tempfile.TemporaryDirectory() as directory:
            dtb = Path(directory) / "tree.dtb"
            dtb.write_bytes(rv32_dtb.build(tree))
            result = subprocess.run([QEMU, "-M", "virt", "-cpu", os.environ.get("RV32_PLATFORM_QEMU_CPU", "rv32"),
                                     "-bios", "none", "-m", "4M", "-nographic", "-monitor", "none", "-no-reboot",
                                     "-dtb", str(dtb), "-kernel", str(self.ELF)],
                                    capture_output=True, text=True, timeout=60)
        return result.returncode, result.stdout.replace("\r", "")

    def virt_with(self, *nodes):
        tree = rv32_dtb.parse(VIRT_FIXTURE.read_bytes())
        tree.child("soc").children.extend(nodes)
        return tree

    def test_virt_as_dumped_passes(self):
        status, console = self.run_with(rv32_dtb.parse(VIRT_FIXTURE.read_bytes()))
        self.assertEqual(status, 0, console)
        self.assertIn("platcheck: input absent", console)

    def test_an_unusable_node_of_ours_fails(self):
        broken = rv32_dtb.Node("input@11001000", {"compatible": rv32_dtb.string("tiny-processors,input")})
        status, console = self.run_with(self.virt_with(broken))
        self.assertEqual(status, 2, console)
        self.assertIn("platcheck: FAILED input, device tree error 6", console)
        self.assertNotIn("PASS", console)

    def test_an_out_of_range_node_of_ours_fails(self):
        wide = rv32_dtb.Node("g3d@11008000", {"compatible": rv32_dtb.string("tiny-processors,g2"),
                                              "reg": rv32_dtb.u32(1, 0x11008000, 0, 0x2000)})
        status, console = self.run_with(self.virt_with(wide))
        self.assertEqual(status, 2, console)
        self.assertIn("platcheck: FAILED g2, device tree error 5", console)


def unterminated_last_name(blob):
    """The blob with the NUL of its last property name removed and the file ending right there."""
    header = list(struct.unpack(">10I", blob[:40]))
    end = header[3] + header[8]
    assert blob[end - 1] == 0 and end <= len(blob) and not any(blob[end:])  # at most the padding follows
    header[1] = end - 1  # totalsize: the file ends where the NUL was
    header[8] -= 1       # size_dt_strings
    return struct.pack(">10I", *header) + blob[40:end - 1]


class FirmwareReaderTest(unittest.TestCase):
    """programs/rv32/fdt.c on the host, compared with the Python parser."""

    @classmethod
    def setUpClass(cls):
        cc = os.environ.get("HOST_CC", "cc")
        if not shutil.which(cc):
            raise unittest.SkipTest(f"{cc} not found")
        cls.work = tempfile.TemporaryDirectory()
        cls.binary = Path(cls.work.name) / "fdt_native"
        flags = ["-std=c11", "-O1", "-g", "-Wall", "-Wextra", "-Werror", "-Iprograms/rv32"]
        sanitize = ["-fsanitize=address,undefined", "-fno-sanitize-recover=all"]
        sources = ["tests/rv32_fdt_native.c", "programs/rv32/fdt.c"]
        sanitized = subprocess.run([cc, *flags, *sanitize, "-o", str(cls.binary), *sources], cwd=ROOT,
                                   capture_output=True, text=True)
        # Without the sanitizers an out-of-bounds read usually returns a plausible byte, so the tests
        # that exist to catch one are skipped (with the compiler's reason) rather than passed by luck.
        cls.sanitizer_error = None if sanitized.returncode == 0 else sanitized.stderr
        if cls.sanitizer_error:
            subprocess.run([cc, *flags, "-o", str(cls.binary), *sources], cwd=ROOT, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def require_sanitizers(self):
        if self.sanitizer_error:
            self.skipTest("AddressSanitizer/UBSan build failed, so an out-of-bounds read could pass "
                          "unnoticed:\n" + self.sanitizer_error)

    def query(self, blob, *args):
        path = Path(self.work.name) / "tree.dtb"
        path.write_bytes(blob)
        result = subprocess.run([str(self.binary), str(path), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout.strip()

    def test_nth_node(self):
        """fdt_find_nth (O3): QEMU lists its eight virtio slots from the highest address down."""
        virt = (ROOT / "tests/fixtures/qemu-8.2.2-virt-4M.dtb").read_bytes()
        self.assertEqual(self.query(virt, "compatible", "virtio,mmio", "nth", "0", "0"), "10008000 00001000")
        self.assertEqual(self.query(virt, "compatible", "virtio,mmio", "nth", "7", "0"), "10001000 00001000")
        self.assertEqual(self.query(virt, "compatible", "virtio,mmio", "nth", "8", "0"), "error 4")
        ours = rv32_dtb.build(rv32_dtb.MACHINE)
        self.assertEqual(self.query(ours, "compatible", "virtio,mmio", "nth", "0", "0"), "10001000 00000200")
        self.assertEqual(self.query(ours, "compatible", "virtio,mmio", "nth", "1", "0"), "error 4")
        self.assertEqual(self.query(ours, "compatible", "tiny-processors,display", "nth", "0", "1"), "12000000 00012c00")

    def test_cells(self):
        """fdt_cell (O1): the input's PLIC source and the interrupt wiring, in our tree and QEMU's."""
        blob = rv32_dtb.build(rv32_dtb.MACHINE)
        cases = {("compatible", "tiny-processors,input", "interrupts", "0"): f"{rv32_dtb.INPUT_IRQ:08x}",
                 ("compatible", "tiny-processors,input", "interrupt-parent", "0"): f"{rv32_dtb.PLIC_PHANDLE:08x}",
                 ("compatible", "riscv,plic0", "riscv,ndev", "0"): "0000001f",
                 ("compatible", "riscv,clint0", "interrupts-extended", "3"): "00000007",
                 ("compatible", "riscv,clint0", "interrupts-extended", "4"): "error 4",   # past the last cell
                 ("compatible", "tiny-processors,console", "interrupts", "0"): "error 7",  # no such property
                 ("compatible", "tiny-processors,none", "interrupts", "0"): "error 4"}    # no such node
        for args, expected in cases.items():
            with self.subTest(args):
                self.assertEqual(self.query(blob, *args), expected)
        virt = (ROOT / "tests/fixtures/qemu-8.2.2-virt-4M.dtb").read_bytes()
        self.assertEqual(self.query(virt, "compatible", "ns16550a", "interrupts", "0"), "0000000a")
        self.assertEqual(self.query(virt, "compatible", "riscv,plic0", "riscv,ndev", "0"), "0000005f")

    def test_our_tree(self):
        blob = rv32_dtb.build(rv32_dtb.MACHINE)
        self.assertEqual(self.query(blob, "model"), "tiny-processors RV32 machine")
        cases = {("device_type", "memory", "0"): "80000000 00400000",
                 ("compatible", "sifive,test0", "0"): "00100000 00000004",
                 ("compatible", "tiny-processors,console", "0"): "10000000 00000008",
                 ("compatible", "riscv,clint0", "0"): "02000000 00010000",
                 ("compatible", "tiny-processors,display", "1"): "12000000 00012c00",
                 ("compatible", "tiny-processors,simd4", "2"): "11006000 00000400",
                 ("compatible", "tiny-processors,simd4", "3"): "error 4",
                 ("compatible", "ns16550a", "0"): "error 4",
                 ("device_type", "cpu", "0"): "00000000 00000000"}  # cpus: #size-cells 0, so size 0
        for args, expected in cases.items():
            with self.subTest(args):
                self.assertEqual(self.query(blob, *args), expected)

    def test_qemu_tree_with_two_cell_addresses(self):
        blob = VIRT_FIXTURE.read_bytes()
        tree = rv32_dtb.parse(blob)
        self.assertEqual(self.query(blob, "model"), rv32_dtb.strings_of(tree.props["model"])[0])
        for compatible, expected in (("ns16550a", "10000000 00000100"), ("sifive,test0", "00100000 00001000"),
                                     ("riscv,clint0", "02000000 00010000"), ("tiny-processors,input", "error 4")):
            with self.subTest(compatible):
                self.assertEqual(self.query(blob, "compatible", compatible, "0"), expected)
        self.assertEqual(self.query(blob, "device_type", "memory", "0"), "80000000 00400000")
        # A two-cell address whose high cell is zero fits in 32 bits.
        self.assertEqual(self.query(blob, "compatible", "pci-host-ecam-generic", "0"), "30000000 10000000")

    def test_every_compatible_agrees_with_the_python_parser(self):
        """Every compatible string in our tree and QEMU's, at every reg index and one past the last,
        gives what the Python model of the contract says, including 'no reg' and 'too wide'."""
        for label, blob in (("ours", machine_blob()), ("qemu", VIRT_FIXTURE.read_bytes())):
            tree = rv32_dtb.parse(blob)
            for compatible in all_compatibles(tree):
                for index in range(4):
                    with self.subTest(tree=label, compatible=compatible, index=index):
                        self.assertEqual(self.query(blob, "compatible", compatible, str(index)),
                                         expected_answer(tree, compatible, index))

    def test_huge_cells_and_indices_are_refused(self):
        """Review on PR #18: cell counts above 2 and indices past the last entry used to wrap the
        entry arithmetic, reading far outside the blob or returning a property header as a reg."""
        self.require_sanitizers()
        blob = machine_blob()

        def soc_cells(address, size):
            def edit(tree):
                tree.child("soc").props["#address-cells"] = rv32_dtb.u32(address)
                tree.child("soc").props["#size-cells"] = rv32_dtb.u32(size)
                # 16 bytes of reg, the review's reproduction: with 32-bit arithmetic the old guard
                # computed entry * (index + 1) = 0x80000008 * 2 = 0x10 and let the read through.
                soc_node(tree, "input").props["reg"] = rv32_dtb.u32(0x11001000, 16, 0x11001000, 16)
            return variant(edit)
        cases = [
            (soc_cells(1, 0xE0000001), "1", "error 5"),   # entry wrapped to 0x80000008 before the fix
            (soc_cells(1, 0xE0000001), "0", "error 5"),
            (soc_cells(3, 1), "0", "error 5"),
            (blob, "536870911", "error 4"),                # returned the property header before the fix
            (blob, str(0x1FFFFFFF), "error 4"),
            (blob, str(0x20000000), "error 4"),
            (blob, str(0xFFFFFFFF), "error 4"),
        ]
        for bad, index, expected in cases:
            with self.subTest(index=index, expected=expected):
                self.assertEqual(self.query(bad, "compatible", "tiny-processors,input", index), expected)

    def test_a_matching_node_without_a_usable_reg_is_not_absent(self):
        """Review on PR #18: a node that matches but has no reg, a reg whose length is not a whole
        number of entries, or no cells to size an entry is FDT_NO_REG (6), not 'not found'."""
        def input_reg(value):
            def edit(tree):
                node = soc_node(tree, "input")
                if value is None:
                    del node.props["reg"]
                else:
                    node.props["reg"] = value
            return variant(edit)

        def soc_no_cells(tree):
            tree.child("soc").props["#address-cells"] = rv32_dtb.u32(0)
            tree.child("soc").props["#size-cells"] = rv32_dtb.u32(0)
        for name, bad in (("no reg", input_reg(None)), ("short reg", input_reg(rv32_dtb.u32(0x11001000)[:6])),
                          ("half an entry", input_reg(rv32_dtb.u32(0x11001000, 16, 0x11002000))),
                          ("zero cells", variant(soc_no_cells))):
            with self.subTest(name):
                self.assertEqual(self.query(bad, "compatible", "tiny-processors,input", "0"), "error 6")
        with self.subTest("the root matches"):
            self.assertEqual(self.query(machine_blob(), "compatible", "tiny-processors,rv32-machine", "0"), "error 6")

    def test_cell_counts_must_be_one_word(self):
        def edit(tree):
            tree.child("soc").props["#size-cells"] = rv32_dtb.u32(1, 1)
        self.assertEqual(self.query(variant(edit), "compatible", "tiny-processors,input", "0"), "error 3")

    def test_nop_tokens_are_skipped(self):
        blob = with_nop_after_first_prop(machine_blob())
        self.assertEqual(rv32_dtb.parse(blob), rv32_dtb.MACHINE)
        self.assertEqual(self.query(blob, "model"), "tiny-processors RV32 machine")
        self.assertEqual(self.query(blob, "compatible", "tiny-processors,g2", "0"), "11008000 00002000")

    def test_unterminated_name_under_every_query(self):
        """Each query compares property names with a different string; none may run off the end.
        The bad name is the input node's `interrupts` (the last name first used, since O1), so a
        query answered before it (the model, the CLINT) never reaches it; every query that walks
        past it is refused."""
        self.require_sanitizers()
        bad = unterminated_last_name(rv32_dtb.build(rv32_dtb.MACHINE))
        for args, expected in ((("model",), "tiny-processors RV32 machine"),
                               (("compatible", "riscv,clint0", "0"), "02000000 00010000"),
                               (("compatible", "tiny-processors,display", "0"), "error 3"),
                               (("compatible", "tiny-processors,g2", "0"), "error 3")):
            with self.subTest(args):
                self.assertEqual(self.query(bad, *args), expected)

    def test_malformed_blobs_are_refused(self):
        self.require_sanitizers()
        blob = machine_blob()
        size_struct = header(blob, "size_dt_struct")
        name_offset = first_prop(blob) + 8
        cases = {
            "magic": (b"\1" + blob[1:], "error 1"),
            "version": (blob[:20] + struct.pack(">I", 3) + blob[24:], "error 2"),
            "struct past the end": (blob[:36] + struct.pack(">I", len(blob)) + blob[40:], "error 3"),
            "truncated file": (blob[: len(blob) // 2], "error 3"),
            "struct cut short": (blob[:36] + struct.pack(">I", size_struct // 2) + blob[40:], "error 3"),
            "bad token": (blob[:56] + struct.pack(">I", 7) + blob[60:], "error 3"),
            # The last property name ("interrupts" since O1) loses its NUL and the blob ends there: comparing
            # it with a query must not read past the strings block (review on PR #18).
            "unterminated name": (unterminated_last_name(blob), "error 3"),
            "name offset wraps": (with_word(blob, name_offset, 0xFFFFFFF0), "error 3"),
            "name offset at the end of the strings": (with_word(blob, name_offset, header(blob, "size_dt_strings")), "error 3"),
            "name offset far out": (with_word(blob, name_offset, 0x7FFFFFFF), "error 3"),
            "property length wraps": (with_word(blob, first_prop(blob) + 4, 0xFFFFFFFC), "error 3"),
            "END_NODE first": (with_word(blob, header(blob, "off_dt_struct"), 2), "error 3"),
            "nine levels deep": (nested(7), "error 3"),
            "unterminated node name": (unterminated_node_name(), "error 3"),
            "strings size wraps": (with_header(blob, size_dt_strings=0xFFFFFFFF), "error 3"),
            "misaligned structure block": (with_header(blob, off_dt_struct=header(blob, "off_dt_struct") + 2), "error 3"),
            "strings inside the header": (with_header(blob, off_dt_strings=8), "error 3"),
            "last compatible version 18": (with_header(blob, last_comp_version=18), "error 2"),
        }
        for name, (bad, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.query(bad, "compatible", "tiny-processors,g2", "0"), expected)
        with self.subTest("eight levels are allowed"):
            self.assertEqual(self.query(nested(6), "compatible", "tiny-processors,g2", "0"), "11008000 00002000")

    def test_addresses_that_do_not_fit_in_32_bits(self):
        def edit(tree):
            soc = tree.child("soc")
            soc.props["#address-cells"] = rv32_dtb.u32(2)
            node = soc_node(tree, "input")
            node.props["reg"] = rv32_dtb.u32(1, 0x11001000, 16)
        self.assertEqual(self.query(variant(edit), "compatible", "tiny-processors,input", "0"), "error 5")


if __name__ == "__main__":
    unittest.main()
