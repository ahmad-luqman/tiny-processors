"""Track 2, O5: user mode and PMP on both backends (docs/rv32-os.md).

Directed programs enter user mode with mret, trap back with ecall, try the privileged
instructions and CSRs user mode may not use, and probe every kind of PMP entry (OFF, TOR, NA4,
NAPOT), the permission bits, the priority of lower-numbered entries, locked entries holding
machine mode, and the WARL fields. Each runs on the emulator and the RTL in step-tick mode and
the traces must be identical; the expected causes, addresses and CSR values are written here by
hand from the privileged specification (docs/rv32.md, "Behavior fixed in Track 2").

Every program shares one handler, which logs (mcause, mtval, mepc, mstatus) for each trap at
LOG + 16 n, returns past the instruction for an exception, to x1 for a fetch fault (the jump's
link register), and to mepc for an interrupt, and finishes the run on an ecall.
"""
import unittest

import test_rv32_irq as irq
import test_rv32_rtl as integer_tests
from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_rtl import trap_records

HANDLER = RAM + 0x400  # machine code below, the handler here, user code at USER
USER = RAM + 0x800     # 2 KiB of user code
LOG = RAM + 0x2000     # the trap count, then 16 bytes per trap from LOG + 16
DUMP = RAM + 0x2800    # machine-mode values stored for the checks
DATA = RAM + 0x3000
WFI = 0x10500073
FCSR = 0x003
MSTATUS_M = 0x80007800  # SD, FS = 3, MPP = 3
MSTATUS_U = 0x80006000  # SD, FS = 3, MPP = 0

OFF, TOR, NA4, NAPOT = 0x00, 0x08, 0x10, 0x18
R, W, X, L = 0x01, 0x02, 0x04, 0x80


def napot(base, size):
    """pmpaddr for a naturally aligned power-of-two region of at least 8 bytes."""
    return (base >> 2) | ((size >> 3) - 1)


def handler():
    """The shared handler; it uses only x26 to x31."""
    head = LI(28, LOG) + [
        LW(29, 28, 0), SLLI(30, 29, 4), ADD(30, 30, 28),
        CSRRS(31, MCAUSE, 0), SW(31, 30, 16),
        CSRRS(27, MTVAL, 0), SW(27, 30, 20),
        CSRRS(27, MEPC, 0), SW(27, 30, 24),
        CSRRS(27, MSTATUS, 0), SW(27, 30, 28),
        ADDI(29, 29, 1), SW(29, 28, 0),
    ]
    # An exception: an ecall (8 or 11) jumps to the finishing code; a fetch fault
    # returns to x1; anything else returns past the instruction.
    exception = [
        ADDI(26, 0, 8), BEQ(31, 26, 4 * 17), ADDI(26, 0, 11), BEQ(31, 26, 4 * 15),
        ADDI(26, 0, 1), BNE(31, 26, 12), CSRRW(0, MEPC, 1), MRET(),
        CSRRS(27, MEPC, 0), ADDI(27, 27, 4), CSRRW(0, MEPC, 27), MRET(),
    ]
    # An interrupt (mcause negative) disarms the timer and resumes at mepc.
    interrupt = irq.DISARM_TIMER + [MRET()]
    assert len(interrupt) == 6, "the ecall branches assume six words of interrupt path"
    return head + [BLT(31, 0, 4 * (1 + len(exception)))] + exception + interrupt + FINISH()


def program(machine, user=()):
    """`machine` from RAM (after mtvec is set), the handler at HANDLER, `user` at USER."""
    words = LI(5, HANDLER) + [CSRRW(0, MTVEC, 5)] + list(machine)
    assert len(words) <= (HANDLER - RAM) // 4, "the machine code overlaps the handler"
    words += [0] * ((HANDLER - RAM) // 4 - len(words)) + handler()
    assert len(words) <= (USER - RAM) // 4, "the handler overlaps the user code"
    words += [0] * ((USER - RAM) // 4 - len(words)) + list(user)
    assert len(words) <= (LOG - RAM) // 4, "the user code overlaps the log"
    return words


def enter_user(at=USER, mpie=True):
    """mepc = at, MPP = user, MPIE as asked, then mret (x5 and x6)."""
    return LI(5, at) + [CSRRW(0, MEPC, 5)] + LI(6, 0x1800) + [CSRRC(0, MSTATUS, 6)] \
        + LI(6, 0x80) + [CSRRS(0, MSTATUS, 6) if mpie else CSRRC(0, MSTATUS, 6), MRET()]


def set_pmp(entries):
    """entries: {n: (cfg, pmpaddr)}; pmpaddr first, then both configuration registers (x5, x6)."""
    words = []
    for n, (_, address) in sorted(entries.items()):
        words += LI(5, address) + [CSRRW(0, PMPADDR0 + n, 5)]
    for register in range(2):
        value = 0
        for n, (cfg, _) in entries.items():
            if n // 4 == register:
                value |= cfg << (8 * (n % 4))
        words += LI(6, value) + [CSRRW(0, PMPCFG0 + register, 6)]
    return words


def store_at(address, *regs):
    """Machine mode: store registers at `address` onwards (x7)."""
    return LI(7, address) + [SW(reg, 7, 4 * i) for i, reg in enumerate(regs)]


ALL = {0: (NAPOT | R | W | X, 0xFFFFFFFF)}  # everything, for tests about user mode itself


class PmpTest(unittest.TestCase):
    setUpClass = classmethod(integer_tests.RtlTest.setUpClass.__func__)
    tearDownClass = classmethod(integer_tests.RtlTest.tearDownClass.__func__)

    run_both = irq.InterruptTest.run_both
    assert_same = irq.InterruptTest.assert_same

    def log(self, trace):
        """The handler's records, as (mcause, mtval, mepc, mstatus) tuples."""
        count = irq.stored(trace, LOG) or 0
        return [tuple(irq.stored(trace, LOG + 16 * (n + 1) + 4 * k) for k in range(4)) for n in range(count)]

    def test_mret_enters_user_mode_and_ecall_returns_with_cause_8(self):
        user = [ADDI(10, 0, 7), ECALL()]
        emulator, rtl = self.assert_same(program(set_pmp(ALL) + enter_user(), user))
        # MIE was clear when mret entered user mode with MPIE set, so user mode ran with MIE set;
        # the ecall moved it to MPIE and put user (0) in MPP.
        self.assertEqual(self.log(rtl.trace), [(8, 0, USER + 4, MSTATUS_U | 0x80)])
        self.assertEqual(trap_records(rtl.trace), trap_records(emulator.trace))

    def test_mpp_is_warl_machine_or_user(self):
        machine = []
        for mpp in range(4):
            machine += LI(5, 0x1800) + [CSRRC(0, MSTATUS, 5)] + LI(5, mpp << 11) + [CSRRS(0, MSTATUS, 5),
                                                                                   CSRRS(10 + mpp, MSTATUS, 0)]
        emulator, rtl = self.assert_same(program(machine + store_at(DUMP, 10, 11, 12, 13) + [ECALL()]))
        got = [irq.stored(rtl.trace, DUMP + 4 * i) for i in range(4)]
        self.assertEqual(got, [MSTATUS_U, MSTATUS_U, MSTATUS_U, MSTATUS_M])

    def test_privileged_instructions_and_csrs_are_illegal_in_user_mode(self):
        refused = [CSRRS(5, MSTATUS, 0), CSRRS(5, 0x340, 0), MRET(), WFI,
                   CSRRS(5, MCOUNTEREN, 0), CSRRS(5, PMPADDR0, 0), CSRRS(5, PMPCFG0, 0),
                   RDTIME(5)]                                 # TM is clear in mcounteren
        allowed = [RDCYCLE(6), RDINSTRET(7), RDINSTRETH(8), CSRRS(9, FCSR, 0)]
        user = refused + allowed + [ECALL()]
        machine = set_pmp(ALL) + [CSRRWI(0, MCOUNTEREN, 0b101)] + enter_user()
        emulator, rtl = self.assert_same(program(machine, user))
        expected = [(2, word, USER + 4 * i, MSTATUS_U | 0x80) for i, word in enumerate(refused)]
        expected.append((8, 0, USER + 4 * len(user) - 4, MSTATUS_U | 0x80))
        self.assertEqual(self.log(rtl.trace), expected)

    def test_pmp_grants_and_refuses_user_accesses(self):
        entries = {
            0: (NA4 | R, DATA >> 2),                          # one read-only word: beats entry 2
            1: (NAPOT | R | X, napot(USER, 0x800)),           # the user code
            2: (NAPOT | R | W, napot(DATA, 0x1000)),          # data
            4: (OFF, (RAM + 0x5000) >> 2),                    # TOR bottom for entry 5
            5: (TOR | R | W, (RAM + 0x5100) >> 2),            # [0x5000, 0x5100)
            6: (NAPOT | W, napot(RAM + 0x6000, 0x100)),       # W without R: stored as no access
        }
        checks = [
            (LW(10, 20, 0), None),                            # DATA: readable
            (SW(21, 20, 0), (7, DATA)),                       # but not writable (entry 0 first)
            (SW(21, 20, 4), None),                            # DATA + 4: entry 2, writable
            (LW(11, 20, 4), None),
            (SW(21, 22, 0), None),                            # TOR bottom
            (SW(21, 22, 0xFC), None),                         # TOR last word
            (SW(21, 22, 0x100), (7, RAM + 0x5100)),           # TOR top is exclusive
            (LW(12, 22, -4), (5, RAM + 0x4FFC)),              # below the TOR region: nothing matches
            (LW(12, 23, 0), (5, RAM + 0x6000)),               # entry 6 grants nothing
            (LW(12, 24, 0), None),                            # user code is readable
            (SW(21, 24, 0), (7, USER)),                       # not writable
            (LW(12, 25, 0), (5, HANDLER)),                    # machine code: no entry
            (LW(12, 19, 0), (5, UNMAPPED)),                   # outside RAM: no entry, refused before the bus
        ]
        user = LI(20, DATA) + LI(21, 0x5A5A5A5A) + LI(22, RAM + 0x5000) + LI(23, RAM + 0x6000) \
            + LI(24, USER) + LI(25, HANDLER) + LI(19, UNMAPPED) + LI(18, DATA + 0x10)
        first = USER + 4 * len(user)
        user += [word for word, _ in checks]
        jump_at = USER + 4 * len(user)
        user += [JALR(1, 18, 0), ECALL()]                     # data is not executable
        machine = set_pmp(entries) + [CSRRS(10, PMPCFG1, 0), CSRRS(11, PMPADDR0 + 5, 0)]
        machine += LI(5, RAM + 0x6000) + [SW(5, 5, 0)]         # an unlocked entry does not hold machine mode
        machine += store_at(DUMP, 10, 11) + enter_user()
        emulator, rtl = self.assert_same(program(machine, user))
        expected = [(cause, tval, first + 4 * i, MSTATUS_U | 0x80)
                    for i, (_, fault) in enumerate(checks) if fault for cause, tval in [fault]]
        expected.append((1, DATA + 0x10, DATA + 0x10, MSTATUS_U | 0x80))
        expected.append((8, 0, jump_at + 4, MSTATUS_U | 0x80))
        self.assertEqual(self.log(rtl.trace), expected)
        self.assertEqual(irq.stored(rtl.trace, DUMP), (NAPOT << 16) | (TOR | R | W) << 8)  # entry 6's W dropped
        self.assertEqual(irq.stored(rtl.trace, DUMP + 4), (RAM + 0x5100) >> 2)
        self.assertEqual(irq.stored(rtl.trace, DATA + 4), 0x5A5A5A5A)
        self.assertEqual(irq.stored(rtl.trace, RAM + 0x6000), RAM + 0x6000)

    def test_locked_entries_hold_machine_mode_and_ignore_writes(self):
        entries = {
            0: (NA4 | L | R, DATA >> 2),                      # read-only, even for machine mode
            1: (OFF, (RAM + 0x4000) >> 2),                    # the bottom of entry 2, locked with it
            2: (TOR | L, (RAM + 0x5000) >> 2),                # no access at all
            3: (NAPOT, napot(RAM + 0x6000, 0x100)),           # unlocked, no permissions: machine ignores it
        }
        machine = set_pmp(entries) + LI(20, DATA) + LI(21, RAM + 0x4000) + LI(22, RAM + 0x6000)
        machine += [LW(10, 20, 0), SW(20, 20, 0)]            # read passes, write faults
        machine += [SW(20, 21, 0), SW(20, 22, 0)]            # entry 2 faults, entry 3 passes
        machine += LI(5, 0x1F1F001F) + [CSRRW(0, PMPCFG0, 5)]  # locked bytes keep their value; entry 3 changes
        machine += [CSRRW(0, PMPADDR0, 0), CSRRW(0, PMPADDR0 + 1, 0), CSRRW(0, PMPADDR0 + 2, 0)]
        machine += [CSRRS(11, PMPCFG0, 0), CSRRS(12, PMPADDR0, 0), CSRRS(13, PMPADDR0 + 1, 0), CSRRS(14, PMPADDR0 + 2, 0)]
        machine += store_at(DUMP, 11, 12, 13, 14)
        jump = len(machine) + 2
        machine += LI(18, RAM + 0x4000) + [JALR(1, 18, 0), ECALL()]  # locked without X: no fetch
        emulator, rtl = self.assert_same(program(machine))
        at = lambda word: RAM + 12 + 4 * machine.index(word)  # noqa: E731
        self.assertEqual(self.log(rtl.trace), [
            (7, DATA, at(SW(20, 20, 0)), MSTATUS_M),
            (7, RAM + 0x4000, at(SW(20, 21, 0)), MSTATUS_M),
            (1, RAM + 0x4000, RAM + 0x4000, MSTATUS_M),
            (11, 0, RAM + 12 + 4 * (jump + 1), MSTATUS_M),
        ])
        self.assertEqual(irq.stored(rtl.trace, DUMP), 0x1F << 24 | (TOR | L) << 16 | OFF << 8 | (NA4 | L | R))
        self.assertEqual(irq.stored(rtl.trace, DUMP + 4), DATA >> 2)
        self.assertEqual(irq.stored(rtl.trace, DUMP + 8), (RAM + 0x4000) >> 2)
        self.assertEqual(irq.stored(rtl.trace, DUMP + 12), (RAM + 0x5000) >> 2)

    def test_interrupts_are_enabled_in_user_mode(self):
        # User mode runs with MIE clear (MPIE was clear at mret), yet the timer interrupt is taken.
        user = [ADDI(10, 0, 0), ADDI(10, 10, 1), ADDI(11, 0, 200), BLT(10, 11, -8), ECALL()]
        machine = set_pmp(ALL) + irq.set_timer(0) + LI(5, 1 << 7) + [CSRRW(0, irq.MIE_CSR, 5)] + enter_user(mpie=False)
        emulator, rtl = self.assert_same(program(machine, user))
        log = self.log(rtl.trace)
        self.assertEqual(len(log), 2, log)
        self.assertEqual(log[0][:2], (0x80000007, 0))
        self.assertEqual(log[0][3], MSTATUS_U)                # from user mode, with MIE (so MPIE) clear
        self.assertEqual(log[0][2], USER)                     # before the first user instruction
        self.assertEqual(log[1][0], 8)
        self.assertEqual((emulator.halt["interrupts"], rtl.halt["interrupts"]), (1, 1))

    def test_pmp_with_memory_stalls(self):
        user = [LW(10, 0, 0), ECALL()]                         # address 0: no entry, a load fault
        words = program(set_pmp({0: (NAPOT | R | X, napot(USER, 0x800))}) + enter_user(), user)
        for seed in (1, 9):
            with self.subTest(seed=seed):
                emulator, rtl = self.assert_same(words, seed=seed)
                self.assertEqual([entry[:3] for entry in self.log(rtl.trace)], [(5, 0, USER), (8, 0, USER + 4)])


if __name__ == "__main__":
    unittest.main()
