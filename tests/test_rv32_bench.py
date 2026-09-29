"""The benchmark runner's checks (tools/rv32_bench.py) on recorded consoles.

The consoles are the emulator's output for the RV32IM images. Each check must accept them and
reject each way a run could go wrong: a CRC or a final value off, a missing or malformed timing
line, CoreMark's own error report. main() runs on stubbed backends, so a cross-backend difference
is shown to fail without a simulator.
"""
from pathlib import Path
import tempfile
import unittest
from unittest import mock

from tools import rv32_bench
from tools.rv32_bench import BenchError, Timing, check_coremark, check_dhrystone, comparable, measure, timing_lines
from tools.rv32_rtl import Run

COREMARK = """\
2K performance run parameters for coremark.
CoreMark Size    : 666
Total ticks      : 11144930
Total time (secs): 111
Iterations/Sec   : 0
Iterations       : 30
Compiler version : clang 18.1.3 (1ubuntu1)
Compiler flags   : -O2
Memory location  : STATIC
seedcrc          : 0xe9f5
[0]crclist       : 0xe714
[0]crcmatrix     : 0x1fd7
[0]crcstate      : 0x8e3a
[0]crcfinal      : 0xf8b3
Correct operation validated. See README.md for run and reporting rules.
bench: coremark iterations=30 cycles=11144930 instret=11144946
"""

DHRYSTONE = """\

Dhrystone Benchmark, Version C, Version 2.2
Program compiled without 'register' attribute
Using rdcycle(), HZ=1000000

Trying 500 runs through Dhrystone:
bench: dhrystone cycles=443063 instret=443075
Final values of the variables used in the benchmark:

Int_Glob:            5
        should be:   5
Bool_Glob:           1
        should be:   1
Ch_1_Glob:           A
        should be:   A
Ch_2_Glob:           B
        should be:   B
Arr_1_Glob[8]:       7
        should be:   7
Arr_2_Glob[8][7]:    510
        should be:   Number_Of_Runs + 10
Ptr_Glob->
  Ptr_Comp:          -2147221680
        should be:   (implementation-dependent)
  Discr:             0
        should be:   0
  Enum_Comp:         2
        should be:   2
  Int_Comp:          17
        should be:   17
  Str_Comp:          DHRYSTONE PROGRAM, SOME STRING
        should be:   DHRYSTONE PROGRAM, SOME STRING
Next_Ptr_Glob->
  Ptr_Comp:          -2147221680
        should be:   (implementation-dependent), same as above
  Discr:             0
        should be:   0
  Enum_Comp:         1
        should be:   1
  Int_Comp:          18
        should be:   18
  Str_Comp:          DHRYSTONE PROGRAM, SOME STRING
        should be:   DHRYSTONE PROGRAM, SOME STRING
Int_1_Loc:           5
        should be:   5
Int_2_Loc:           13
        should be:   13
Int_3_Loc:           7
        should be:   7
Enum_Loc:            1
        should be:   1
Str_1_Loc:           DHRYSTONE PROGRAM, 1'ST STRING
        should be:   DHRYSTONE PROGRAM, 1'ST STRING
Str_2_Loc:           DHRYSTONE PROGRAM, 2'ND STRING
        should be:   DHRYSTONE PROGRAM, 2'ND STRING

Microseconds for one run through Dhrystone: 886
Dhrystones per Second:                      1128
"""


class BenchCheckTest(unittest.TestCase):
    def test_recorded_consoles_pass(self):
        self.assertEqual(check_coremark(COREMARK), 30)
        self.assertEqual(check_dhrystone(DHRYSTONE), 500)
        self.assertEqual(timing_lines(COREMARK), Timing("coremark", 30, 11144930, 11144946))
        self.assertEqual(timing_lines(DHRYSTONE), Timing("dhrystone", None, 443063, 443075))
        row = measure("emulator", DHRYSTONE, {}, Path("dhrystone-im.bin"))
        self.assertEqual((row.image, row.name, row.iterations, row.cycles, row.instret),
                         ("dhrystone-im", "dhrystone", 500, 443063, 443075))
        self.assertEqual(measure("verilator", COREMARK, {}, Path("coremark-im.bin")).iterations, 30)

    def test_coremark_rejects_a_wrong_crc_or_its_own_error(self):
        for bad in (COREMARK.replace("0x1fd7", "0x1fd8"), COREMARK.replace("Correct operation validated.", "Errors detected"),
                    COREMARK.replace("Iterations       :", "ERROR! Must execute for at least 10 secs for a valid result!\nIterations       :"),
                    COREMARK.replace("seedcrc          : 0xe9f5\n", "")):
            with self.subTest(bad=bad[-120:]), self.assertRaises(BenchError):
                check_coremark(bad)

    def test_dhrystone_rejects_a_wrong_value(self):
        for old, new in (("Int_Glob:            5", "Int_Glob:            6"),
                         ("Arr_2_Glob[8][7]:    510", "Arr_2_Glob[8][7]:    511"),
                         ("Str_2_Loc:           DHRYSTONE PROGRAM, 2'ND STRING", "Str_2_Loc:           DHRYSTONE PROGRAM, 3'RD STRING")):
            with self.subTest(old=old):
                self.assertIn(old, DHRYSTONE)
                with self.assertRaises(BenchError):
                    check_dhrystone(DHRYSTONE.replace(old, new))
        with self.assertRaises(BenchError):
            check_dhrystone(DHRYSTONE.replace("Trying 500 runs through Dhrystone:", "Trying runs:"))
        # The two record pointers must agree with each other.
        first = DHRYSTONE.index("Ptr_Comp:")
        pointer = DHRYSTONE[first:].split("\n", 1)[0].split(":", 1)[1].strip()
        with self.assertRaises(BenchError):
            check_dhrystone(DHRYSTONE[:first] + DHRYSTONE[first:].replace(pointer, "12", 1))

    def test_timing_lines_are_one_well_formed_line(self):
        timing = COREMARK.splitlines()[-1] + "\n"
        for bad in (COREMARK.replace("bench: coremark", "bench: coremark extra=1"),
                    COREMARK + "bench: again cycles=1 instret=1\n",
                    COREMARK + timing,  # the same line twice
                    "no timing here\n"):
            with self.subTest(bad=bad[-80:]), self.assertRaises(BenchError):
                timing_lines(bad)

    def test_measure_refuses_what_it_cannot_report(self):
        """A run whose numbers cannot be reported is a failure of that image, not a crash."""
        for backend_console in (
                COREMARK.replace("iterations=30", "iterations=31"),  # the timing line disagrees with CoreMark
                COREMARK.replace("cycles=11144930", "cycles=0"),
                COREMARK.replace("instret=11144946", "instret=0"),
                COREMARK.replace("Iterations       : 30", "Iterations       : 0").replace("iterations=30 ", ""),
                DHRYSTONE.replace("cycles=443063", "cycles=0"),
                DHRYSTONE.replace("Trying 500", "Trying 0").replace("510", "10"),
                DHRYSTONE.replace("bench: dhrystone", "bench: whetstone")):
            with self.subTest(console=backend_console[-300:]), self.assertRaises(BenchError):
                measure("emulator", backend_console, {}, Path("x.bin"))
        # Without iterations= the CoreMark line is still usable: CoreMark's own count is used.
        self.assertEqual(measure("emulator", COREMARK.replace("iterations=30 ", ""), {}, Path("x.bin")).iterations, 30)

    def test_comparison_sets_aside_only_device_time(self):
        other = COREMARK.replace("Total ticks      : 11144930", "Total ticks      : 55000000").replace(
            "cycles=11144930", "cycles=55000000").replace("Total time (secs): 111", "Total time (secs): 550")
        self.assertEqual(comparable(other), comparable(COREMARK))
        self.assertNotEqual(comparable(COREMARK.replace("0xf8b3", "0xf8b4")), comparable(COREMARK))


def passing_run(console, cycles=100):
    return Run(0, console, "", "", [], {"halt": "done", "outcome": "pass", "cycles": cycles})


class BenchMainTest(unittest.TestCase):
    """main() on stubbed backends: every backend must print the same console and retire the same count."""

    def bench(self, emulator_console, rtl_console, rtl_run=None):
        with tempfile.TemporaryDirectory() as directory:
            image = Path(directory) / "coremark-im.bin"
            image.write_bytes(bytes(8))
            rtl = rtl_run or passing_run(rtl_console)
            with mock.patch.object(rv32_bench, "run_emulator", return_value=passing_run(emulator_console)), \
                    mock.patch.object(rv32_bench, "run_rtl", return_value=rtl) as run_rtl, \
                    mock.patch("sys.stdout"), mock.patch("sys.stderr"):
                rv32_bench.main([str(image), "--backend", "emulator", "--backend", "verilator", "--out", directory])
            self.assertIsNone(run_rtl.call_args.args[2], "the bench runs without a trace")

    def test_agreeing_backends_pass(self):
        rtl = COREMARK.replace("cycles=11144930", "cycles=55000000").replace("Total ticks      : 11144930",
                                                                            "Total ticks      : 55000000")
        self.bench(COREMARK, rtl)

    def test_a_cross_backend_difference_fails_the_image(self):
        for name, rtl in (("console", COREMARK.replace("Compiler flags   : -O2", "Compiler flags   : -O3")),
                          ("instret", COREMARK.replace("instret=11144946", "instret=11144947"))):
            with self.subTest(name=name), self.assertRaises(SystemExit) as raised:
                self.bench(COREMARK, rtl)
            self.assertIn("1 image(s) failed: coremark-im", str(raised.exception))

    def test_a_failed_run_fails_the_image(self):
        for run in (passing_run(COREMARK)._replace(status=1), passing_run(COREMARK)._replace(noise="%Error: x\n"),
                    passing_run(COREMARK)._replace(halt={"halt": "limit", "outcome": "error=limit", "cycles": 1})):
            with self.subTest(run=run.status), self.assertRaises(SystemExit) as raised:
                self.bench(COREMARK, None, rtl_run=run)
            self.assertIn("1 image(s) failed", str(raised.exception))


if __name__ == "__main__":
    unittest.main()
