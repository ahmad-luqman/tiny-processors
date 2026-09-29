"""The architectural-test model (tests/arch/model_test.h) on its own, without the suite.

Tiny programs use the model's macros the way the suite's tests do: the halt prints the
signature, the boot's trap handler skips exactly the suite's `csrs mstatus, a0` and fails any
other trap, and the optional asserts fail a wrong result. Each runs on the emulator and, as the
reference, on QEMU, whose virt board has the same console and done register.
"""
import os
from pathlib import Path
import shutil
import subprocess
import tempfile
import unittest

from tools.rv32_arch_test import DEFAULT_QEMU_CPU, MODEL, SUITES, EXCLUDED, TestFailure, signature_lines
from tools.rv32_image import flatten, parse_elf
from tools.rv32_run_emu import emulator_command, halt_line
from tools.rv32_rtl import ROOT, decode

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
        subprocess.run([CC, "--target=riscv32-unknown-elf", "-march=rv32if_zicsr", "-mabi=ilp32", "-mno-relax",
                        "-nostdlib", "-static", f"--ld-path={LD}", f"-Wl,-T,{MODEL / 'link.ld'}", f"-I{MODEL}",
                        *(f"-D{define}" for define in defines), "-o", str(elf), str(source)], check=True)
        image = elf.with_suffix(".bin")
        image.write_bytes(flatten(parse_elf(elf.read_bytes())))
        return elf, image

    def run_emulator(self, image):
        result = subprocess.run(emulator_command(EMULATOR, image, limit=100000), capture_output=True)
        return decode(result.stdout), halt_line(decode(result.stderr))

    def run_qemu(self, elf):
        result = subprocess.run([QEMU, "-M", "virt", "-cpu", QEMU_CPU, "-bios", "none", "-kernel", str(elf), "-m", "4M",
                                 "-nographic", "-monitor", "none", "-no-reboot"], capture_output=True, timeout=30)
        return decode(result.stdout), result.returncode

    def test_halt_prints_the_signature_on_both(self):
        elf, image = self.build("plain", "li t0, 1")
        console, halt = self.run_emulator(image)
        self.assertEqual((halt["halt"], halt["outcome"]), ("done", "pass"))
        self.assertEqual(signature_lines(console), ["12345678", "deadbeef", "00000000", "00000000"])
        self.assertEqual(self.run_qemu(elf), (console, 0))

    def test_the_fp_enable_write_is_skipped_and_registers_survive(self):
        # The suite's RVTEST_FP_ENABLE: set FS in mstatus. The handler must return past it with
        # t0 and t1 intact, which the signature records.
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
        self.assertEqual((halt["halt"], halt["outcome"], halt["traps"]), ("done", "pass", 1))
        self.assertEqual(signature_lines(console)[2:], ["00000011", "00000022"])
        self.assertEqual(self.run_qemu(elf), (console, 0), "QEMU has mstatus: no trap, same signature")

    def test_any_other_trap_fails_with_code_2(self):
        for name, body in (("ecall", "ecall"), ("other_mstatus", "csrw mstatus, a0"), ("illegal", ".word 0")):
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
        self.assertEqual(sorted(SUITES), ["F", "I", "M"])
        self.assertFalse(set(SUITES) & set(EXCLUDED), "a suite is either selected or excluded with a reason")


if __name__ == "__main__":
    unittest.main()
