"""Issue #20: S-mode and Sv32 on both backends, the half mmucheck cannot cover (docs/rv32.md "Sv32").
Issue #24 adds the RTL's TLB: its hit and miss counts against a model, a flush on a satp write, a
replacement once more than four pages are live, a store hit on a clean page, and a stale entry.

mmucheck runs on QEMU as the reference. These directed programs cover what QEMU cannot referee
or does differently: a page-table read that PMP refuses, that lands in a device window or
outside RAM, PMP on the translated address, the order of a misaligned address against a page
fault, and the cycle formula with page-table walks. They also cover what mmucheck leaves out:
machine state from S mode, the sstatus mask, MPRV without translation, TW, and which interrupt
goes first. Each runs on the emulator and the RTL in step-tick mode and the traces
must be identical; the expected causes and addresses are written here by hand. The one exception
is the stale-entry test: a program that changes a page-table entry without sfence.vma may see
either translation, and the emulator, which has no TLB, sees the new one.

The page tables are data in the image. Machine mode sets satp and PMP and enters S mode with
mret; the S code runs at its physical address through an identity megapage, and U code through
a megapage alias with U set. One machine-mode handler logs (mcause, mtval, mepc, mstatus) for
the nth trap at LOG + 16 (n + 1), after the count at LOG. It returns past the instruction, to
x1 after a fetch fault, and to the interrupted instruction after an interrupt, with mip's
software bits cleared. It finishes the run on an ecall from U mode.
"""
import re
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
    level0[3] = pte(PAGE_U, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D)  # issue #24: supervisor pages to retarget
    level0[4] = pte(GUARDED, PTE_V | PTE_R | PTE_A)
    for n in range(5, 9):
        level0[n] = pte(PAGE_S, PTE_V | PTE_R | PTE_W | PTE_A | PTE_D)
    level0[9] = pte(PAGE_S, PTE_V | PTE_R | PTE_W | PTE_A)  # D clear
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


def tlb_model(trace, translated):
    """What the RTL's TLB (issue #24) makes of a trap-free run: (hits, misses, page-table reads).
    Each trace line that `translated` accepts looks up its fetch's pc, then its data address if it
    has one. A miss reads one entry for a megapage and two for a 4 KiB page under TEST_VA's root
    entry (the only root entry these tests point at a level-0 table), and fills the next of four entries in turn. Nothing may flush the TLB in between."""
    entries, fill, hits, misses, reads = [None] * 4, 0, 0, 0, 0
    lines = list(filter(translated, trace))
    assert lines, "no translated lines to model"
    for line in lines:
        word = int(line.split()[2], 16)
        flushes = (word & 0xFE007FFF) == 0x12000073 or (word & 0x7F == 0x73 and word >> 20 == SATP and word >> 12 & 3)
        assert " trap " not in line and " interrupt " not in line and not flushes, f"the model cannot follow {line!r}"
        data = re.search(r"mem\[([0-9a-f]{8})\]", line)
        for address in [int(line.split()[1], 16)] + ([int(data.group(1), 16)] if data else []):
            small = address >> 22 == TEST_VA >> 22
            key = (address >> 22, (address >> 12) & 0x3FF if small else None)
            if key in entries:
                hits += 1
            else:
                misses += 1
                reads += 2 if small else 1
                entries[fill] = key
                fill = (fill + 1) % 4
    return hits, misses, reads


def trap_log(trace):
    """What handler() logged: (mcause, mtval, mepc, mstatus) per trap, in order."""
    count = stored(trace, LOG) or 0
    return [tuple(stored(trace, LOG + 16 * (n + 1) + 4 * k) for k in range(4)) for n in range(count)]


def supervisor_line(line):
    return int(line.split()[1], 16) >= SUPER


class MmuTest(StepTicksCase):

    def log(self, trace):
        return trap_log(trace)

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
        # Counted from the trace, not the core: every S-mode fetch and data access looks up the TLB,
        # and a miss walks, one read through a megapage and two through the level-0 table at
        # TEST_VA. A miss adds one cycle per read, plus the FETCH cycle that starts a fetch's walk or
        # the XLATE after a data access's; a hit adds none.
        hits, misses, reads = tlb_model(emulator.trace, supervisor_line)
        self.assertEqual((misses, reads), (3, 4))  # the image's megapage, TEST_VA's page, root[0] for done
        for timing in ({"ticks": None, "stall": 1}, {"ticks": None, "seed": 11}, {"ticks": "steps", "stall": 0}):
            with self.subTest(**timing):
                emulator, rtl = self.run_both(words, **timing)
                self.assertIsNone(diff_traces(rtl.trace, emulator.trace))
                relation, holds = cycle_relation(rtl)
                self.assertTrue(holds, relation)
                self.assertEqual([rtl.halt[key] for key in ("walks", "ptw_waits", "tlb_hits", "tlb_misses")],
                                 [reads, reads + misses, hits, misses], relation)

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

    # Issue #24: the RTL's TLB. TEST_VA's level-0 entries 3 to 9 are supervisor pages for these.
    SUPERVISOR_PAGE = PTE_V | PTE_R | PTE_W | PTE_A | PTE_D

    def retarget(self, n, physical, flags=SUPERVISOR_PAGE):
        """S code that rewrites level0[n] through the image's megapage, with no sfence.vma. Uses x20, x21."""
        return LI(20, LEVEL0 + 4 * n) + LI(21, pte(physical, flags)) + [SW(21, 20, 0)]

    def test_a_stale_entry_is_used_until_sfence_vma_and_pmp_still_checks_it(self):
        """Without sfence.vma the RTL may keep using a page-table entry the program has changed; the
        privileged spec allows that, and the emulator, which has no TLB, sees the change. A satp
        read does not flush. PMP still checks the stale physical address: GUARDED, which PMP
        refuses, stays refused after its entry is pointed at a page PMP grants. sfence.vma ends it."""
        va3, va4 = TEST_VA + 0x3000, TEST_VA + 0x4000
        supervisor = LI(10, va3) + [LW(11, 10, 0)] + self.retarget(3, PAGE_S)  # reads PAGE_U, then retargets
        stale_at = SUPER + 4 * (len(supervisor) + 1)
        supervisor += [CSRRS(5, SATP, 0), LW(12, 10, 0)]
        supervisor += LI(22, va4) + [LW(13, 22, 0)] + self.retarget(4, PAGE_U, PTE_V | PTE_R | PTE_A)
        supervisor += [LW(13, 22, 0), SFENCE_VMA(), LW(14, 10, 0), LW(15, 22, 0)]
        supervisor += LI(16, DUMP) + [SW(register, 16, 4 * (register - 11)) for register in range(11, 16)] + FINISH()
        emulator, rtl = self.run_both(program(pmp(refused=GUARDED), supervisor))
        self.assertEqual((emulator.halt["outcome"], rtl.halt["outcome"]), ("pass", "pass"))
        dumped = {name: [stored(run.trace, DUMP + 4 * i) for i in range(5)] for name, run in (("emulator", emulator), ("rtl", rtl))}
        self.assertEqual(dumped, {"rtl": [0x7000, 0x7000, 0, 0x5000, 0x7000],  # stale, refused, then fresh
                                  "emulator": [0x7000, 0x5000, 0x7000, 0x5000, 0x7000]})
        self.assertEqual([entry[:2] for entry in self.log(rtl.trace)], [(5, va4), (5, va4)])
        self.assertEqual([entry[:2] for entry in self.log(emulator.trace)], [(5, va4)])
        # The traces agree up to the stale load; on the RTL the second va4 load and its retry are hits.
        first = next(i for i, pair in enumerate(zip(rtl.trace, emulator.trace)) if pair[0] != pair[1])
        self.assertEqual(int(rtl.trace[first].split()[1], 16), stale_at)
        # Misses: the image's megapage, va3 and va4, each before and after sfence.vma, and root[0] for done.
        self.assertEqual((rtl.halt["tlb_misses"], rtl.halt["walks"]), (7, 11))

    def test_a_satp_write_flushes_the_tlb(self):
        """Writing satp, even with the value it holds, empties the TLB: the load after it sees the
        retargeted entry on both backends."""
        supervisor = LI(10, TEST_VA + 0x3000) + [LW(11, 10, 0)] + self.retarget(3, PAGE_S)
        supervisor += [CSRRS(5, SATP, 0), CSRRW(0, SATP, 5), LW(12, 10, 0)]
        supervisor += LI(16, DUMP) + [SW(11, 16, 0), SW(12, 16, 4)] + FINISH()
        emulator, rtl = self.assert_same(program(pmp(), supervisor))
        self.assertEqual([stored(rtl.trace, DUMP), stored(rtl.trace, DUMP + 4)], [0x7000, 0x5000])

    def test_the_oldest_entry_goes_once_more_than_four_pages_are_live(self):
        """Six pages live at once (the image's megapage, five 4 KiB pages) and root[0] at the end:
        entries are replaced oldest first, which the model predicts, and the page retargeted
        without sfence.vma has been replaced by the time it is read again, so both backends see the
        new translation. The cycle formula holds with stalls."""
        supervisor = LI(10, TEST_VA + 0x3000) + [LW(11, 10, 0)] + self.retarget(3, PAGE_S)
        for n in range(5, 9):
            supervisor += LI(22, TEST_VA + 0x1000 * n) + [LW(23, 22, 0)]
        supervisor += [LW(12, 10, 0)] + LI(16, DUMP) + [SW(11, 16, 0), SW(12, 16, 4)] + FINISH()
        words = program(pmp(), supervisor)
        emulator, rtl = self.assert_same(words)
        self.assertEqual([stored(rtl.trace, DUMP), stored(rtl.trace, DUMP + 4)], [0x7000, 0x5000])
        hits, misses, reads = tlb_model(emulator.trace, supervisor_line)
        self.assertGreater(misses, 7, "the image's megapage is replaced and comes back")
        emulator, rtl = self.run_both(words, ticks=None, stall=1)
        relation, holds = cycle_relation(rtl)
        self.assertTrue(holds, relation)
        self.assertEqual([rtl.halt[key] for key in ("walks", "tlb_hits", "tlb_misses")], [reads, hits, misses], relation)

    def test_a_store_that_hits_a_clean_page_faults(self):
        """A load fills the TLB from a leaf with D clear; the store after it hits that entry and is a
        store page fault (Svade), as a walk would make it."""
        va9 = TEST_VA + 0x9000
        supervisor = LI(10, va9) + [LW(11, 10, 0), SW(11, 10, 0)] + FINISH()
        emulator, rtl = self.assert_same(program(pmp(), supervisor))
        self.assertEqual([entry[:2] for entry in self.log(rtl.trace)], [(15, va9)])
        # The image's megapage, va9's page and root[0] miss; every other lookup hits: each S-mode fetch,
        # the store's data address (the trap line has no mem[] field) and nothing else.
        lines = [line for line in rtl.trace if supervisor_line(line)]
        lookups = len(lines) + sum("mem[" in line for line in lines) + 1
        self.assertEqual([rtl.halt[key] for key in ("walks", "tlb_misses", "tlb_hits", "ptw_waits")], [4, 3, lookups - 3, 3 + 4])

    def test_a_fetch_that_hits_an_entry_it_may_not_use_faults(self):
        """Fetches that hit entries filled earlier: a page a load filled without X is a fetch page
        fault; the image's megapage, filled by S-mode fetches, is a fetch access fault where PMP
        refuses the physical page and a fetch page fault from U mode (U clear). None of them walks."""
        va3 = TEST_VA + 0x3000
        user_at = ALIAS + (USER_CODE - RAM)
        supervisor = LI(10, va3) + [LW(11, 10, 0), JALR(1, 10, 0)]
        supervisor += LI(10, PAGE_S) + [JALR(1, 10, 0)]
        supervisor += LI(5, SUPER) + [CSRRW(0, SEPC, 5)] + LI(1, user_at) + LI(6, MSTATUS_SPP) + [CSRRC(0, SSTATUS, 6), SRET()]
        emulator, rtl = self.assert_same(program(pmp(refused=PAGE_S), supervisor, [ECALL()]))
        self.assertEqual([entry[:2] for entry in self.log(rtl.trace)], [(12, va3), (1, PAGE_S), (12, SUPER), (8, 0)])
        # Misses: the image's megapage, va3 (two reads) and the user alias; the three faults are hits.
        self.assertEqual((rtl.halt["tlb_misses"], rtl.halt["walks"]), (3, 4))

    def test_sum_mxr_and_the_permissions_are_checked_on_a_hit(self):
        """Entries filled while SUM or MXR allowed the access fault once it is cleared, and a store
        hitting an X-only entry faults: the hit checks the cached leaf against the access as it is."""
        user_va, execute_only = TEST_VA + 0x1000, EXECUTE_ONLY_VA + (PAGE_S - RAM)
        supervisor = LI(6, MSTATUS_SUM) + [CSRRS(0, SSTATUS, 6)] + LI(10, user_va) + [SW(0, 10, 0)]
        supervisor += [CSRRC(0, SSTATUS, 6), LW(11, 10, 0)]                                  # 13
        supervisor += LI(6, MSTATUS_MXR) + [CSRRS(0, SSTATUS, 6)] + LI(12, execute_only) + [LW(13, 12, 0)]
        supervisor += [CSRRC(0, SSTATUS, 6), LW(13, 12, 0), SW(0, 12, 0)] + FINISH()          # 13, 15
        emulator, rtl = self.assert_same(program(pmp(), supervisor))
        self.assertEqual([entry[:2] for entry in self.log(rtl.trace)], [(13, user_va), (13, execute_only), (15, execute_only)])
        # Misses: the image's megapage, the user page, the X-only megapage and root[0]; the faults are hits.
        self.assertEqual((rtl.halt["tlb_misses"], rtl.halt["walks"]), (4, 5))

    def test_user_mode_data_hits_a_supervisor_entry(self):
        """S mode fills TEST_VA's entry (U clear); the same load from U mode hits it and faults."""
        user = LI(12, TEST_VA) + [LW(13, 12, 0), ECALL()]
        supervisor = LI(10, TEST_VA) + [LW(11, 10, 0)]
        supervisor += LI(5, ALIAS + (USER_CODE - RAM)) + [CSRRW(0, SEPC, 5)] + LI(6, MSTATUS_SPP)
        supervisor += [CSRRC(0, SSTATUS, 6), SRET()]
        emulator, rtl = self.assert_same(program(pmp(), supervisor, user))
        self.assertEqual([entry[:2] for entry in self.log(rtl.trace)], [(13, TEST_VA), (8, 0)])
        # Misses: the image's megapage, TEST_VA's page and the user alias; the U-mode load is a hit.
        self.assertEqual((rtl.halt["tlb_misses"], rtl.halt["walks"]), (3, 4))

    def test_a_store_that_hits_a_read_only_dirty_entry_faults(self):
        """W, not D, refuses this store hit: the leaf has R, A and D but no W."""
        va3 = TEST_VA + 0x3000
        supervisor = self.retarget(3, PAGE_U, PTE_V | PTE_R | PTE_A | PTE_D)  # before any fill: nothing stale
        supervisor += LI(10, va3) + [LW(11, 10, 0), SW(11, 10, 0)] + FINISH()
        emulator, rtl = self.assert_same(program(pmp(), supervisor))
        self.assertEqual([entry[:2] for entry in self.log(rtl.trace)], [(15, va3)])
        self.assertEqual(rtl.halt["tlb_misses"], 3)  # the image's megapage, va3, root[0]: the store hits

    def mprv(self, machine, after):
        """Machine code with satp on and MPRV set with MPP = U, then `machine`, then MPRV clear and `after`."""
        words = LI(5, SATP_SV32 | ROOT >> 12) + [CSRRW(0, SATP, 5)]
        words += LI(6, MSTATUS_MPP) + [CSRRC(0, MSTATUS, 6)] + LI(6, MSTATUS_MPRV) + [CSRRS(0, MSTATUS, 6)]
        return pmp() + words + list(machine) + LI(6, MSTATUS_MPRV) + [CSRRC(0, MSTATUS, 6)] + list(after)

    def test_an_mprv_load_hits_at_the_privilege_mpp_names(self):
        """Machine mode with MPRV and MPP = U loads a U page twice: a walk, then a hit, both checked at
        U's privilege (a hit checked at machine mode's would ignore U; one at S's would fault)."""
        machine = LI(10, TEST_VA + 0x1000) + [LW(11, 10, 0), LW(12, 10, 4)]
        dump = LI(16, DUMP) + [SW(11, 16, 0), SW(12, 16, 4)]
        emulator, rtl = self.assert_same(program(self.mprv(machine, dump), FINISH()))
        self.assertEqual(self.log(rtl.trace), [])
        self.assertEqual([stored(rtl.trace, DUMP), stored(rtl.trace, DUMP + 4)], [0x7000, 0x7001])

    def test_sfence_vma_in_machine_mode_flushes(self):
        """Machine mode, translating through MPRV with MPP = S, fills va3, retargets it and runs
        sfence.vma itself: the next load sees the new page on both backends."""
        va3 = TEST_VA + 0x3000
        machine = LI(6, 0x800) + [CSRRS(0, MSTATUS, 6)]  # MPP = S
        machine += LI(10, va3) + [LW(13, 10, 0)] + self.retarget(3, PAGE_S) + [SFENCE_VMA(), LW(14, 10, 0)]
        dump = LI(16, DUMP) + [SW(13, 16, 0), SW(14, 16, 4)]
        emulator, rtl = self.assert_same(program(self.mprv(machine, dump), FINISH()))
        self.assertEqual([stored(rtl.trace, DUMP), stored(rtl.trace, DUMP + 4)], [0x7000, 0x5000])

    def test_the_lowest_numbered_entry_wins_when_two_match(self):
        """A 4 KiB entry for va3, then TEST_VA's root entry rewritten as a megapage without
        sfence.vma, then a fill through it: two entries match va3 and the older, lower-numbered 4 KiB
        one translates it. The emulator, with no TLB, follows the megapage, so the traces differ."""
        va3 = TEST_VA + 0x3000
        supervisor = LI(10, va3) + [LW(11, 10, 0)]
        supervisor += LI(20, ROOT + 4 * (TEST_VA >> 22)) + LI(21, pte(RAM, RWXAD)) + [SW(21, 20, 0)]
        supervisor += LI(22, TEST_VA + 0x5000) + [LW(23, 22, 0), LW(12, 10, 0)]
        supervisor += LI(16, DUMP) + [SW(12, 16, 0)] + FINISH()
        emulator, rtl = self.run_both(program(pmp(), supervisor))
        self.assertEqual((emulator.halt["outcome"], rtl.halt["outcome"]), ("pass", "pass"))
        self.assertEqual(stored(rtl.trace, DUMP), 0x7000)
        self.assertNotEqual(stored(emulator.trace, DUMP), 0x7000)


if __name__ == "__main__":
    unittest.main()
