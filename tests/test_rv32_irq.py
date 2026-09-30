"""Track 2, O1: interrupts on both backends (docs/rv32-os.md).

Directed programs exercise the interrupt CSRs, CLINT and PLIC interrupts, wfi and the PLIC's
registers. In the RTL's step-tick mode (`+ticks=steps`) device time is counted as on the
emulator, so these programs are compared trace for trace, interrupt lines included; with cycle
ticks they are checked for what must hold on any backend. Expected values are written here by
hand from the privileged specification and the PLIC contract, so neither backend is checked only
against the other.
"""
import os
import tempfile
import unittest
from pathlib import Path

import test_rv32_rtl as integer_tests
from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_rtl import diff_traces, run_emulator, run_rtl, trap_records, write_image

MIE_CSR, MIP_CSR, MSCRATCH = 0x304, 0x344, 0x340
WFI = 0x10500073
HANDLER = RAM + 0x400  # handlers live here; bodies must stay below
SAVE = RAM + 0x2000    # where handlers store what they saw
MSTATUS_RESET = 0x80007800  # SD, FS = 3 and MPP = 3 read as constants


def at_handler(body, handler):
    """`body`, padded to HANDLER, then `handler`; mtvec is set by the body's first words."""
    words = LI(5, HANDLER) + [CSRRW(0, MTVEC, 5)] + list(body)
    assert len(words) <= (HANDLER - RAM) // 4, "the body overlaps the handler"
    return words + [0] * ((HANDLER - RAM) // 4 - len(words)) + list(handler)


def record_and_return(disarm):
    """A handler that stores mcause, mepc and mstatus at SAVE (+0, +4, +8, bumping a count at +12),
    runs `disarm` to remove the interrupt's cause, and returns to mepc."""
    return LI(28, SAVE) + [
        CSRRS(29, MCAUSE, 0), SW(29, 28, 0),
        CSRRS(29, MEPC, 0), SW(29, 28, 4),
        CSRRS(29, MSTATUS, 0), SW(29, 28, 8),
        LW(29, 28, 12), ADDI(29, 29, 1), SW(29, 28, 12),
    ] + list(disarm) + [MRET()]


DISARM_TIMER = LI(27, MTIMECMP) + [ADDI(26, 0, -1), SW(26, 27, 0), SW(26, 27, 4)]
DISARM_SOFTWARE = LI(27, MSIP) + [SW(0, 27, 0)]


def set_timer(value):
    """mtimecmp = value (below 2^32): low word to all ones first, then high 0, then the low word."""
    return LI(27, MTIMECMP) + [ADDI(26, 0, -1), SW(26, 27, 0), SW(0, 27, 4)] + LI(26, value) + [SW(26, 27, 0)]


def dump(*regs):
    """Store registers at SAVE + 0x40 onwards, then finish: the trace shows each store's value."""
    words = LI(28, SAVE + 0x40)
    for i, reg in enumerate(regs):
        words.append(SW(reg, 28, 4 * i))
    return words + FINISH()


def stored(trace, address):
    """The last value a trace line stored at `address`, or None."""
    value = None
    for line in trace:
        if f"mem[{address:08x}]<-" in line:
            value = int(line.split(f"mem[{address:08x}]<-")[1].split("/")[0], 16)
    return value


class InterruptTest(unittest.TestCase):
    setUpClass = classmethod(integer_tests.RtlTest.setUpClass.__func__)
    tearDownClass = classmethod(integer_tests.RtlTest.tearDownClass.__func__)

    def run_both(self, words, ticks="steps", stall=None, seed=None, input_script=None, limit=200000, halt="done"):
        with tempfile.TemporaryDirectory(dir=self.workdir.name) as directory:
            hex_path, bin_path = write_image(words, directory, "image")
            script = None
            if input_script is not None:
                script = Path(directory) / "input.txt"
                script.write_text(input_script)
            emulator = run_emulator(self.emulator, bin_path, Path(directory) / "emu.trace", limit=limit, input_script=script)
            rtl = run_rtl(self.simulator, hex_path, Path(directory) / "rtl.trace", stall=stall, seed=seed,
                          input_script=script, ticks=ticks, max_cycles=4000000)
        report = f"\n--- simulator ---\n{rtl.noise}--- console ---\n{rtl.console}--- stderr ---\n{rtl.stderr}{emulator.stderr}"
        self.assertEqual(rtl.status, 0, report)
        self.assertEqual(rtl.noise, "", report)
        self.assertEqual(emulator.halt["halt"], halt, report)
        self.assertEqual(rtl.halt["halt"], halt, report)
        return emulator, rtl

    def assert_same(self, words, **kwargs):
        """Step ticks: identical traces and a pass on both backends."""
        emulator, rtl = self.run_both(words, **kwargs)
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertEqual((emulator.halt["outcome"], rtl.halt["outcome"]), ("pass", "pass"), emulator.stderr + rtl.stderr)
        self.assertEqual(rtl.halt["steps"], len(rtl.trace))
        return emulator, rtl

    def test_the_interrupt_csrs(self):
        words = [CSRRS(1, MSTATUS, 0)]                     # reset value
        words += LI(2, 0xFFFFFFFF) + [CSRRW(0, MSTATUS, 2), CSRRS(3, MSTATUS, 0)]  # only MIE and MPIE are writable
        words += [CSRRW(0, MSTATUS, 0), CSRRS(4, MSTATUS, 0)]
        words += [CSRRW(0, MIE_CSR, 2), CSRRS(5, MIE_CSR, 0), CSRRW(0, MIE_CSR, 0)]  # MSIE, MTIE, MEIE
        words += [CSRRW(0, MIP_CSR, 2), CSRRS(6, MIP_CSR, 0)]  # mip is read-only: nothing pending, nothing set
        words += LI(7, 0x12345678) + [CSRRW(8, MSCRATCH, 7), CSRRS(9, MSCRATCH, 0)]
        emulator, rtl = self.assert_same(words + dump(1, 3, 4, 5, 6, 8, 9))
        got = [stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(7)]
        self.assertEqual(got, [MSTATUS_RESET, MSTATUS_RESET | 0x88, MSTATUS_RESET, 0x888, 0, 0, 0x12345678])

    def test_a_timer_interrupt_is_taken_at_the_next_boundary_and_mret_restores_mie(self):
        body = set_timer(0) + LI(1, 1 << 7) + [CSRRW(0, MIE_CSR, 1), CSRRSI(0, MSTATUS, 8),
                                               ADDI(10, 0, 42),           # the interrupt comes before this
                                               CSRRS(11, MSTATUS, 0)] + dump(10, 11)
        words = at_handler(body, record_and_return(DISARM_TIMER))
        emulator, rtl = self.assert_same(words)
        lines = [line for line in rtl.trace if " interrupt " in line]
        self.assertEqual(len(lines), 1, lines)
        csrrsi = next(i for i, line in enumerate(rtl.trace) if line.split()[2] == f"{CSRRSI(0, MSTATUS, 8):08x}")
        entry = rtl.trace[csrrsi + 1]
        after = f"{int(rtl.trace[csrrsi].split()[1], 16) + 4:08x}"
        self.assertEqual(entry.split()[1:], [after, "00000000", "interrupt", "7"], entry)
        self.assertEqual(stored(rtl.trace, SAVE), 0x80000007)
        self.assertEqual(stored(rtl.trace, SAVE + 4), int(after, 16))
        self.assertEqual(stored(rtl.trace, SAVE + 8), MSTATUS_RESET | 0x80)   # MIE clear, MPIE set in the handler
        self.assertEqual(stored(rtl.trace, SAVE + 0x44), MSTATUS_RESET | 0x88)  # mret: MIE from MPIE, MPIE set
        self.assertEqual(stored(rtl.trace, SAVE + 0x40), 42)
        self.assertEqual((emulator.halt["interrupts"], rtl.halt["interrupts"]), (1, 1))
        self.assertEqual(trap_records(rtl.trace), [], "an interrupt is not a trap record")

    def test_masking_msi_before_mti_and_mei_first(self):
        # Both CLINT interrupts pending with MIE clear: nothing happens until MIE is set, then MSI
        # and MTI in that order. The handler counts; the stored causes show the order.
        body = set_timer(0) + LI(1, MSIP) + LI(2, 1) + [SW(2, 1, 0)]
        body += LI(3, (1 << 3) | (1 << 7)) + [CSRRW(0, MIE_CSR, 3), CSRRS(4, MIP_CSR, 0), CSRRSI(0, MSTATUS, 8),
                                              CSRRCI(0, MSTATUS, 8)] + dump(4)
        handler = LI(28, SAVE) + [LW(29, 28, 12), SLLI(30, 29, 2), ADD(30, 30, 28), CSRRS(31, MCAUSE, 0), SW(31, 30, 16),
                                  ADDI(29, 29, 1), SW(29, 28, 12),
                                  ANDI(31, 31, 15), ADDI(30, 0, 3), BNE(31, 30, 4 * (len(DISARM_SOFTWARE) + 2))] + DISARM_SOFTWARE + [JAL(0, 4 + 4 * len(DISARM_TIMER))] \
            + DISARM_TIMER + [MRET()]
        emulator, rtl = self.assert_same(at_handler(body, handler))
        self.assertEqual(stored(rtl.trace, SAVE + 0x40), (1 << 3) | (1 << 7))
        self.assertEqual([stored(rtl.trace, SAVE + 16), stored(rtl.trace, SAVE + 20)], [0x80000003, 0x80000007])

    def test_an_external_interrupt_from_the_input_queue(self):
        # Frame 0's event is queued before the first instruction. With priority 1 and threshold 0
        # MEIP is set; the handler claims (12), drains the queue and completes; a claim then returns 0.
        body = LI(1, PLIC + 4 * PLIC_SOURCE_INPUT) + LI(2, 1) + [SW(2, 1, 0)]
        body += LI(1, PLIC_ENABLE) + LI(2, 1 << PLIC_SOURCE_INPUT) + [SW(2, 1, 0)]
        body += [CSRRS(3, MIP_CSR, 0)] + LI(1, 1 << 11) + [CSRRW(0, MIE_CSR, 1), CSRRSI(0, MSTATUS, 8), CSRRCI(0, MSTATUS, 8)]
        body += LI(1, PLIC_CLAIM) + [LW(4, 1, 0)] + LI(1, PLIC_PENDING) + [LW(5, 1, 0)] + dump(3, 4, 5)
        handler = LI(20, PLIC_CLAIM) + [LW(21, 20, 0)] + LI(22, INPUT) + [LW(23, 22, 0), BNE(23, 0, -4)] \
            + [SW(21, 20, 0)] + LI(28, SAVE) + [SW(21, 28, 0), CSRRS(29, MCAUSE, 0), SW(29, 28, 4), MRET()]
        emulator, rtl = self.assert_same(at_handler(body, handler), input_script="frame 0 down SPACE\nframe 0 up SPACE\n")
        self.assertEqual(stored(rtl.trace, SAVE + 0x40), 1 << 11)   # MEIP before the handler
        self.assertEqual(stored(rtl.trace, SAVE), PLIC_SOURCE_INPUT)  # the claim
        self.assertEqual(stored(rtl.trace, SAVE + 4), 0x8000000B)
        self.assertEqual(stored(rtl.trace, SAVE + 0x44), 0)          # nothing left to claim
        self.assertEqual(stored(rtl.trace, SAVE + 0x48), 0)          # nothing pending

    def test_plic_registers(self):
        words = []
        regs = []

        def load(reg, address):
            words.extend(LI(1, address) + [LW(reg, 1, 0)])
            regs.append(reg)

        words += LI(1, PLIC + 4 * PLIC_SOURCE_INPUT) + LI(2, 0xFF) + [SW(2, 1, 0)]  # 3 bits kept
        load(3, PLIC + 4 * PLIC_SOURCE_INPUT)
        words += LI(1, PLIC + 4 * 5) + [SW(2, 1, 0)]                               # an unwired source
        load(4, PLIC + 4 * 5)
        words += LI(1, PLIC_ENABLE) + LI(2, 0xFFFFFFFF) + [SW(2, 1, 0)]           # only wired sources
        load(5, PLIC_ENABLE)
        words += LI(1, PLIC_THRESHOLD) + LI(2, 0xFE) + [SW(2, 1, 0)]
        load(6, PLIC_THRESHOLD)
        load(7, PLIC_CLAIM)                                                        # nothing pending
        load(8, PLIC_PENDING)
        words += [CSRRS(9, MIP_CSR, 0)]
        regs.append(9)
        emulator, rtl = self.assert_same(words + dump(*regs))
        got = [stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(len(regs))]
        self.assertEqual(got, [7, 0, 1 << PLIC_SOURCE_INPUT, 6, 0, 0, 0])

    def test_plic_faults(self):
        # Byte access, a store to the pending word, an unimplemented offset and the byte past the
        # window each fault with the address in mtval; a handler skips each one.
        skip = [CSRRS(29, MEPC, 0), ADDI(29, 29, 4), CSRRW(0, MEPC, 29), MRET()]
        body = []
        for word, address in ((LB(2, 1, 0), PLIC_CLAIM), (SW(0, 1, 0), PLIC_PENDING), (LW(2, 1, 0), PLIC + 0x3000),
                              (LW(2, 1, 0), PLIC + PLIC_SIZE), (SH(0, 1, 0), PLIC_THRESHOLD)):
            body += LI(1, address) + [word]
        emulator, rtl = self.assert_same(at_handler(body + FINISH(), skip))
        records = [line.split()[3:] for line in trap_records(rtl.trace)]
        self.assertEqual(records, [["5", f"{PLIC_CLAIM:08x}"], ["7", f"{PLIC_PENDING:08x}"], ["5", f"{PLIC + 0x3000:08x}"],
                                   ["5", f"{PLIC + PLIC_SIZE:08x}"], ["7", f"{PLIC_THRESHOLD:08x}"]])

    def test_claim_complete_protocol(self):
        # A claimed source is not pending until completed, even while its line stays high; a
        # completion for a disabled source is ignored; completing the enabled one re-pends it.
        words = LI(1, PLIC + 4 * PLIC_SOURCE_INPUT) + LI(2, 3) + [SW(2, 1, 0)]
        words += LI(1, PLIC_ENABLE) + LI(2, 1 << PLIC_SOURCE_INPUT) + [SW(2, 1, 0)]
        words += LI(10, PLIC_CLAIM) + LI(11, PLIC_PENDING) + LI(12, PLIC_ENABLE)
        words += [LW(3, 10, 0), LW(4, 11, 0), LW(5, 10, 0)]           # claim 12, then nothing pending, claim 0
        words += [SW(0, 12, 0), SW(3, 10, 0), LW(6, 11, 0)]            # disabled: completion ignored
        words += [SW(2, 12, 0), SW(3, 10, 0), LW(7, 11, 0)]            # enabled: completed, pending again
        emulator, rtl = self.assert_same(words + dump(3, 4, 5, 6, 7), input_script="frame 0 down A\n")
        got = [stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(5)]
        self.assertEqual(got, [PLIC_SOURCE_INPUT, 0, 0, 0, 1 << PLIC_SOURCE_INPUT])

    def test_step_ticks_make_time_trace_comparable(self):
        # mtime, time and cycle read the steps before the reading instruction, as on the emulator.
        words = LI(1, MTIME) + [LW(2, 1, 0), RDTIME(3), RDCYCLE(4), ADDI(0, 0, 0), LW(5, 1, 0)]
        words += LI(6, 1000) + [SW(6, 1, 0), LW(7, 1, 0), RDTIME(8)]
        emulator, rtl = self.assert_same(words + dump(2, 3, 4, 5, 7, 8), seed=3)
        got = [stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(6)]
        self.assertEqual(got, [2, 3, 4, 6, 1001, 1002])

    def test_wfi_waits_on_the_rtl_with_cycle_ticks(self):
        # A timer interrupt 3,000 ticks ahead, MIE clear: wfi waits for the level (not for MIE) on
        # the RTL, so it retires once there; the emulator's wfi retires at once and the loop spins.
        body = LI(1, MTIME) + [LW(2, 1, 0), ADDI(2, 2, 2000), ADDI(2, 2, 1000)]
        body += LI(27, MTIMECMP) + [ADDI(26, 0, -1), SW(26, 27, 0), SW(0, 27, 4), SW(2, 27, 0)]
        body += LI(1, 1 << 7) + [CSRRW(0, MIE_CSR, 1)]
        body += [WFI, CSRRS(3, MIP_CSR, 0), ANDI(3, 3, 1 << 7), BEQ(3, 0, -12)] + FINISH()
        emulator, rtl = self.run_both(body, ticks="cycles")
        self.assertEqual((emulator.halt["outcome"], rtl.halt["outcome"]), ("pass", "pass"))
        wfis = lambda trace: sum(line.split()[2] == f"{WFI:08x}" for line in trace)  # noqa: E731
        self.assertEqual(wfis(rtl.trace), 1, "the RTL's wfi waited")
        self.assertGreater(wfis(emulator.trace), 100, "the emulator's wfi retires at once")
        self.assertGreater(rtl.halt["cycles"], 3000)
        with self.subTest("step ticks: wfi never waits"):
            self.assert_same(body)

    def test_interrupt_into_an_unmapped_handler_is_a_double_fault(self):
        words = set_timer(0) + LI(1, 1 << 7) + [CSRRW(0, MIE_CSR, 1), CSRRSI(0, MSTATUS, 8)] + FINISH()
        emulator, rtl = self.run_both(words, halt="double-fault")
        self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
        self.assertTrue(rtl.trace[-2].endswith(" 00000000 interrupt 7"), rtl.trace[-2])
        self.assertEqual(rtl.trace[-1], f"{len(rtl.trace)} 00000000 00000000 trap 1 00000000")

    def test_step_ticks_with_stalls_keep_the_trace(self):
        body = set_timer(300) + LI(1, 1 << 7) + [CSRRW(0, MIE_CSR, 1), CSRRSI(0, MSTATUS, 8)]
        body += LI(2, 0) + [ADDI(2, 2, 1)] + LI(3, 400) + [BLT(2, 3, -12)] + dump(2)
        words = at_handler(body, record_and_return(DISARM_TIMER))
        for seed in (1, 9):
            with self.subTest(seed=seed):
                self.assert_same(words, seed=seed)


if __name__ == "__main__":
    unittest.main()
