"""Track 2, O1: interrupts on both backends (docs/rv32-os.md).

Directed programs exercise the interrupt CSRs, CLINT and PLIC interrupts, wfi and the PLIC's
registers. In the RTL's step-tick mode (`+ticks=steps`) device time is counted as on the
emulator, so these programs are compared trace for trace, interrupt lines included; with cycle
ticks they are checked for what must hold on any backend. Expected values are written here by
hand from the privileged specification and the PLIC contract, so neither backend is checked only
against the other.
"""
import unittest

from rv32_step_case import DISARM_SOFTWARE, DISARM_TIMER, StepTicksCase, set_timer, stored
from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_rtl import diff_traces, trap_records

HANDLER = RAM + 0x400  # handlers live here; bodies must stay below
SAVE = RAM + 0x2000    # where handlers store what they saw


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


def dump(*regs):
    """Store registers at SAVE + 0x40 onwards, then finish: the trace shows each store's value."""
    words = LI(28, SAVE + 0x40)
    for i, reg in enumerate(regs):
        words.append(SW(reg, 28, 4 * i))
    return words + FINISH()


class InterruptTest(StepTicksCase):

    def test_the_interrupt_csrs(self):
        words = [CSRRS(1, MSTATUS, 0)]                     # reset value
        # MIE and MPIE are writable, and since issue #20 SIE, SPIE, SPP, MPRV, SUM, MXR, TVM, TW, TSR
        words += LI(2, 0xFFFFFFFF) + [CSRRW(0, MSTATUS, 2), CSRRS(3, MSTATUS, 0)]
        words += [CSRRW(0, MSTATUS, 0), CSRRS(4, MSTATUS, 0)]  # MPP = 0 is user mode (O5)
        words += [CSRRW(0, MIE_CSR, 2), CSRRS(5, MIE_CSR, 0), CSRRW(0, MIE_CSR, 0)]  # the M and S enables
        # mip: the devices' bits are read-only and nothing is pending; software raises SSIP, STIP, SEIP
        words += [CSRRW(0, MIP_CSR, 2), CSRRS(6, MIP_CSR, 0), CSRRW(0, MIP_CSR, 0)]
        words += LI(7, 0x12345678) + [CSRRW(8, MSCRATCH, 7), CSRRS(9, MSCRATCH, 0)]
        emulator, rtl = self.assert_same(words + dump(1, 3, 4, 5, 6, 8, 9))
        got = [stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(7)]
        self.assertEqual(got, [MSTATUS_RESET, MSTATUS_RESET | 0x7E01AA, MSTATUS_RESET & ~MSTATUS_MPP, 0xAAA, 0x222, 0,
                               0x12345678])

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
        # mret: MIE from MPIE, MPIE set, back to machine mode with MPP now user (O5)
        self.assertEqual(stored(rtl.trace, SAVE + 0x44), (MSTATUS_RESET & ~MSTATUS_MPP) | 0x88)
        self.assertEqual(stored(rtl.trace, SAVE + 0x40), 42)
        self.assertEqual((emulator.halt["interrupts"], rtl.halt["interrupts"]), (1, 1))
        self.assertEqual(trap_records(rtl.trace), [], "an interrupt is not a trap record")

    def test_masking_then_msi_before_mti(self):
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

    def test_mei_msi_mti_all_pending_are_taken_in_that_order(self):
        # An input event (MEI through the PLIC), MSIP and a due timer, all pending before MIE is
        # set: MEI first, then MSI, then MTI. The handler removes each cause and logs mcause.
        body = LI(1, PLIC + 4 * PLIC_SOURCE_INPUT) + LI(2, 1) + [SW(2, 1, 0)]
        body += LI(1, PLIC_ENABLE) + LI(2, 1 << PLIC_SOURCE_INPUT) + [SW(2, 1, 0)]
        body += set_timer(0) + LI(1, MSIP) + LI(2, 1) + [SW(2, 1, 0)]
        body += LI(3, (1 << 3) | (1 << 7) | (1 << 11)) + [CSRRW(0, MIE_CSR, 3), CSRRS(4, MIP_CSR, 0), CSRRSI(0, MSTATUS, 8),
                                                          CSRRCI(0, MSTATUS, 8)] + dump(4)
        log = LI(28, SAVE) + [LW(29, 28, 12), SLLI(30, 29, 2), ADD(30, 30, 28), CSRRS(31, MCAUSE, 0), SW(31, 30, 16),
                              ADDI(29, 29, 1), SW(29, 28, 12), ANDI(31, 31, 15)]
        external = LI(20, PLIC_CLAIM) + [LW(21, 20, 0)] + LI(22, INPUT) + [LW(23, 22, 0), BNE(23, 0, -4), SW(21, 20, 0), MRET()]
        software = DISARM_SOFTWARE + [MRET()]
        handler = log + [ADDI(30, 0, 11), BNE(31, 30, 4 * (1 + len(external)))] + external \
            + [ADDI(30, 0, 3), BNE(31, 30, 4 * (1 + len(software)))] + software + DISARM_TIMER + [MRET()]
        emulator, rtl = self.assert_same(at_handler(body, handler), input_script="frame 0 down A\n")
        self.assertEqual(stored(rtl.trace, SAVE + 0x40), (1 << 3) | (1 << 7) | (1 << 11))
        self.assertEqual([stored(rtl.trace, SAVE + 16 + 4 * i) for i in range(3)], [0x8000000B, 0x80000003, 0x80000007])
        self.assertEqual((emulator.halt["interrupts"], rtl.halt["interrupts"]), (3, 3))

    def test_two_plic_sources_arbitrate_and_virtio_interrupts(self):
        # A virtio-blk read of sector 0 (O3) raises source 1 while an input event holds source 12.
        # Higher priority wins, a tie goes to the lower number, priority 0 never interrupts (it
        # stays pending), and the virtio completion is then taken as a trap through MEI.
        ring = RAM + 0x3000
        desc, avail, used, header, data, status = ring, ring + 0x100, ring + 0x200, ring + 0x300, ring + 0x400, ring + 0x600
        words = []

        def put(address, value):
            words.extend(LI(1, address) + LI(2, value) + [SW(2, 1, 0)])

        for i, (address, length, flags) in enumerate(((header, 16, 1 | 1 << 16), (data, 512, 3 | 2 << 16), (status, 1, 2))):
            put(desc + 16 * i, address)
            put(desc + 16 * i + 8, length)
            put(desc + 16 * i + 12, flags)
        put(avail, 1 << 16)                               # idx 1, ring[0] = descriptor 0 (header: IN, sector 0)
        for offset, value in ((0x070, 0), (0x070, 1), (0x070, 3), (0x070, 11), (0x030, 0), (0x038, 8), (0x080, desc),
                              (0x090, avail), (0x0A0, used), (0x044, 1), (0x070, 15), (0x050, 0)):
            put(VIRTIO + offset, value)                   # set up, then notify: served before the store retires
        prio = lambda source: PLIC + 4 * source  # noqa: E731
        put(prio(PLIC_SOURCE_VIRTIO), 2)
        put(prio(PLIC_SOURCE_INPUT), 5)
        put(PLIC_ENABLE, (1 << PLIC_SOURCE_VIRTIO) | (1 << PLIC_SOURCE_INPUT))
        words += LI(10, PLIC_CLAIM) + LI(11, PLIC_PENDING)
        words += [LW(3, 10, 0), LW(4, 10, 0), LW(5, 10, 0), SW(3, 10, 0), SW(4, 10, 0)]  # 12, then 1, then 0
        put(prio(PLIC_SOURCE_INPUT), 2)
        words += [LW(6, 10, 0), SW(6, 10, 0)]                                             # a tie: 1
        put(prio(PLIC_SOURCE_VIRTIO), 0)
        words += [LW(7, 10, 0), SW(7, 10, 0)]                                             # 12
        put(prio(PLIC_SOURCE_INPUT), 0)
        words += [LW(8, 10, 0), LW(9, 11, 0), CSRRS(12, MIP_CSR, 0)]                      # 0, both pending, no MEIP
        put(prio(PLIC_SOURCE_VIRTIO), 1)
        words += LI(1, 1 << 11) + [CSRRW(0, MIE_CSR, 1), CSRRSI(0, MSTATUS, 8), CSRRCI(0, MSTATUS, 8), LW(13, 11, 0)]
        handler = LI(20, PLIC_CLAIM) + [LW(21, 20, 0)] + LI(22, VIRTIO) + [ADDI(23, 0, 1), SW(23, 22, 0x064), SW(21, 20, 0)] \
            + LI(28, SAVE) + [SW(21, 28, 0), CSRRS(29, MCAUSE, 0), SW(29, 28, 4), MRET()]
        emulator, rtl = self.assert_same(at_handler(words + dump(3, 4, 5, 6, 7, 8, 9, 12, 13), handler),
                                         input_script="frame 0 down A\n")
        got = [stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(9)]
        both = (1 << PLIC_SOURCE_VIRTIO) | (1 << PLIC_SOURCE_INPUT)
        self.assertEqual(got, [PLIC_SOURCE_INPUT, PLIC_SOURCE_VIRTIO, 0, PLIC_SOURCE_VIRTIO, PLIC_SOURCE_INPUT,
                               0, both, 0, 1 << PLIC_SOURCE_INPUT])
        self.assertEqual((stored(rtl.trace, SAVE), stored(rtl.trace, SAVE + 4)), (PLIC_SOURCE_VIRTIO, 0x8000000B))

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
        self.assertEqual(got, [7, 0, (1 << PLIC_SOURCE_INPUT) | (1 << PLIC_SOURCE_VIRTIO), 6, 0, 0, 0])

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

    def test_pending_latches_until_claimed(self):
        # The gateway (PLIC specification, QEMU): the input's request stays pending after its line
        # drops (the queue drained), a claim still returns it and clears it, and completing it with
        # the line low leaves nothing pending.
        words = LI(1, PLIC + 4 * PLIC_SOURCE_INPUT) + LI(2, 1) + [SW(2, 1, 0)]
        words += LI(1, PLIC_ENABLE) + LI(2, 1 << PLIC_SOURCE_INPUT) + [SW(2, 1, 0)]
        words += LI(10, PLIC_CLAIM) + LI(11, PLIC_PENDING) + LI(12, INPUT)
        words += [LW(13, 12, 0), BNE(13, 0, -4)]                       # drain the queue: the line drops
        words += [LW(3, 11, 0), CSRRS(4, MIP_CSR, 0), LW(5, 10, 0), LW(6, 11, 0), SW(5, 10, 0), LW(7, 11, 0), LW(8, 10, 0)]
        emulator, rtl = self.assert_same(words + dump(3, 4, 5, 6, 7, 8), input_script="frame 0 down A\n")
        got = [stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(6)]
        self.assertEqual(got, [1 << PLIC_SOURCE_INPUT, 1 << 11, PLIC_SOURCE_INPUT, 0, 0, 0])

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
        body += [WFI(), CSRRS(3, MIP_CSR, 0), ANDI(3, 3, 1 << 7), BEQ(3, 0, -12)] + FINISH()
        emulator, rtl = self.run_both(body, ticks="cycles")
        self.assertEqual((emulator.halt["outcome"], rtl.halt["outcome"]), ("pass", "pass"))
        wfis = lambda trace: sum(line.split()[2] == f"{WFI():08x}" for line in trace)  # noqa: E731
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
                emulator, rtl = self.assert_same(words, seed=seed)
                self.assertEqual((emulator.halt["interrupts"], rtl.halt["interrupts"]), (1, 1))  # the timer fired
                self.assertEqual(stored(rtl.trace, SAVE), 0x80000007)


if __name__ == "__main__":
    unittest.main()
