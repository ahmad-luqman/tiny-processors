"""The architectural-test model (tests/arch/model_test.h) on its own, without the suite, and the
riscv-tests environment (tests/riscv-tests/riscv_test.h, issue #34) on its own, without rv32ua.

Tiny programs use the model's macros the way the suite's tests do: the halt prints the
signature, the suite's `csrs mstatus, a0` retires (the machine has mstatus since Track 2), the
boot's trap handler fails any trap, and the optional asserts fail a wrong result. Each runs on the emulator and, as the
reference, on QEMU, whose virt board has the same console and done register. RunnerTest drives
the runner's per-test comparison on stubbed backends, so each rejection is shown without a suite.
"""
import argparse
import contextlib
import io
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest
from unittest import mock

from tools import rv32_arch_test
from tools.rv32_arch_test import (DEFAULT_QEMU_CPU, SUITES, EXCLUDED, TestFailure, link_flags, qemu_command, run_one,
                                  signature_lines)
from tools import rv32_riscv_tests
from tools.rv32_image import flatten, parse_elf
from tools.rv32_run_emu import emulator_command, halt_line
from tools.rv32_rtl import ROOT, Run, decode

CC = os.environ.get("RV32_CC", "clang")
LD = os.environ.get("RV32_LD", "ld.lld")
QEMU = os.environ.get("QEMU_RV32", "qemu-system-riscv32")
QEMU_CPU = os.environ.get("RV32_ARCH_QEMU_CPU", DEFAULT_QEMU_CPU)
EMULATOR = ROOT / "build/rv32/rv32emu"

SOURCE = """#include "model_test.h"
.section .text.init
.globl rvtest_entry_point
rvtest_entry_point:
RVMODEL_BOOT
{body}
RVMODEL_HALT
.data
RVMODEL_DATA_BEGIN
begin:
    .word 0x12345678, 0xdeadbeef, 0, 0
RVMODEL_DATA_END
"""


class ModelTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        for tool in (CC, LD, QEMU):
            if shutil.which(tool) is None and not Path(tool).exists():
                raise RuntimeError(f"missing {tool} (make toolchain-rv32)")
        if not EMULATOR.exists():
            raise RuntimeError(f"missing {EMULATOR} (make build-rv32-emu)")
        cls.work = tempfile.TemporaryDirectory()

    @classmethod
    def tearDownClass(cls):
        cls.work.cleanup()

    def build(self, name, body, *defines):
        directory = Path(self.work.name)
        source, elf = directory / f"{name}.S", directory / f"{name}.elf"
        source.write_text(SOURCE.format(body=body))
        subprocess.run([CC, *link_flags("rv32if_zicsr", LD), *(f"-D{define}" for define in defines),
                        "-o", str(elf), str(source)], check=True)
        image = elf.with_suffix(".bin")
        image.write_bytes(flatten(parse_elf(elf.read_bytes())))
        return elf, image

    def run_emulator(self, image):
        result = subprocess.run(emulator_command(EMULATOR, image, limit=100000), capture_output=True)
        return decode(result.stdout), halt_line(decode(result.stderr))

    def run_qemu(self, elf):
        result = subprocess.run(qemu_command(QEMU, elf, cpu=QEMU_CPU), capture_output=True, timeout=30)
        return decode(result.stdout), result.returncode

    def test_halt_prints_the_signature_on_both(self):
        elf, image = self.build("plain", "li t0, 1")
        console, halt = self.run_emulator(image)
        self.assertEqual((halt["halt"], halt["outcome"]), ("done", "pass"))
        self.assertEqual(signature_lines(console), ["12345678", "deadbeef", "00000000", "00000000"])
        self.assertEqual(self.run_qemu(elf), (console, 0))

    def test_the_fp_enable_write_retires_and_registers_survive(self):
        # The suite's RVTEST_FP_ENABLE: set FS in mstatus. Since O1 the write retires without a
        # trap, as on QEMU, with t0 and t1 intact, which the signature records.
        body = """li t0, 0x11
    li t1, 0x22
    li a0, 0x2000
    csrs mstatus, a0
    fmv.w.x f1, t0
    la a1, begin
    sw t0, 8(a1)
    sw t1, 12(a1)"""
        elf, image = self.build("fp_enable", body)
        console, halt = self.run_emulator(image)
        self.assertEqual((halt["halt"], halt["outcome"], halt["traps"]), ("done", "pass", 0))
        self.assertEqual(signature_lines(console)[2:], ["00000011", "00000022"])
        self.assertEqual(self.run_qemu(elf), (console, 0), "QEMU: no trap either, same signature")

    def test_any_other_trap_fails_with_code_2(self):
        for name, body in (("ecall", "ecall"), ("missing_csr", "csrw 0x7c0, a0"), ("illegal", ".word 0")):
            with self.subTest(name=name):
                _, image = self.build(name, body)
                console, halt = self.run_emulator(image)
                self.assertEqual((halt["halt"], halt["outcome"]), ("done", "fail=2"), halt)
                self.assertEqual(console, "")

    def test_asserts_fail_a_wrong_result_with_code_3(self):
        body = "li x5, {value}\n    RVMODEL_IO_ASSERT_GPR_EQ(x6, x5, 0x1234)"
        _, good = self.build("assert_good", body.format(value="0x1234"), "RVMODEL_ASSERT")
        self.assertEqual(self.run_emulator(good)[1]["outcome"], "pass")
        _, bad = self.build("assert_bad", body.format(value="0x1235"), "RVMODEL_ASSERT")
        self.assertEqual(self.run_emulator(bad)[1]["outcome"], "fail=3")
        _, off = self.build("assert_off", body.format(value="0x1235"))
        self.assertEqual(self.run_emulator(off)[1]["outcome"], "pass", "without RVMODEL_ASSERT the macro is empty")

    def test_signature_parser_and_selection_tables(self):
        self.assertEqual(signature_lines("0000abcd\nffffffff\n"), ["0000abcd", "ffffffff"])
        for bad in ("", "PASS 1234\n", "0000ABCD\n", "123\n"):
            with self.subTest(bad=bad), self.assertRaises(TestFailure):
                signature_lines(bad)
        self.assertEqual(sorted(SUITES), ["A", "F", "I", "M"])
        self.assertEqual(SUITES["A"].march, "rv32ia_zicsr", "exactly A: an AMO outside it would not assemble")
        self.assertFalse(set(SUITES) & set(EXCLUDED), "a suite is either selected or excluded with a reason")


# A riscv-tests ISA test in miniature: TEST_CASE as riscv-tests' test_macros.h defines it (that
# file is fetched only with the suite), and TEST_PASSFAIL's expansion written out.
RISCV_TEST_SOURCE = """#include "riscv_test.h"
#define TEST_CASE(testnum, testreg, correctval, code...) test_ ## testnum: li TESTNUM, testnum; code; li x7, correctval; bne testreg, x7, fail;
RVTEST_RV32U
RVTEST_CODE_BEGIN
  {first}
  TEST_CASE(2, a0, 5, li a0, 5)
  TEST_CASE(3, a4, {want}, la a3, word; li a1, 2; amoadd.w a4, a1, (a3); lw a4, (a3))
  {extra}
  bne x0, TESTNUM, pass
fail:
  RVTEST_FAIL
pass:
  RVTEST_PASS
RVTEST_CODE_END
  .data
RVTEST_DATA_BEGIN
word: .word 40, 0
RVTEST_DATA_END
"""


class RiscvTestEnvTest(unittest.TestCase):
    """riscv_test.h: a test that passes prints PASS, a failing case prints FAIL with its number in
    the done word, and any trap prints TRAP and fails with number 1, on the emulator and QEMU."""
    setUpClass = classmethod(ModelTest.setUpClass.__func__)
    tearDownClass = classmethod(ModelTest.tearDownClass.__func__)
    run_emulator = ModelTest.run_emulator
    run_qemu = ModelTest.run_qemu

    def build_riscv_test(self, name, want, extra="", first=""):
        directory = Path(self.work.name)
        source, elf = directory / f"{name}.S", directory / f"{name}.elf"
        source.write_text(RISCV_TEST_SOURCE.format(want=want, extra=extra, first=first))
        subprocess.run([CC, *link_flags(rv32_riscv_tests.MARCH, LD), f"-I{rv32_riscv_tests.ENV}",
                        "-o", str(elf), str(source)], check=True)
        image = elf.with_suffix(".bin")
        image.write_bytes(flatten(parse_elf(elf.read_bytes())))
        return elf, image

    def test_pass_fail_and_trap(self):
        for name, want, extra, first, console, outcome in (("pass", 42, "", "", "PASS\n", "pass"),
                                                           ("fail", 41, "", "", "FAIL\n", "fail=3"),
                                                           ("fail before any case", 42, "", "j fail", "FAIL\n", "fail=1"),
                                                           ("trap", 42, "ecall", "", "TRAP\n", "fail=1")):
            with self.subTest(name=name):
                elf, image = self.build_riscv_test(name.replace(" ", "_"), want, extra, first)
                got, halt = self.run_emulator(image)
                self.assertEqual((got, halt["outcome"]), (console, outcome))
                qemu_console, status = self.run_qemu(elf)
                self.assertEqual(qemu_console, console)
                self.assertEqual(status == 0, outcome == "pass", "QEMU exits nonzero on a fail word")

    def test_reservation_rules_agree_with_qemu(self):
        """Rows of docs/rv32-a.md's QEMU table, run on both: an SC with no reservation to an unmapped
        word fails without a fault; mret alone ends a reservation; an SC to the next word fails; a
        misaligned SC traps even with no reservation (here, a TRAP, the same on both)."""
        cases = {
            "unmapped": ("TEST_CASE(4, a4, 1, li a3, 0x00200000; sc.w a4, x0, (a3))", "PASS\n"),
            "mret": ("TEST_CASE(5, a4, 1, la a3, word; lr.w a5, (a3); la t0, 1f; csrw mepc, t0; li t1, 0x1800; "
                     "csrs mstatus, t1; mret; 1: sc.w a4, x0, (a3))", "PASS\n"),
            "next word": ("TEST_CASE(6, a4, 1, la a3, word; lr.w a5, (a3); addi a6, a3, 4; sc.w a4, x0, (a6))", "PASS\n"),
            "misaligned": ("la a3, word; addi a3, a3, 1; sc.w a4, x0, (a3)", "TRAP\n"),
        }
        for name, (extra, console) in cases.items():
            with self.subTest(name=name):
                elf, image = self.build_riscv_test(name.replace(" ", "_"), 42, extra)
                self.assertEqual(self.run_emulator(image)[0], console)
                self.assertEqual(self.run_qemu(elf)[0], console)

    def test_selection(self):
        self.assertEqual(sorted(rv32_riscv_tests.EXCLUDED), ["amocas_d", "amocas_w"], "Zacas only")


SIGNATURE = "12345678\ndeadbeef\n"
TRACE = ["1 80000000 00000013", "2 80000004 00000013"]


def passing(console=SIGNATURE, trace=TRACE):
    return Run(0, console, "", "", list(trace), {"halt": "done", "outcome": "pass", "steps": len(trace)})


class RunnerTest(unittest.TestCase):
    """run_one on stubbed backends: every disagreement with the emulator fails the test."""

    def run_one(self, backends, emulator=None, qemu=SIGNATURE, rtl=None):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            args = argparse.Namespace(out=work / "out", backends=backends, emulator="emu", icarus="tb.vvp",
                                      verilator="sim", qemu="qemu", qemu_cpu="cpu", arch_test=work, cc="cc", ld="ld",
                                      limit=100, timeout=10, keep=False)
            paths = (work / "t.elf", work / "t.bin", work / "t.hex")
            with mock.patch.object(rv32_arch_test, "assemble", return_value=paths), \
                    mock.patch.object(rv32_arch_test, "run_emulator", return_value=emulator or passing()), \
                    mock.patch.object(rv32_arch_test, "run_qemu", return_value=qemu), \
                    mock.patch.object(rv32_arch_test, "run_rtl", return_value=rtl or passing()):
                return run_one(Path("add-01.S"), "I", args)

    def test_agreement_passes(self):
        self.assertEqual(self.run_one(["emulator", "qemu", "icarus", "verilator"])[:3], ("I/add-01", 2, 2))

    def test_each_disagreement_fails(self):
        cases = {
            "signature differs from QEMU": dict(backends=["emulator", "qemu"], qemu="12345678\ndeadbeee\n"),
            "QEMU prints fewer words": dict(backends=["emulator", "qemu"], qemu="12345678\n"),
            "RTL console differs": dict(backends=["emulator", "verilator"], rtl=passing("12345678\n00000000\n")),
            "RTL trace differs": dict(backends=["emulator", "icarus"], rtl=passing(trace=[TRACE[0], "2 80000004 00000093"])),
            "RTL trace is short": dict(backends=["emulator", "icarus"], rtl=passing(trace=TRACE[:1])),
            "emulator console is no signature": dict(backends=["emulator"], emulator=passing("PASS\n")),
        }
        for name, case in cases.items():
            with self.subTest(name=name), self.assertRaises(TestFailure):
                self.run_one(**case)

    def test_a_run_that_did_not_pass_fails(self):
        """check_passed decides for the emulator and each RTL run; it exits, which the runner records."""
        for name, run in (("status", passing()._replace(status=1)), ("noise", passing()._replace(noise="%Error\n")),
                          ("no halt", passing()._replace(halt=None)),
                          ("assert failed", passing()._replace(halt={"halt": "done", "outcome": "fail=3", "steps": 2}))):
            with self.subTest(backend="emulator", name=name), self.assertRaises(SystemExit):
                self.run_one(["emulator"], emulator=run)
            with self.subTest(backend="verilator", name=name), self.assertRaises(SystemExit):
                self.run_one(["emulator", "verilator"], rtl=run)

    def test_shared_command_lines(self):
        flags = link_flags("rv32im_zicsr", "ld.lld")
        self.assertIn("-march=rv32im_zicsr", flags)
        self.assertIn("--ld-path=ld.lld", flags)
        command = qemu_command("qemu", "t.elf", cpu=DEFAULT_QEMU_CPU)
        self.assertEqual(command[command.index("-cpu") + 1], DEFAULT_QEMU_CPU)
        self.assertEqual(command[command.index("-m") + 1], "16M", "the FDT offset check assumes 16 MiB")


class RiscvTestsRunnerTest(unittest.TestCase):
    """tools/rv32_riscv_tests.py on stubbed backends and checkouts: every failure is reported, and a
    checkout missing a test is refused rather than run short."""

    def run_one(self, backends, emulator=None, qemu="PASS\n", rtl=None):
        with tempfile.TemporaryDirectory() as directory:
            work = Path(directory)
            args = argparse.Namespace(out=work / "out", backends=backends, emulator="emu", icarus="tb.vvp",
                                      verilator="sim", qemu="qemu", qemu_cpu="cpu", riscv_tests=work, cc="cc", ld="ld",
                                      limit=100, timeout=10, keep=False)
            paths = (work / "t.elf", work / "t.bin", work / "t.hex")
            with mock.patch.object(rv32_riscv_tests, "assemble", return_value=paths), \
                    mock.patch.object(rv32_riscv_tests, "run_emulator", return_value=emulator or passing("PASS\n")), \
                    mock.patch.object(rv32_riscv_tests, "run_qemu", return_value=qemu), \
                    mock.patch.object(rv32_arch_test, "run_rtl", return_value=rtl or passing("PASS\n")):
                return rv32_riscv_tests.run_one(Path("lrsc.S"), args)

    def test_agreement_passes(self):
        self.assertEqual(self.run_one(["emulator", "qemu", "icarus", "verilator"])[:2], ("lrsc", 2))

    def test_each_disagreement_fails(self):
        cases = {
            "emulator printed FAIL": dict(backends=["emulator"], emulator=passing("FAIL\n")),
            "QEMU printed TRAP": dict(backends=["emulator", "qemu"], qemu="TRAP\n"),
            "RTL console differs": dict(backends=["emulator", "verilator"], rtl=passing("FAIL\n")),
            "RTL trace differs": dict(backends=["emulator", "icarus"], rtl=passing("PASS\n", [TRACE[0], "2 80000004 00000093"])),
        }
        for name, case in cases.items():
            with self.subTest(name=name), self.assertRaises(TestFailure):
                self.run_one(**case)
        with self.subTest(name="emulator did not pass"), self.assertRaises(SystemExit):
            self.run_one(["emulator"], emulator=passing("PASS\n")._replace(halt={"halt": "done", "outcome": "fail=3", "steps": 2}))

    def test_pack_image_refuses_a_bad_entry_and_an_image_too_large(self):
        parsed = argparse.Namespace(symbols={"rvtest_entry_point": rv32_arch_test.RAM_BASE + 4})
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(rv32_arch_test, "parse_elf", return_value=parsed), self.assertRaises(TestFailure):
            elf = Path(directory) / "t.elf"
            elf.write_bytes(b"")
            rv32_arch_test.pack_image(elf)
        parsed = argparse.Namespace(symbols={"rvtest_entry_point": rv32_arch_test.RAM_BASE})
        with tempfile.TemporaryDirectory() as directory, \
                mock.patch.object(rv32_arch_test, "parse_elf", return_value=parsed), \
                mock.patch.object(rv32_arch_test, "flatten", return_value=bytes(rv32_arch_test.QEMU_FDT_OFFSET + 4)), \
                self.assertRaises(TestFailure):
            elf = Path(directory) / "t.elf"
            elf.write_bytes(b"")
            rv32_arch_test.pack_image(elf)

    def makefrag(self, root, names, present):
        source = root / "isa" / "rv32ua"
        source.mkdir(parents=True)
        (source / "Makefrag").write_text("rv32ua_sc_tests = \\\n\t" + " ".join(names) + " \\\n\n")
        for name in present:
            (source / f"{name}.S").write_text("")

    def test_selection_wants_every_listed_test(self):
        names = ["amoadd_w", "lrsc", "amocas_w", "amocas_d"]
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.makefrag(root, names, names)
            self.assertEqual([test.stem for test in rv32_riscv_tests.select_tests(root, [])], ["amoadd_w", "lrsc"])
            self.assertEqual([test.stem for test in rv32_riscv_tests.select_tests(root, ["lr*"])], ["lrsc"])
            with self.assertRaises(SystemExit):
                rv32_riscv_tests.select_tests(root, ["nosuch*"])
            (root / "isa" / "rv32ua" / "lrsc.S").unlink()
            with self.assertRaises(SystemExit):
                rv32_riscv_tests.select_tests(root, [])
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            self.makefrag(root, ["amoadd_w", "lrsc"], ["amoadd_w", "lrsc"])
            with self.assertRaises(SystemExit):  # EXCLUDED's amocas_* no longer listed: stale
                rv32_riscv_tests.select_tests(root, [])

    def test_arch_selection_wants_every_pinned_test(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            source = root / "riscv-test-suite" / "rv32i_m" / "M" / "src"
            source.mkdir(parents=True)
            for i in range(SUITES["M"].tests - 1):
                (source / f"t{i}-01.S").write_text("")
            with self.assertRaises(SystemExit):
                rv32_arch_test.select_tests(root, ["M"], [])
            (source / "last-01.S").write_text("")
            self.assertEqual(len(rv32_arch_test.select_tests(root, ["M"], [])), SUITES["M"].tests)

    def test_fetch_refetches_a_checkout_missing_a_suite(self):
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".git").mkdir()
            for suite in SUITES:
                source = root / "riscv-test-suite" / "rv32i_m" / suite / "src"
                source.mkdir(parents=True)
                for i in range(SUITES[suite].tests - (suite == "A")):
                    (source / f"t{i}-01.S").write_text("")
            with mock.patch.object(rv32_arch_test, "checkout_commit", return_value=rv32_arch_test.ARCH_TEST_COMMIT), \
                    mock.patch.object(rv32_arch_test, "sparse_checkout") as checkout, \
                    contextlib.redirect_stdout(io.StringIO()):
                rv32_arch_test.fetch(root)
                self.assertEqual(checkout.call_count, 1, "A is one test short: fetched again")
                (root / "riscv-test-suite" / "rv32i_m" / "A" / "src" / "last-01.S").write_text("")
                rv32_arch_test.fetch(root)
                self.assertEqual(checkout.call_count, 1, "complete: left alone")


if __name__ == "__main__":
    unittest.main()
