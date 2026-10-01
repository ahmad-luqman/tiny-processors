"""Issue #20: S-mode and Sv32 on both backends, the half mmucheck cannot cover (docs/rv32.md "Sv32").

mmucheck runs on QEMU as the reference. These directed programs cover what QEMU cannot referee
or does differently: a page-table read that PMP refuses, that lands in a device window or
outside RAM, PMP on the translated address, the order of a misaligned address against a page
fault, and the cycle formula with page-table walks. They also cover what mmucheck leaves out:
machine state from S mode, the sstatus mask, MPRV without translation, TW, and which interrupt
goes first. Each runs on the emulator and the RTL in step-tick mode and the traces
must be identical; the expected causes and addresses are written here by hand.

The page tables are data in the image. Machine mode sets satp and PMP and enters S mode with
mret; the S code runs at its physical address through an identity megapage, and U code through
a megapage alias with U set. One machine-mode handler logs (mcause, mtval, mepc, mstatus) for
the nth trap at LOG + 16 (n + 1), after the count at LOG. It returns past the instruction, to
x1 after a fetch fault, and to the interrupted instruction after an interrupt, with mip's
software bits cleared. It finishes the run on an ecall from U mode.
"""
import unittest

from rv32_step_case import StepTicksCase, stored
from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_rtl import cycle_relation, diff_traces, trap_records

INTERRUPT = 1 << 31

HANDLER = RAM + 0x400  # machine code below it
SUPER = RAM + 0x800    # S-mode code, at its own address through the identity megapage
USER_CODE = RAM + 0xC00
LOG = RAM + 0x2000
DUMP = RAM + 0x2800
ROOT = RAM + 0x4000
LEVEL0 = RAM + 0x5000
PAGE_S = RAM + 0x6000  # a supervisor page
PAGE_U = RAM + 0x7000  # a user page
GUARDED = RAM + 0x8000  # a page-table page PMP keeps from S mode
END = RAM + 0x9000

ALIAS = 0x40000000     # the image again, with U set
TEST_VA = 0x10000000   # the level-0 table's 4 KiB pages
DEVICE_TABLE_VA = 0x10400000   # a pointer to a "table" in the console's window
MISSING_TABLE_VA = 0x10800000  # a pointer to a "table" where nothing is mapped
GUARDED_TABLE_VA = 0x10C00000  # a pointer to a table PMP refuses
EXECUTE_ONLY_VA = 0x11000000   # a megapage of the image, X only
DEVICE_CODE_VA = 0x11400000    # an X megapage over the done register's window

NAPOT, R, W, X = 0x18, 0x01, 0x02, 0x04
RWXAD = PTE_V | PTE_R | PTE_W | PTE_X | PTE_A | PTE_D


def pte(physical, flags):
    return (physical >> 12) << 10 | flags


def tables():
    """The root table, the level-0 table and the guarded one, as words from ROOT to END."""
    root = [0] * 1024
    root[0] = pte(0, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D)  # an identity megapage over the done register
    root[RAM >> 22] = pte(RAM, RWXAD)
    root[ALIAS >> 22] = pte(RAM, RWXAD | PTE_U)
    root[TEST_VA >> 22] = pte(LEVEL0, PTE_V)
    root[DEVICE_TABLE_VA >> 22] = pte(CONSOLE, PTE_V)
    root[MISSING_TABLE_VA >> 22] = pte(0x00200000, PTE_V)
    root[GUARDED_TABLE_VA >> 22] = pte(GUARDED, PTE_V)
    root[EXECUTE_ONLY_VA >> 22] = pte(RAM, PTE_V | PTE_X | PTE_A)
    root[DEVICE_CODE_VA >> 22] = pte(0, PTE_V | PTE_X | PTE_A)
    level0 = [0] * 1024
    level0[0] = pte(PAGE_S, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D)
    level0[1] = pte(PAGE_U, PTE_V | PTE_R | PTE_W | PTE_U | PTE_A | PTE_D)
    level0[2] = pte(PAGE_S, PTE_R | PTE_W | PTE_A | PTE_D)  # V clear
    page_s = [0x5000 + i for i in range(1024)]
    page_u = [0x7000 + i for i in range(1024)]
    guarded = [pte(PAGE_S, PTE_V | PTE_R | PTE_A)] * 1024
    return root + level0 + page_s + page_u + guarded


def handler():
    """Logs each trap; after an interrupt clears mip's software bits and returns to the
    interrupted instruction; returns to x1 after a fetch fault (1 or 12), finishes on a U ecall
    (8), otherwise returns past the instruction in the mode it came from. Uses x26 to x31."""
    head = LI(28, LOG) + [
        LW(29, 28, 0), SLLI(30, 29, 4), ADD(30, 30, 28),
        CSRRS(31, MCAUSE, 0), SW(31, 30, 16),
        CSRRS(27, MTVAL, 0), SW(27, 30, 20),
        CSRRS(27, MEPC, 0), SW(27, 30, 24),
        CSRRS(27, MSTATUS, 0), SW(27, 30, 28),
        ADDI(29, 29, 1), SW(29, 28, 0),
    ]
    tail = [BGE(31, 0, 4 * 3), CSRRW(0, MIP_CSR, 0), MRET(),  # an interrupt: clear it, go back
            ADDI(26, 0, 8), BEQ(31, 26, 4 * 11),            # a user ecall: finish
            ADDI(26, 0, 1), BEQ(31, 26, 4 * 7),              # a fetch fault: to x1
            ADDI(26, 0, 12), BEQ(31, 26, 4 * 5),
            CSRRS(27, MEPC, 0), ADDI(27, 27, 4), CSRRW(0, MEPC, 27), MRET(),
            CSRRW(0, MEPC, 1), MRET()]
    return head + tail + FINISH()


def program(machine, supervisor, user=()):
    """`machine` then the entry to S mode at SUPER, the handler, the S and U code, the tables."""
    words = LI(5, HANDLER) + [CSRRW(0, MTVEC, 5)] + list(machine)
    words += LI(5, SATP_SV32 | ROOT >> 12) + [CSRRW(0, SATP, 5), SFENCE_VMA()]
    words += LI(5, SUPER) + [CSRRW(0, MEPC, 5)] + LI(6, 0x1800) + [CSRRC(0, MSTATUS, 6)]
    words += LI(6, 0x800) + [CSRRS(0, MSTATUS, 6), MRET()]  # MPP = S
    for at, code in ((HANDLER, handler()), (SUPER, supervisor), (USER_CODE, user), (ROOT, tables())):
        assert len(words) <= (at - RAM) // 4, f"code overlaps {at:#x}"
        words += [0] * ((at - RAM) // 4 - len(words)) + list(code)
    return words


def pmp(refused=None):
    """PMP for S and U mode: everything, and with `refused` that 4 KiB page refused first."""
    words = []
    if refused is not None:
        words += LI(5, (refused >> 2) | (0x1000 >> 3) - 1) + [CSRRW(0, PMPADDR0, 5)]
    words += LI(5, 0xFFFFFFFF) + [CSRRW(0, PMPADDR0 + 1, 5)]
    return words + LI(6, (NAPOT | R | W | X) << 8 | (0 if refused is None else NAPOT)) + [CSRRW(0, PMPCFG0, 6)]


class MmuTest(StepTicksCase):

    def log(self, trace):
        count = stored(trace, LOG) or 0
        return [tuple(stored(trace, LOG + 16 * (n + 1) + 4 * k) for k in range(4)) for n in range(count)]

    def test_translated_loads_stores_and_fetches_keep_the_cycle_formula(self):
        """No trap anywhere, so the testbench's cycle identity must hold with the walks: 4 cycles a
        step, 5 with a data access, plus stalls and the walk's own cycles; transfers are the fetches,
        the data accesses and the page-table reads."""
        supervisor = LI(10, TEST_VA) + [LW(11, 10, 8), SW(11, 10, 12), LW(12, 10, 12), LB(13, 10, 9)]
        supervisor += LI(14, PAGE_S) + [LW(15, 14, 0)]  # the same page through the identity megapage
        supervisor += LI(16, DUMP) + [SW(11, 16, 0), SW(12, 16, 4), SW(13, 16, 8)]
        supervisor += FINISH()  # the done register, through root[0]
        words = program(pmp(), supervisor)
        emulator, rtl = self.assert_same(words)
        self.assertEqual([stored(rtl.trace, DUMP + 4 * i) for i in range(3)], [0x5002, 0x5002, 0x50])
        self.assertIn(f"mem[{TEST_VA + 12:08x}]<-00005002/4", "\n".join(rtl.trace), "the trace shows the virtual address")
        # Counted from the trace, not the core: every S-mode fetch and data access walks once, one
        # read through a megapage and two through the level-0 table at TEST_VA. A walk adds one
        # cycle per read, plus the FETCH cycle that starts a fetch's walk or the XLATE after a
        # data access's.
        supervisor_lines = [line for line in emulator.trace if int(line.split()[1], 16) >= SUPER]
        accesses = [line for line in supervisor_lines if "mem[" in line]
        reads = len(supervisor_lines) + len(accesses) + sum(f"mem[{TEST_VA >> 12:05x}" in line for line in accesses)
        for timing in ({"ticks": None, "stall": 1}, {"ticks": None, "seed": 11}, {"ticks": "steps", "stall": 0}):
            with self.subTest(**timing):
                emulator, rtl = self.run_both(words, **timing)
                self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
                relation, holds = cycle_relation(rtl)
                self.assertTrue(holds, relation)
                self.assertEqual((rtl.halt["walks"], rtl.halt["ptw_waits"]),
                                 (reads, reads + len(supervisor_lines) + len(accesses)), relation)

    def test_user_fetches_through_the_alias_and_sret_enters_user_mode(self):
        user = LI(10, TEST_VA + 0x1000) + [LW(11, 10, 0)] + LI(12, TEST_VA) + [LW(13, 12, 0), ECALL()]
        supervisor = LI(5, ALIAS + (USER_CODE - RAM)) + [CSRRW(0, SEPC, 5)] + LI(6, MSTATUS_SPP) + [CSRRC(0, SSTATUS, 6), SRET()]
        emulator, rtl = self.assert_same(program(pmp(), supervisor, user))
        # The user page reads; the supervisor page faults in U mode (13); sret came from S with SPP = U.
        log = self.log(rtl.trace)
        self.assertEqual([entry[:2] for entry in log], [(13, TEST_VA), (8, 0)])
        self.assertEqual(log[0][2], ALIAS + (USER_CODE - RAM) + 4 * (len(LI(10, TEST_VA + 0x1000)) + 1 + len(LI(12, TEST_VA))))
        self.assertIn("->00007000/4", "\n".join(rtl.trace))

    def test_page_table_reads_only_from_ram_that_pmp_grants(self):
        supervisor = []
        for address in (DEVICE_TABLE_VA, MISSING_TABLE_VA, GUARDED_TABLE_VA):
            supervisor += LI(10, address) + [LW(11, 10, 0), SW(11, 10, 0), JALR(1, 10, 0)]
        supervisor += LI(10, DEVICE_CODE_VA + DONE) + [JALR(1, 10, 0)]  # a leaf over a device: no fetch
        supervisor += LI(10, TEST_VA + 0x2002) + [LW(11, 10, 0)]  # misaligned, in an invalid page
        supervisor += LI(10, TEST_VA + 0x2000) + [LW(11, 10, 0)]  # the page fault itself
        supervisor += FINISH()
        emulator, rtl = self.assert_same(program(pmp(refused=GUARDED), supervisor))
        expected = []
        for address in (DEVICE_TABLE_VA, MISSING_TABLE_VA, GUARDED_TABLE_VA):
            expected += [(5, address), (7, address), (1, address)]  # an access fault of the access's kind, tval the VA
        expected += [(1, DEVICE_CODE_VA + DONE)]
        # We check alignment before translating (the spec allows either order; QEMU translates first).
        expected += [(4, TEST_VA + 0x2002), (13, TEST_VA + 0x2000)]
        self.assertEqual([entry[:2] for entry in self.log(rtl.trace)], expected)
        self.assertEqual(trap_records(rtl.trace), trap_records(emulator.trace))

    def test_sum_and_mxr_from_supervisor_mode(self):
        supervisor = LI(10, TEST_VA + 0x1000) + [SW(0, 10, 0)]                       # a U page without SUM: 15
        supervisor += LI(6, MSTATUS_SUM) + [CSRRS(0, SSTATUS, 6), SW(0, 10, 4), LW(11, 10, 4)]  # with SUM
        supervisor += [CSRRC(0, SSTATUS, 6)]
        supervisor += LI(12, EXECUTE_ONLY_VA + (PAGE_S - RAM)) + [LW(13, 12, 0)]    # X only: 13
        supervisor += LI(6, MSTATUS_MXR) + [CSRRS(0, SSTATUS, 6), LW(13, 12, 0)]     # with MXR: reads
        supervisor += LI(16, DUMP) + [SW(11, 16, 0), SW(13, 16, 4)] + FINISH()
        emulator, rtl = self.assert_same(program(pmp(), supervisor))
        self.assertEqual([entry[:2] for entry in self.log(rtl.trace)],
                         [(15, TEST_VA + 0x1000), (13, EXECUTE_ONLY_VA + (PAGE_S - RAM))])
        self.assertEqual([stored(rtl.trace, DUMP), stored(rtl.trace, DUMP + 4)], [0, 0x5000])

    def test_supervisor_instructions_and_their_traps(self):
        """sfence.vma and wfi are legal in S mode; TVM, TW and TSR make them, satp and sret illegal;
        a delegated breakpoint goes to stvec with SPP = S; ecall from S is cause 9."""
        handler_s = [CSRRS(20, SCAUSE, 0), CSRRS(21, SEPC, 0), CSRRS(22, SSTATUS, 0),
                     ADDI(21, 21, 4), CSRRW(0, SEPC, 21), SRET()]
        at = SUPER + 0x200
        supervisor = LI(5, at) + [CSRRW(0, STVEC, 5), SFENCE_VMA(), WFI(), EBREAK(), ECALL()]
        supervisor += LI(16, DUMP) + [SW(20, 16, 0), SW(21, 16, 4), SW(22, 16, 8)] + FINISH()
        supervisor += [0] * ((at - SUPER) // 4 - len(supervisor)) + handler_s
        machine = LI(5, 1 << 3) + [CSRRW(0, MEDELEG, 5)]  # breakpoints to S
        emulator, rtl = self.assert_same(program(pmp() + machine, supervisor))
        self.assertEqual([entry[0] for entry in self.log(rtl.trace)], [9])
        self.assertEqual(stored(rtl.trace, DUMP), 3)
        self.assertEqual(stored(rtl.trace, DUMP + 8) & (MSTATUS_SPP | MSTATUS_SPIE | MSTATUS_SIE), MSTATUS_SPP)
        trapped = [SFENCE_VMA(), WFI(), CSRRS(5, SATP, 0), SRET()]
        supervisor = trapped + FINISH()
        machine = LI(5, MSTATUS_TVM | MSTATUS_TW | MSTATUS_TSR) + [CSRRS(0, MSTATUS, 5)]
        emulator, rtl = self.assert_same(program(pmp() + machine, supervisor))
        status = MSTATUS_SUPERVISOR | MSTATUS_TVM | MSTATUS_TW | MSTATUS_TSR  # from S, MIE was clear
        self.assertEqual(self.log(rtl.trace), [(2, word, SUPER + 4 * i, status) for i, word in enumerate(trapped)])

    def test_supervisor_mode_cannot_reach_machine_state(self):
        """Machine CSRs and mret are illegal in S mode, and an all-ones sstatus write sets only
        SIE, SPIE, SPP, SUM and MXR: not MIE, MPIE, MPP, MPRV, TVM, TW or TSR."""
        denied = [CSRRS(5, MSTATUS, 0), CSRRW(0, MTVEC, 0), CSRRW(0, PMPCFG0, 0), CSRRW(0, MEDELEG, 0),
                  CSRRS(5, MSCRATCH, 0), MRET()]
        supervisor = denied + [ADDI(6, 0, -1), CSRRW(0, SSTATUS, 6), CSRRS(22, SSTATUS, 0)]
        supervisor += LI(16, DUMP) + [SW(22, 16, 0), ECALL()] + FINISH()
        emulator, rtl = self.assert_same(program(pmp(), supervisor))
        ecall_at = SUPER + 4 * (len(denied) + 3 + len(LI(16, DUMP)) + 1)
        written = MSTATUS_SIE | MSTATUS_SPIE | MSTATUS_SPP | MSTATUS_SUM | MSTATUS_MXR
        self.assertEqual(self.log(rtl.trace), [(2, word, SUPER + 4 * i, MSTATUS_SUPERVISOR) for i, word in enumerate(denied)]
                         + [(9, 0, ecall_at, MSTATUS_SUPERVISOR | written)])
        self.assertEqual(stored(rtl.trace, DUMP), 0x80006000 | written, "SD and FS read through sstatus too")

    def test_pmp_checks_the_physical_address(self):
        """A valid leaf to a page PMP refuses is an access fault for S loads, stores and fetches,
        tval the virtual address; and with satp off, MPRV makes PMP check a machine-mode load at
        MPP's privilege."""
        machine = LI(6, MSTATUS_MPP) + [CSRRC(0, MSTATUS, 6)] + LI(6, MSTATUS_MPRV) + [CSRRS(0, MSTATUS, 6)]
        machine += LI(10, PAGE_S) + [LW(11, 10, 0)]  # MPP = U: refused
        machine += LI(6, MSTATUS_MPP) + [CSRRS(0, MSTATUS, 6), LW(11, 10, 0)]  # MPP = M: reads
        machine += LI(6, MSTATUS_MPRV) + [CSRRC(0, MSTATUS, 6)]
        fetch_va = EXECUTE_ONLY_VA + (PAGE_S - RAM)
        supervisor = LI(10, TEST_VA) + [LW(11, 10, 0), SW(11, 10, 0)]
        supervisor += LI(10, PAGE_S) + [LW(11, 10, 0)] + LI(10, fetch_va) + [JALR(1, 10, 0)] + FINISH()
        emulator, rtl = self.assert_same(program(pmp(refused=PAGE_S) + machine, supervisor))
        self.assertEqual([entry[:2] for entry in self.log(rtl.trace)],
                         [(5, PAGE_S), (5, TEST_VA), (7, TEST_VA), (5, PAGE_S), (1, fetch_va)])

    def test_interrupts_bound_for_machine_mode_go_first(self):
        """SSIP stays with machine mode and SEIP is delegated; both are pending and enabled in S
        mode with SIE set. The machine-bound one is taken, although SEI outranks SSI."""
        machine = LI(5, 1 << 9) + [CSRRW(0, MIDELEG, 5)] + LI(5, 1 << 9 | 1 << 1)
        machine += [CSRRW(0, MIE_CSR, 5), CSRRW(0, MIP_CSR, 5), CSRRSI(0, MSTATUS, MSTATUS_SIE)]
        emulator, rtl = self.assert_same(program(pmp() + machine, FINISH()))
        self.assertEqual([entry[:3] for entry in self.log(rtl.trace)], [(INTERRUPT | 1, 0, SUPER)])

    def test_a_delegated_interrupt_waits_for_supervisor_enable(self):
        """A delegated SSI is never taken in machine mode, even with MIE, nor in S mode before SIE;
        once SIE is set it goes to stvec before the next instruction, and sip clears it."""
        handler_s = [CSRRS(20, SCAUSE, 0), CSRRS(21, SEPC, 0), CSRRCI(0, SIP_CSR, 2), SRET()]
        at = SUPER + 0x200
        supervisor = LI(5, at) + [CSRRW(0, STVEC, 5), ADDI(0, 0, 0), CSRRSI(0, SSTATUS, MSTATUS_SIE)]
        taken_at = SUPER + 4 * len(supervisor)
        supervisor += LI(16, DUMP) + [SW(20, 16, 0), SW(21, 16, 4)] + FINISH()
        supervisor += [0] * ((at - SUPER) // 4 - len(supervisor)) + handler_s
        machine = LI(5, 1 << 1) + [CSRRW(0, MIDELEG, 5), CSRRW(0, MIE_CSR, 5), CSRRW(0, MIP_CSR, 5)]
        machine += [CSRRSI(0, MSTATUS, 8), ADDI(0, 0, 0), CSRRCI(0, MSTATUS, 8)]
        emulator, rtl = self.assert_same(program(pmp() + machine, supervisor))
        self.assertEqual(self.log(rtl.trace), [])
        self.assertEqual([stored(rtl.trace, DUMP), stored(rtl.trace, DUMP + 4)], [INTERRUPT | 1, taken_at])

    def test_a_delegated_interrupt_is_taken_in_user_mode_whatever_sie_says(self):
        """Below S mode a delegated interrupt is always enabled: with SIE clear, sret into U mode
        takes the pending SSI at the first user instruction, into S mode."""
        handler_s = [CSRRS(20, SCAUSE, 0), CSRRS(21, SEPC, 0)] + LI(16, DUMP)
        handler_s += [SW(20, 16, 0), SW(21, 16, 4), CSRRCI(0, SIP_CSR, 2), SRET()]
        at = SUPER + 0x200
        user_at = ALIAS + (USER_CODE - RAM)
        supervisor = LI(5, at) + [CSRRW(0, STVEC, 5)] + LI(5, user_at) + [CSRRW(0, SEPC, 5)]
        supervisor += LI(6, MSTATUS_SPP) + [CSRRC(0, SSTATUS, 6), SRET()]
        supervisor += [0] * ((at - SUPER) // 4 - len(supervisor)) + handler_s
        machine = LI(5, 1 << 1) + [CSRRW(0, MIDELEG, 5), CSRRW(0, MIE_CSR, 5), CSRRW(0, MIP_CSR, 5)]
        emulator, rtl = self.assert_same(program(pmp() + machine, supervisor, [ECALL()]))
        self.assertEqual([stored(rtl.trace, DUMP), stored(rtl.trace, DUMP + 4)], [INTERRUPT | 1, user_at])
        self.assertEqual([entry[:3] for entry in self.log(rtl.trace)], [(8, 0, user_at)])


if __name__ == "__main__":
    unittest.main()
