"""Track 2, O2 onward: the console's receive side, the RAM disk, and the kernel (docs/rv32-os.md).

The RAM disk tests use the program ELFs `make check-rv32-os-image` builds. The kernel's console
session runs on the emulator and the RTL in step-tick mode, where the whole trace, interrupt
lines included, must agree; the Make targets run it on QEMU and with cycle ticks.
"""
import os
import re
import shutil
import struct
import subprocess
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import test_rv32_rtl as integer_tests
from tools import rv32_mkfs as mkfs
from tools import rv32_ramdisk
from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_image import SHT_NOBITS, flatten, parse_elf
from tools.rv32_rtl import ROOT, Run, compare_backends, diff_traces, run_emulator, run_rtl, write_image

OS = ROOT / "build/rv32/os"
PROGRAMS = ("sh", "hello", "primes", "pong", "tetris", "menu", "syscheck", "fault", "cat", "write", "files", "bars", "life",
            "fill", "dmaprobe", "libccheck", "lua", "fpcheck", "fpmate", "mandel")
ENGINES = frozenset({"menu", "dmaprobe"})  # the programs flagged `accelerators`
SPANS = {"menu": 3, "lua": 6}  # slots; every other program takes one
STACKS = {"lua": 0x28000}  # bytes (Track 3); every other program has the default


def slot_of(pc):
    """The program slot an address lies in, or None below the slots (the kernel)."""
    return (pc - rv32_ramdisk.SLOT_BASE) // rv32_ramdisk.SLOT_SIZE if pc >= rv32_ramdisk.SLOT_BASE else None


def slot_named(name):
    """A program's first slot, as the Makefile assigns it."""
    out = subprocess.run(["make", "-s", "--no-print-directory", f"print-rv32-os-slot-{name}"], cwd=ROOT,
                         capture_output=True, text=True, check=True).stdout
    return int(out.split()[0])


def elfs(*names):
    paths = [OS / f"{name}.elf" for name in names]
    missing = [p for p in paths if not p.exists()]
    if missing:
        raise unittest.SkipTest(f"{missing[0]} is missing: run make check-rv32-os-image")
    return paths


class RamdiskTest(unittest.TestCase):
    def test_round_trip(self):
        blob = rv32_ramdisk.build(elfs(*PROGRAMS), ENGINES)
        entries = rv32_ramdisk.parse(blob)
        self.assertEqual([e["name"] for e in entries], list(PROGRAMS))
        for index, entry in enumerate(entries):
            with self.subTest(entry["name"]):
                self.assertEqual(entry["load"] % rv32_ramdisk.SLOT_SIZE, rv32_ramdisk.SLOT_BASE % rv32_ramdisk.SLOT_SIZE)
                self.assertEqual(entry["entry"], entry["load"])
                self.assertEqual(entry["flags"], rv32_ramdisk.ACCELERATORS if entry["name"] in ENGINES else 0)
                self.assertLessEqual(entry["size"], entry["memory"])
                self.assertEqual(entry["span"], rv32_ramdisk.SLOT_SIZE * SPANS.get(entry["name"], 1))
                self.assertEqual(entry["stack"], STACKS.get(entry["name"], rv32_ramdisk.STACK_SIZE))
                self.assertLessEqual(entry["memory"], entry["span"] - entry["stack"])
                elf = parse_elf((OS / f"{entry['name']}.elf").read_bytes())
                self.assertEqual(entry["data"], flatten(elf, entry["load"]))
        ranges = sorted((e["load"], e["load"] + e["span"]) for e in entries)
        self.assertTrue(all(end <= start for (_, end), (start, _) in zip(ranges, ranges[1:])), "no two spans overlap")
        self.assertEqual(blob, (OS / "ramdisk.img").read_bytes(), "the committed build rule makes the same bytes")

    def test_refusals(self):
        hello, primes = elfs("hello", "primes")
        with tempfile.TemporaryDirectory() as directory:
            twin = Path(directory) / "twin.elf"
            shutil.copy(hello, twin)
            long_name = Path(directory) / ("x" * 24 + ".elf")
            shutil.copy(hello, long_name)
            cases = {"same slot": ([hello, twin], frozenset()), "same name": ([hello, hello], frozenset()),
                     "long name": ([long_name], frozenset()), "unknown accelerator program": ([hello], frozenset({"menu"})),
                     "kernel image": ([ROOT / "build/rv32/os/kernel.elf"], frozenset())}
            for name, (paths, accelerators) in cases.items():
                with self.subTest(name), self.assertRaises(rv32_ramdisk.RamdiskError):
                    rv32_ramdisk.build(paths, accelerators)
        # Track 3: a stack is whole pages, at least two (one is the kernel's guard), inside the span
        # and above the program. hello loads at 0x8012_0000 with one slot; each case changes its
        # stack's bottom or, to reach the last rule with a well-formed stack, its size in memory.
        original = parse_elf(hello.read_bytes())
        not_pages, too_small = "is not two or more pages inside the span", "is not two or more pages"
        for name, bottom, memsz, message in (
                ("stack not whole pages", 0x80137f00, None, not_pages),
                ("stack of one page", 0x8013f000, None, too_small),
                ("stack of the whole span", 0x80120000, None, not_pages),
                ("stack over the program", 0x8013e000, 0x1f000, "leave no room for the stack"),
                ("no stack symbol", None, None, "no _stack_bottom")):
            symbols = {k: v for k, v in original.symbols.items() if k != "_stack_bottom" or bottom is not None}
            if bottom is not None:
                symbols["_stack_bottom"] = bottom
            segments = [seg._replace(memsz=memsz) if memsz and seg.type == 1 else seg for seg in original.segments]
            elf = original._replace(symbols=symbols, segments=segments)
            with self.subTest(name), mock.patch.object(rv32_ramdisk, "parse_elf", return_value=elf), \
                    self.assertRaisesRegex(rv32_ramdisk.RamdiskError, message):
                rv32_ramdisk.program(hello)
        self.assertEqual(rv32_ramdisk.program(hello).stack, rv32_ramdisk.STACK_SIZE, "the unpatched ELF passes")
        blob = rv32_ramdisk.build([hello, primes])
        for name, bad in (("magic", b"XXXX" + blob[4:]), ("short", blob[:8]),
                          ("count", blob[:4] + struct.pack("<I", 1000) + blob[8:]),
                          ("offset", blob[:16 + 24 + 16] + struct.pack("<I", len(blob)) + blob[16 + 24 + 20:])):
            with self.subTest(name), self.assertRaises(rv32_ramdisk.RamdiskError):
                rv32_ramdisk.parse(bad)


class FileSystemToolTest(unittest.TestCase):
    """tools/rv32_mkfs.py: the layout the kernel's fs.c reads, and its refusals."""

    def test_layout(self):
        disk = mkfs.blank()
        mkfs.add(disk, "one", b"x" * 700)
        mkfs.add(disk, "two", b"hello")
        self.assertEqual(mkfs.entries(disk), [("one", 2, 8, 700), ("two", 10, 8, 5)])
        self.assertEqual(mkfs.read(disk, "two"), b"hello")
        self.assertEqual(struct.unpack_from("<5I", disk, 0), (0x31534654, 256, 1, 2, 16))
        self.assertEqual(disk[10 * 512:10 * 512 + 5], b"hello", "an extent starts on its sector")

    def test_refusals(self):
        disk = mkfs.blank()
        mkfs.add(disk, "a", b"")
        for name, action in (("duplicate", lambda: mkfs.add(disk, "a", b"")),
                             ("long name", lambda: mkfs.add(disk, "x" * 20, b"")),
                             ("too big", lambda: mkfs.add(disk, "big", bytes(0x20000))),
                             ("missing", lambda: mkfs.read(disk, "nope")),
                             ("undecodable name", lambda: mkfs.entries(disk[:512] + b"\xff" * 20 + disk[532:])),
                             ("not tfs", lambda: mkfs.entries(bytes(0x20000))),
                             ("wrong size", lambda: mkfs.entries(bytes(512)))):
            with self.subTest(name), self.assertRaises(mkfs.FsError):
                action()
        full = mkfs.blank()
        for i in range(16):
            mkfs.add(full, f"f{i}", b"", capacity=1)
        with self.assertRaises(mkfs.FsError):
            mkfs.add(full, "one-more", b"", capacity=1)


class ComparisonTest(unittest.TestCase):
    """The runner's comparisons for multitasking (O4), on made-up runs."""

    def run_of(self, trace, checkpoints=()):
        return Run(0, "ok\n", "", "", trace, {"halt": "done"}, list(checkpoints))

    def test_trap_records_are_compared_per_region(self):
        shell = "80100140 00000073 trap 11 00000000"
        child = "80120010 00000073 trap 11 00000000"
        fault = "80120020 00000000 trap 5 00200000"
        a = self.run_of([f"1 {shell}", f"2 {child}", f"3 {fault}"])
        b = self.run_of([f"1 {child}", f"2 {shell}", f"3 {fault}"])
        self.assertIsNone(compare_backends(a, b, "results"), "two processes may interleave")
        c = self.run_of([f"1 {fault}", f"2 {child}", f"3 {shell}"])
        self.assertIn("region at 0x80120000", compare_backends(a, c, "results"), "one process's order is fixed")
        retried = self.run_of([f"1 {shell}", f"2 {shell}", f"3 {child}", f"4 {fault}"])
        self.assertIn("trap mismatch", compare_backends(a, retried, "results"))
        self.assertIsNone(compare_backends(a, retried, "results", traps="faults"), "a retried ecall is not a fault")
        moved = self.run_of([f"1 {shell}", f"2 {child}", "3 80120024 00000000 trap 5 00200000"])
        self.assertIn("trap mismatch", compare_backends(a, moved, "results", traps="faults"))

    def test_checkpoint_count(self):
        a = self.run_of([], ["frame 1 00000001", "frame 2 00000002"])
        b = self.run_of([], ["frame 1 00000002", "frame 2 00000001"])
        self.assertIn("checkpoint mismatch", compare_backends(a, b, "results"))
        self.assertIsNone(compare_backends(a, b, "results", checkpoints="count"))
        self.assertIn("count mismatch", compare_backends(a, self.run_of([], ["frame 1 00000001"]), "results", checkpoints="count"))


class ConsoleReceiveTest(unittest.TestCase):
    """O2: the console's RBR and LSR.DR on both backends, trace for trace."""
    setUpClass = classmethod(integer_tests.RtlTest.setUpClass.__func__)
    tearDownClass = classmethod(integer_tests.RtlTest.tearDownClass.__func__)

    def test_bytes_wait_from_reset_and_are_taken_in_order(self):
        words = LI(5, CONSOLE) + [LBU(6, 5, 5), LBU(7, 5, 0), LBU(8, 5, 5), LBU(9, 5, 0), LBU(10, 5, 5), LBU(11, 5, 0)]
        words += [SB(7, 5, 0), SB(9, 5, 0)] + FINISH()  # echo them
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            hex_path, bin_path = write_image(words, directory, "image")
            source = Path(directory) / "input.txt"
            source.write_bytes(b"ok")
            emulator = run_emulator(self.emulator, bin_path, Path(directory) / "emu.trace", console_input=source)
            rtl = run_rtl(self.simulator, hex_path, Path(directory) / "rtl.trace", stall=1, console_input=source)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual((emulator.console, rtl.console), ("ok", "ok"))
        loads = [line.split("->")[1].split("/")[0] for line in rtl.trace if "->" in line]
        self.assertEqual(loads, ["00000021", "0000006f", "00000021", "0000006b", "00000020", "00000000"])


class DecimalTest(unittest.TestCase):
    """u_decimal on the host: the sessions only print numbers up to five digits, so the long
    division's top bits and ten-digit values are checked here against Python's own str()."""

    def test_matches_str_across_the_range(self):
        import ctypes
        with tempfile.TemporaryDirectory() as directory:
            library = Path(directory) / "udecimal.so"
            subprocess.run([os.environ.get("HOST_CC", "cc"), "-shared", "-fPIC", "-O2", "-std=c11", "-Wall", "-Wextra",
                            "-Werror", "-o", str(library), str(ROOT / "programs/rv32/os/udecimal.c")], check=True)
            u_decimal = ctypes.CDLL(str(library)).u_decimal
            u_decimal.restype, u_decimal.argtypes = ctypes.c_uint32, [ctypes.c_uint32, ctypes.c_char_p]
            values = [0, 1, 9, 10, 11, 99, 100, 65535, 99999, 100000, 999999999, 10**9, 2**31 - 1, 2**31,
                      2**32 - 6, 2**32 - 1] + [10**k - 1 for k in range(1, 10)] + [10**k for k in range(1, 10)]
            values += [(0x9E3779B9 * i) & 0xffffffff for i in range(1, 2000)]
            for value in values:
                digits = ctypes.create_string_buffer(11)
                start = u_decimal(value, digits)
                self.assertEqual(digits.raw[start:10].decode(), str(value), value)
                self.assertEqual(digits.raw[10], 0)


class LayoutTest(unittest.TestCase):
    """The slot layout is written in three places: sys.h (the kernel and the programs), the
    Makefile (the link addresses) and tools/rv32_ramdisk.py (the RAM disk's checks)."""

    def test_the_three_descriptions_of_the_slots_agree(self):
        text = (ROOT / "programs/rv32/os/sys.h").read_text()
        header = {name: int(value, 16) for name, value in re.findall(r"#define (OS_\w+)\s+(0x[0-9a-f]+)u", text)}
        header["OS_SLOTS"] = int(re.search(r"#define OS_SLOTS\s+(\d+)u", text).group(1))
        make = subprocess.run(["make", "-s", "--no-print-directory", "print-rv32-os-layout"], cwd=ROOT,
                              capture_output=True, text=True, check=True).stdout.split()
        self.assertEqual((header["OS_SLOT_BASE"], header["OS_SLOT_SIZE"]), (int(make[0], 16), int(make[1], 16)))
        self.assertEqual((header["OS_SLOT_BASE"], header["OS_SLOT_SIZE"], header["OS_SLOTS"], header["OS_STACK_SIZE"]),
                         (rv32_ramdisk.SLOT_BASE, rv32_ramdisk.SLOT_SIZE, rv32_ramdisk.SLOTS, rv32_ramdisk.STACK_SIZE))
        self.assertEqual(header["OS_SLOT_BASE"], 0x80000000 + header["OS_KERNEL_SIZE"])

    def test_the_page_tables_fit_the_kernel(self):
        """Issue #25: the kernel's page tables are a page-aligned NOBITS section inside its MiB,
        below its stack, sized for every process table entry, and PMP lets the walks read exactly
        that section."""
        (kernel,) = elfs("kernel")
        elf = parse_elf(kernel.read_bytes())
        section = next((s for s in elf.sections if s.name == ".pagetables"), None)
        self.assertIsNotNone(section, "kernel.elf has a .pagetables section")
        procs, tables = kernel_constant("MAX_PROCS"), kernel_constant("PAGE_TABLES")
        self.assertEqual(section.type, SHT_NOBITS, "startup does not clear it; the kernel does")
        self.assertEqual(section.addr % 4096, 0)
        self.assertEqual(section.size, procs * tables * 4096)
        self.assertEqual((elf.symbols["__pagetables_start"], elf.symbols["__pagetables_end"]),
                         (section.addr, section.addr + section.size))
        self.assertLessEqual(section.addr + section.size, elf.symbols["_stack_bottom"])

    def test_every_process_fits_its_entrys_tables(self):
        """Issue #25: an entry has a root and PAGE_TABLES - 1 level-0 tables, one per 4 MiB region
        its process touches. On our machine's tree the most any process touches, every slot, the
        framebuffer and every engine window, fits; the kernel checks the same at boot
        (check_page_table_budget), and QEMU virt's tree has only the slots."""
        from tools import rv32_dtb
        ranges = [(rv32_ramdisk.SLOT_BASE, rv32_ramdisk.SLOTS * rv32_ramdisk.SLOT_SIZE)]
        regs = rv32_dtb.regions(rv32_dtb.MACHINE)
        ranges.append([(base, size) for path, base, size in regs if path.startswith("/soc/display@")][1])  # the framebuffer
        ranges += [(base, size) for path, base, size in regs if path.split("@")[0] in ("/soc/simd4", "/soc/gpu", "/soc/g3d")]
        self.assertEqual(len(ranges), 1 + 1 + 5, "the slots, the framebuffer and the five engine windows")
        touched = {region for base, size in ranges for region in range(base >> 22, ((base + size - 1) >> 22) + 1)}
        self.assertLessEqual(1 + len(touched), kernel_constant("PAGE_TABLES"))


class ShellTest(unittest.TestCase):
    """Issue #30: the shell at a terminal, on the emulator. Enter is \\r there, and the shell echoes
    each key as it comes, so backspace, ^U and a full line show as they happen."""

    @classmethod
    def setUpClass(cls):
        if not (OS / "kernel.bin").exists():
            raise unittest.SkipTest("run make check-rv32-os-image")
        cls.workdir = tempfile.TemporaryDirectory()
        cls.emulator = Path(cls.workdir.name) / "rv32emu"
        integer_tests.build_emulator(cls.emulator)

    @classmethod
    def tearDownClass(cls):
        cls.workdir.cleanup()

    def console(self, keys):
        """The console transcript of a boot that receives `keys`, which must end in a halt."""
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            work = Path(directory)
            (work / "keys").write_bytes(keys)
            shutil.copy(OS / "disk.img", work / "disk")
            run = run_emulator(self.emulator, OS / "kernel.bin", None, console_input=work / "keys", disk=work / "disk")
        self.assertEqual(run.halt["outcome"], "pass", run.stderr)
        return run.console

    def test_enter_is_lf_cr_or_crlf(self):
        lf = self.console(b"hello hi\nhello x\nhalt\n")
        self.assertIn("$ hello hi\nhello from pid 2, args: hi\n$ hello x\n", lf)
        self.assertEqual(self.console(b"hello hi\rhello x\rhalt\r"), lf)
        self.assertEqual(self.console(b"hello hi\r\nhello x\r\nhalt\r\n"), lf, "\\r\\n is one Enter, not two")
        blank = self.console(b"hello a\r\n\nhalt\n")
        self.assertIn("args: a\n$ \n$ halt\n", blank, "\\r\\n\\n is one empty line, not two")

    def test_each_key_is_echoed_as_it_comes(self):
        keys = (b"helx\x7flo b\x08c\n" + b"\x1b[Ajunk\x15hello d\te\x1bOA\n" + b"hello \x80f\xff\x03\n" +
                b"x" * 79 + b"\n" + b"x" * 80 + b"\n" + b"x" * 85 + b"\x7f" * 10 + b"\n" + b"x" * 80 + b"\x15hello h\n" +
                b"hello \x1b[3~\x1bx\x1b\x1b[A\x1b[[Ai\n" + b"hello e\x1b\n" + b"hello f\x1b[\n" + b"\x7fhalt\n")
        console = self.console(keys)
        self.assertEqual(self.console(keys.replace(b"\n", b"\r")), console, "typed or piped, the same echo")
        rub, x = "\b \b", "x" * 79
        self.assertIn(f"$ helx{rub}lo b{rub}c\nhello from pid 2, args: c\n", console, "from the first key")
        self.assertIn(f"$ junk{rub * 4}hello d e\nhello from pid 3, args: d e\n", console,
                      "escape sequences are dropped, a tab is a space, ^U rubs out the line")
        self.assertIn("$ hello \af\a\a\nhello from pid 4, args: f\n", console,
                      "a control byte and each non-ASCII byte ring and are dropped")
        self.assertIn(f"$ {x}\nsh: {x}: cannot run\n", console, "79 bytes fit")
        self.assertIn(f"$ {x}\a\nsh: line too long\n", console, "the 80th rings and refuses the line")
        self.assertIn(f"$ {x}{chr(7) * 6}{rub * 10}\nsh: line too long\n", console,
                      "a line that lost a byte stays refused, not cut")
        self.assertIn(f"$ {x}\a{rub * 79}hello h\nhello from pid 5, args: h\n", console,
                      "^U clears the line and its refusal")
        self.assertIn("$ hello i\nhello from pid 6, args: i\n", console,
                      "ESC [3~, ESC x, ESC ESC [A and the Linux console's ESC [[A are dropped whole")
        self.assertIn("$ hello e\nhello from pid 7, args: e\n$ hello f\nhello from pid 8, args: f\n", console,
                      "Enter ends the line even inside an escape sequence")
        self.assertIn("$ halt\n", console, "backspace on an empty line rubs out nothing")


def kernel_constant(name):
    """A #define of kernel.c, a decimal number with or without the u suffix."""
    text = (ROOT / "programs/rv32/os/kernel.c").read_text()
    return int(re.search(rf"#define {name} (\d+)u?\b", text).group(1))


class KernelTest(unittest.TestCase):
    """The kernel's console session in step-tick mode: the emulator and the RTL agree trace for trace."""
    setUpClass = classmethod(integer_tests.RtlTest.setUpClass.__func__)
    tearDownClass = classmethod(integer_tests.RtlTest.tearDownClass.__func__)

    def test_console_session_is_trace_identical_in_step_ticks(self):
        image = OS / "kernel.bin"
        if not image.exists():
            self.skipTest("run make check-rv32-os-image")
        session = ROOT / "programs/rv32/os/session.txt"
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            hex_path = Path(directory) / "kernel.hex"
            from tools.rv32_image import write_hex
            write_hex(hex_path, image.read_bytes())
            disks = [Path(directory) / name for name in ("emu.disk", "rtl.disk")]
            for disk in disks:
                shutil.copy(OS / "disk.img", disk)
            emulator = run_emulator(self.emulator, image, Path(directory) / "emu.trace", console_input=session, disk=disks[0])
            rtl = run_rtl(self.simulator, hex_path, Path(directory) / "rtl.trace", seed=11, ticks="steps",
                          console_input=session, max_cycles=60000000, timeout=900, disk=disks[1])
            # The outcome first: a run that failed leaves a disk that says nothing about the kernel.
            self.assertEqual(rtl.halt["outcome"], "pass", rtl.stderr)
            self.assertEqual(disks[0].read_bytes(), disks[1].read_bytes(), "both backends leave the same disk")
            self.assertEqual(mkfs.read(disks[1].read_bytes(), "note"), b"hi\n")
            self.assertEqual(len(mkfs.read(disks[1].read_bytes(), "big")), 4096, "a file stops at its capacity")
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual(rtl.console, (ROOT / "programs/rv32/os/session.expected").read_text())
        self.assertGreater(rtl.halt["interrupts"], 10, "the timer ticked and the kernel took it")
        self.assertIn("kernel: pid 10 fault killed: cause 13", rtl.console, "a page fault since issue #25")


    def test_two_jobs_share_the_machine_fairly(self):
        """O4's round robin, measured: in the jobs session the emulator's trace (read as it is
        written, through a pipe) shows the timer passing the machine back and forth between bars
        and life, and while both run each gets about half of the instructions."""
        image = OS / "kernel.bin"
        if not image.exists():
            self.skipTest("run make check-rv32-os-image")
        slot = slot_of
        bars, life = slot_named("bars"), slot_named("life")
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            disk, fifo = Path(directory) / "jobs.disk", Path(directory) / "trace"
            shutil.copy(OS / "disk.img", disk)
            os.mkfifo(fifo)
            process = subprocess.Popen([str(self.emulator), "--image", str(image), "--disk", str(disk), "--trace", str(fifo),
                                        "--console-input", str(ROOT / "programs/rv32/os/jobs.session")],
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            order = []  # (step, program) for each instruction of either job
            with open(fifo) as trace:
                for line in trace:
                    step, pc = line.split(maxsplit=2)[:2]
                    which = slot(int(pc, 16))
                    if which in (bars, life):
                        order.append((int(step), which))
            console, _ = process.communicate(timeout=120)
        self.assertEqual(process.returncode, 0)
        self.assertIn("preempted yes", console)
        switches = [i for i in range(1, len(order)) if order[i][1] != order[i - 1][1]]
        self.assertGreater(len(switches), 100, "the timer passed the machine between them many times")
        both = order[switches[0]:switches[-1]]  # while both were running
        share = sum(which == bars for _, which in both) / len(both)
        self.assertGreater(share, 0.4, f"bars ran {share:.0%} of the time both were ready")
        self.assertLess(share, 0.6, f"bars ran {share:.0%} of the time both were ready")

    def test_the_fpu_changes_hands_only_on_a_claim(self):
        """Issue #33's lazy switch, measured: in the float session the emulator's trace shows each
        of fpcheck and fpmate taking the FPU by an illegal-instruction trap on an F instruction (FS
        Off), the claims alternating between the two (a process with FS on keeps it until the
        other claims), the claiming instruction running again right after, and the kernel saving
        the previous owner's state on some claims (it was Dirty) and not on others (Clean: f_hold
        writes neither f registers nor fcsr)."""
        image, disk_image, elf = OS / "kernel.bin", OS / "apps.disk", OS / "kernel.elf"
        if not image.exists() or not disk_image.exists():
            self.skipTest("run make check-rv32-os-image build/rv32/os/apps.disk")
        fpu_save = f"{parse_elf(elf.read_bytes()).symbols['fpu_save']:08x}"
        slot = slot_of
        fpcheck, fpmate = slot_named("fpcheck"), slot_named("fpmate")
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            disk, fifo, out = Path(directory) / "float.disk", Path(directory) / "trace", Path(directory) / "console"
            shutil.copy(disk_image, disk)
            os.mkfifo(fifo)
            with open(out, "w") as console_file:  # a file, not a pipe: the trace is read to the end first
                process = subprocess.Popen([str(self.emulator), "--image", str(image), "--disk", str(disk), "--trace",
                                            str(fifo), "--console-input", str(ROOT / "programs/rv32/os/float.session")],
                                           stdout=console_file, stderr=subprocess.DEVNULL, text=True)
                # A claim is a cause-2 trap whose instruction the process runs again next: (slot, saved).
                claims, kills, pending, saving = [], 0, {}, False
                with open(fifo) as trace:
                    for line in trace:
                        fields = line.split()
                        saving = saving or fields[1] == fpu_save  # since the last trap in either slot
                        which = slot(int(fields[1], 16))
                        if which not in (fpcheck, fpmate):
                            continue
                        if which in pending:
                            if fields[1] == pending.pop(which):
                                claims.append((which, saving))
                            else:
                                kills += 1
                        if fields[3:5] == ["trap", "2"]:
                            pending[which] = fields[1]
                            saving = False
                self.assertEqual(process.wait(timeout=300), 0)
            console = out.read_text()
        kills += len(pending)  # a trap with nothing after it: `fpmate bad`, killed
        self.assertIn("fpcheck: ok", console)
        self.assertIn("fpmate killed: cause 2", console)
        self.assertEqual(kills, 1, "only fpmate bad's invalid instruction is not run again")
        order = [which for which, _ in claims]
        self.assertEqual(order[0], fpcheck, "fpcheck's first F instruction claims the FPU")
        while_both = order[:len(order) - order[::-1].index(fpcheck)]  # to fpcheck's last claim
        self.assertGreater(len(while_both), 20, "the FPU changed hands many times")
        self.assertTrue(all(a != b for a, b in zip(while_both, while_both[1:])), f"claims alternate: {while_both[:20]}")
        saved = sum(saved for _, saved in claims[1:len(while_both)])
        self.assertGreater(saved, 10, "a Dirty owner's state is saved")
        self.assertGreater(len(while_both) - 1 - saved, 10, "a Clean owner's is not")

if __name__ == "__main__":
    unittest.main()
