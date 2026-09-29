"""The benchmark runner's checks (tools/rv32_bench.py) on recorded consoles.

The consoles are the emulator's output for the RV32IM images. Each check must accept them and
reject each way a run could go wrong: a CRC or a final value off, a missing or malformed timing
line, CoreMark's own error report.
"""
import unittest

from tools.rv32_bench import BenchError, check_coremark, check_dhrystone, comparable, timing_lines

COREMARK = """\
2K performance run parameters for coremark.
CoreMark Size    : 666
Total ticks      : 11144930
Total time (secs): 11
Iterations/Sec   : 2
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
        check_coremark(COREMARK)
        check_dhrystone(DHRYSTONE)
        self.assertEqual(timing_lines(COREMARK), {"coremark": (30, 11144930, 11144946)})
        self.assertEqual(timing_lines(DHRYSTONE), {"dhrystone": (None, 443063, 443075)})

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
        # The two record pointers must agree with each other.
        first = DHRYSTONE.index("Ptr_Comp:")
        pointer = DHRYSTONE[first:].split("\n", 1)[0].split(":", 1)[1].strip()
        with self.assertRaises(BenchError):
            check_dhrystone(DHRYSTONE[:first] + DHRYSTONE[first:].replace(pointer, "12", 1))

    def test_timing_lines_are_one_well_formed_line(self):
        with self.assertRaises(BenchError):
            timing_lines(COREMARK.replace("bench: coremark", "bench: coremark extra=1"))
        with self.assertRaises(BenchError):
            timing_lines(COREMARK + "bench: again cycles=1 instret=1\n")
        with self.assertRaises(BenchError):
            timing_lines("no timing here\n")

    def test_comparison_sets_aside_only_device_time(self):
        other = COREMARK.replace("Total ticks      : 11144930", "Total ticks      : 55000000").replace(
            "cycles=11144930", "cycles=55000000").replace("Total time (secs): 11", "Total time (secs): 55")
        self.assertEqual(comparable(other), comparable(COREMARK))
        self.assertNotEqual(comparable(COREMARK.replace("0xf8b3", "0xf8b4")), comparable(COREMARK))


if __name__ == "__main__":
    unittest.main()
