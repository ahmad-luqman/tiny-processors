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
import tempfile
import unittest

from tools import rv32_dtb, rv32_virt_map

ROOT = Path(__file__).resolve().parents[1]
QEMU = os.environ.get("QEMU_RV32", "qemu-system-riscv32")


def virt_blob():
    if not shutil.which(QEMU):
        raise unittest.SkipTest(f"{QEMU} not found")
    return rv32_virt_map.dump_virt(QEMU)


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
        result = subprocess.run(["python3", "tools/rv32_dtb.py", "--check"], cwd=ROOT, capture_output=True, text=True)
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
                          ("token", blob[:56] + struct.pack(">I", 7) + blob[60:])):
            with self.subTest(name), self.assertRaises(ValueError):
                rv32_dtb.parse(bad)


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
        cls.virt = rv32_dtb.parse(virt_blob())

    def test_our_map_fits_virt(self):
        self.assertEqual(rv32_virt_map.check(self.virt, rv32_dtb.MACHINE), [])

    def test_the_pre_track_1_map_does_not(self):
        """Before Track 1 the input window sat on virt's flash; the checker must see that."""
        problems = rv32_virt_map.check(self.virt, moved(rv32_dtb.MACHINE, "input", 0x2000_1000))
        self.assertEqual(len(problems), 1, problems)
        self.assertIn("overlaps virt's /flash@20000000", problems[0])
        problems = rv32_virt_map.check(self.virt, moved(rv32_dtb.MACHINE, "clint", 0x0200_1000))
        self.assertIn("is not inside virt's riscv,clint0", " ".join(problems))
        problems = rv32_virt_map.check(self.virt, moved(rv32_dtb.MACHINE, "g3d", 0x1000_1000))
        self.assertIn("virtio_mmio@10001000", " ".join(problems))


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
        command = [cc, *flags, *sanitize, "-o", str(cls.binary), "tests/rv32_fdt_native.c", "programs/rv32/fdt.c"]
        if subprocess.run(command, cwd=ROOT, capture_output=True).returncode != 0:
            command = [cc, *flags, "-o", str(cls.binary), "tests/rv32_fdt_native.c", "programs/rv32/fdt.c"]
            subprocess.run(command, cwd=ROOT, check=True)

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def query(self, blob, *args):
        path = Path(self.work.name) / "tree.dtb"
        path.write_bytes(blob)
        result = subprocess.run([str(self.binary), str(path), *args], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")
        return result.stdout.strip()

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
        blob = virt_blob()
        tree = rv32_dtb.parse(blob)
        self.assertEqual(self.query(blob, "model"), rv32_dtb.strings_of(tree.props["model"])[0])
        for compatible, expected in (("ns16550a", "10000000 00000100"), ("sifive,test0", "00100000 00001000"),
                                     ("riscv,clint0", "02000000 00010000"), ("tiny-processors,input", "error 4")):
            with self.subTest(compatible):
                self.assertEqual(self.query(blob, "compatible", compatible, "0"), expected)
        self.assertEqual(self.query(blob, "device_type", "memory", "0"), "80000000 00400000")
        # A two-cell address whose high cell is zero fits in 32 bits.
        self.assertEqual(self.query(blob, "compatible", "pci-host-ecam-generic", "0"), "30000000 10000000")

    def test_malformed_blobs_are_refused(self):
        blob = rv32_dtb.build(rv32_dtb.MACHINE)
        size_struct = struct.unpack(">I", blob[36:40])[0]
        cases = {
            "magic": (b"\1" + blob[1:], "error 1"),
            "version": (blob[:20] + struct.pack(">I", 3) + blob[24:], "error 2"),
            "struct past the end": (blob[:36] + struct.pack(">I", len(blob)) + blob[40:], "error 3"),
            "truncated file": (blob[: len(blob) // 2], "error 3"),
            "struct cut short": (blob[:36] + struct.pack(">I", size_struct // 2) + blob[40:], "error 3"),
            "bad token": (blob[:56] + struct.pack(">I", 7) + blob[60:], "error 3"),
        }
        for name, (bad, expected) in cases.items():
            with self.subTest(name):
                self.assertEqual(self.query(bad, "compatible", "tiny-processors,g2", "0"), expected)


if __name__ == "__main__":
    unittest.main()
