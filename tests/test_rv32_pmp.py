"""Track 2, O5: user mode and PMP on both backends (docs/rv32-os.md).

Directed programs enter user mode with mret, trap back with ecall, try the privileged
instructions and CSRs user mode may not use, and probe every kind of PMP entry (OFF, TOR, NA4,
NAPOT), the permission bits, the priority of lower-numbered entries, locked entries holding
machine mode, and the WARL fields. Each runs on the emulator and the RTL in step-tick mode and
the traces must be identical; the expected causes, addresses and CSR values are written here by
hand from the privileged specification (docs/rv32.md, "Behavior fixed in Track 2").

Every program shares one handler, which logs (mcause, mtval, mepc, mstatus) for each trap at
LOG + 16 n, returns past the instruction for an exception, to x1 for a fetch fault (the jump's
link register), and to mepc for an interrupt, and finishes the run on an ecall (or, when the
program asks, continues its machine-mode code after a user-mode ecall).
"""
import unittest

from rv32_step_case import DISARM_TIMER, StepTicksCase, set_timer, stored
from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_rtl import trap_records

HANDLER = RAM + 0x400  # machine code below, the handler here, user code at USER
USER = RAM + 0x800     # 2 KiB of user code
LOG = RAM + 0x2000     # the trap count, then 16 bytes per trap from LOG + 16
DUMP = RAM + 0x2800    # machine-mode values stored for the checks
DATA = RAM + 0x3000
RESUME = RAM + 0x300   # where a program's machine code goes on after its user code, if it does
MSTATUS_M = MSTATUS_RESET  # SD, FS = 3, MPP = 3
MSTATUS_U = MSTATUS_USER   # SD, FS = 3, MPP = 0

OFF, TOR, NA4, NAPOT = 0x00, 0x08, 0x10, 0x18
R, W, X, L = 0x01, 0x02, 0x04, 0x80


def napot(base, size):
    """pmpaddr for a naturally aligned power-of-two region of at least 8 bytes."""
    return (base >> 2) | ((size >> 3) - 1)


def handler(resume=False):
    """The shared handler; it uses only x26 to x31. With `resume`, an ecall from user mode goes on
    in machine mode at RESUME instead of finishing."""
    head = LI(28, LOG) + [
        LW(29, 28, 0), SLLI(30, 29, 4), ADD(30, 30, 28),
        CSRRS(31, MCAUSE, 0), SW(31, 30, 16),
        CSRRS(27, MTVAL, 0), SW(27, 30, 20),
        CSRRS(27, MEPC, 0), SW(27, 30, 24),
        CSRRS(27, MSTATUS, 0), SW(27, 30, 28),
        ADDI(29, 29, 1), SW(29, 28, 0),
    ]
    # An exception: a fetch fault returns to x1; anything but an ecall returns past the instruction.
    returns = [ADDI(26, 0, 1), BNE(31, 26, 12), CSRRW(0, MEPC, 1), MRET(),
               CSRRS(27, MEPC, 0), ADDI(27, 27, 4), CSRRW(0, MEPC, 27), MRET()]
    # An interrupt (mcause negative) disarms the timer and resumes at mepc.
    interrupt = DISARM_TIMER + [MRET()]
    # A user ecall with `resume`: machine mode at RESUME (MPP back to machine).
    go_on = LI(27, RESUME) + [CSRRW(0, MEPC, 27)] + LI(27, MSTATUS_MPP) + [CSRRS(0, MSTATUS, 27), MRET()] if resume else []
    tests = 5  # the words of `branches` below
    at_returns = len(head) + tests
    at_go_on = at_returns + len(returns) + len(interrupt)
    at_finish = at_go_on + len(go_on)
    branches = [BLT(31, 0, 4 * (at_returns + len(returns) - len(head))),       # an interrupt
                ADDI(26, 0, 8), BEQ(31, 26, 4 * (at_go_on - (len(head) + 2))),  # a user ecall
                ADDI(26, 0, 11), BEQ(31, 26, 4 * (at_finish - (len(head) + 4)))]  # a machine ecall
    assert len(branches) == tests
    return head + branches + returns + interrupt + go_on + FINISH()


def program(machine, user=(), after=()):
    """`machine` from RAM (after mtvec is set), the handler at HANDLER, `user` at USER; with
    `after`, a user ecall continues in machine mode with `after` (at RESUME)."""
    words = LI(5, HANDLER) + [CSRRW(0, MTVEC, 5)] + list(machine)
    assert len(words) <= (RESUME - RAM) // 4, "the machine code overlaps RESUME"
    words += [0] * ((RESUME - RAM) // 4 - len(words)) + list(after)
    assert len(words) <= (HANDLER - RAM) // 4, "the machine code overlaps the handler"
    words += [0] * ((HANDLER - RAM) // 4 - len(words)) + handler(resume=bool(after))
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


class PmpTest(StepTicksCase):

    def log(self, trace):
        """The handler's records, as (mcause, mtval, mepc, mstatus) tuples."""
        count = stored(trace, LOG) or 0
        return [tuple(stored(trace, LOG + 16 * (n + 1) + 4 * k) for k in range(4)) for n in range(count)]

    def test_mret_enters_user_mode_and_ecall_returns_with_cause_8(self):
        user = [ADDI(10, 0, 7), ECALL()]
        emulator, rtl = self.assert_same(program(set_pmp(ALL) + enter_user(), user))
        # MIE was clear when mret entered user mode with MPIE set, so user mode ran with MIE set;
        # the ecall moved it to MPIE and put user (0) in MPP.
        self.assertEqual(self.log(rtl.trace), [(8, 0, USER + 4, MSTATUS_U | 0x80)])
        self.assertEqual(trap_records(rtl.trace), trap_records(emulator.trace))

    def test_mpp_is_warl_machine_supervisor_or_user(self):
        machine = []
        for mpp in range(4):
            machine += LI(5, 0x1800) + [CSRRC(0, MSTATUS, 5)] + LI(5, mpp << 11) + [CSRRS(0, MSTATUS, 5),
                                                                                   CSRRS(10 + mpp, MSTATUS, 0)]
        emulator, rtl = self.assert_same(program(machine + store_at(DUMP, 10, 11, 12, 13) + [ECALL()]))
        got = [stored(rtl.trace, DUMP + 4 * i) for i in range(4)]
        self.assertEqual(got, [MSTATUS_U, MSTATUS_SUPERVISOR, MSTATUS_U, MSTATUS_M])  # 2 is reserved

    def test_privileged_instructions_and_csrs_are_illegal_in_user_mode(self):
        refused = [CSRRS(5, MSTATUS, 0), CSRRS(5, MSCRATCH, 0), MRET(), WFI(),
                   CSRRS(5, MCOUNTEREN, 0), CSRRS(5, PMPADDR0, 0), CSRRS(5, PMPCFG0, 0),
                   RDTIME(5),                                 # TM is clear in mcounteren
                   SRET(), SFENCE_VMA(), CSRRS(5, SSTATUS, 0), CSRRS(5, SATP, 0)]  # issue #20: S mode's
        allowed = [RDCYCLE(6), RDINSTRET(7), RDINSTRETH(8), CSRRS(9, FCSR, 0)]
        user = refused + allowed + [ECALL()]
        machine = set_pmp(ALL) + [CSRRWI(0, MCOUNTEREN, 0b101), CSRRWI(0, SCOUNTEREN, 0b111)] + enter_user()
        emulator, rtl = self.assert_same(program(machine, user))
        expected = [(2, word, USER + 4 * i, MSTATUS_U | 0x80) for i, word in enumerate(refused)]
        expected.append((8, 0, USER + 4 * len(user) - 4, MSTATUS_U | 0x80))
        self.assertEqual(self.log(rtl.trace), expected)

    def test_floating_state_off_in_user_mode(self):
        """Issue #33: with mstatus.FS Off, user mode's F instructions and floating CSRs are illegal too,
        each taken to machine mode with the word in mtval, and FS stays Off through the traps."""
        from tools.rv32_f_asm import arithmetic, flw, fp
        refused = [CSRRS(9, FCSR, 0), fp(0x70, 9, 4), arithmetic(0, 0), flw(1, 0)]
        machine = set_pmp(ALL) + LI(5, 0x6000) + [CSRRC(0, MSTATUS, 5)] + enter_user()
        emulator, rtl = self.assert_same(program(machine, refused + [ECALL()]))
        mstatus = (MSTATUS_U & ~0x80006000) | 0x80  # FS Off, so no SD; MPIE from the mret
        expected = [(2, word, USER + 4 * i, mstatus) for i, word in enumerate(refused)]
        expected.append((8, 0, USER + 4 * len(refused), mstatus))
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
            (LW(12, 25, 2), (4, HANDLER + 2)),                # misaligned and refused: misalignment first
            (SW(21, 22, 0x102), (6, RAM + 0x5102)),           # the same for a store
            (LW(12, 19, 0), (5, UNMAPPED)),                   # outside RAM: no entry either
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
        self.assertEqual(stored(rtl.trace, DUMP), (NAPOT << 16) | (TOR | R | W) << 8)  # entry 6's W dropped
        self.assertEqual(stored(rtl.trace, DUMP + 4), (RAM + 0x5100) >> 2)
        self.assertEqual(stored(rtl.trace, DATA + 4), 0x5A5A5A5A)
        self.assertEqual(stored(rtl.trace, RAM + 0x6000), RAM + 0x6000)

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
        self.assertEqual(stored(rtl.trace, DUMP), 0x1F << 24 | (TOR | L) << 16 | OFF << 8 | (NA4 | L | R))
        self.assertEqual(stored(rtl.trace, DUMP + 4), DATA >> 2)
        self.assertEqual(stored(rtl.trace, DUMP + 8), (RAM + 0x4000) >> 2)
        self.assertEqual(stored(rtl.trace, DUMP + 12), (RAM + 0x5000) >> 2)

    def test_interrupts_are_enabled_in_user_mode(self):
        # User mode runs with MIE clear (MPIE was clear at mret), yet the timer interrupt is taken.
        user = [ADDI(10, 0, 0), ADDI(10, 10, 1), ADDI(11, 0, 200), BLT(10, 11, -8), ECALL()]
        machine = set_pmp(ALL) + set_timer(0) + LI(5, 1 << 7) + [CSRRW(0, MIE_CSR, 5)] + enter_user(mpie=False)
        emulator, rtl = self.assert_same(program(machine, user))
        log = self.log(rtl.trace)
        self.assertEqual(len(log), 2, log)
        self.assertEqual(log[0][:2], (0x80000007, 0))
        self.assertEqual(log[0][3], MSTATUS_U)                # from user mode, with MIE (so MPIE) clear
        self.assertEqual(log[0][2], USER)                     # before the first user instruction
        self.assertEqual(log[1][0], 8)
        self.assertEqual((emulator.halt["interrupts"], rtl.halt["interrupts"]), (1, 1))

    def test_pmp_with_memory_stalls(self):
        user = LI(10, HANDLER) + [LW(11, 10, 0), ECALL()]     # machine code, mapped: PMP refuses it
        words = program(set_pmp({0: (NAPOT | R | X, napot(USER, 0x800))}) + enter_user(), user)
        for seed in (1, 9):
            with self.subTest(seed=seed):
                emulator, rtl = self.assert_same(words, seed=seed)
                self.assertEqual([entry[:3] for entry in self.log(rtl.trace)], [(5, HANDLER, USER + 8), (8, 0, USER + 12)])

    def test_refused_accesses_never_reach_a_device(self):
        # Each of these would change a device if it reached the bus: a console byte, a software
        # interrupt, the done register ending the run, a PLIC claim taking the pending input
        # event, a PLIC priority. User mode may touch none of them, and machine mode then finds
        # every device as it was.
        prio = PLIC + 4 * PLIC_SOURCE_INPUT
        user = LI(10, CONSOLE) + LI(11, ord("X")) + LI(12, MSIP) + LI(13, 1) + LI(14, DONE) + LI(15, 0x5555) \
            + LI(16, PLIC_CLAIM) + LI(17, prio)
        first = USER + 4 * len(user)
        checks = [(SB(11, 10, 0), (7, CONSOLE)), (SW(13, 12, 0), (7, MSIP)), (SW(15, 14, 0), (7, DONE)),
                  (LW(18, 16, 0), (5, PLIC_CLAIM)), (SW(0, 17, 0), (7, prio))]
        user += [word for word, _ in checks] + [ECALL()]
        machine = LI(5, prio) + LI(6, 1) + [SW(6, 5, 0)] + LI(5, PLIC_ENABLE) + LI(6, 1 << PLIC_SOURCE_INPUT) + [SW(6, 5, 0)]
        machine += set_pmp({0: (NAPOT | R | X, napot(USER, 0x800))}) + enter_user()
        after = [CSRRS(20, MIP_CSR, 0)] + LI(5, prio) + [LW(21, 5, 0)] + LI(5, PLIC_CLAIM) + [LW(22, 5, 0)]
        after += store_at(DUMP, 20, 21, 22) + [ECALL()]
        emulator, rtl = self.assert_same(program(machine, user, after), input_script="frame 0 down A\n")
        expected = [(cause, tval, first + 4 * i, MSTATUS_U | 0x80) for i, (_, (cause, tval)) in enumerate(checks)]
        expected += [(8, 0, first + 4 * len(checks), MSTATUS_U | 0x80), (11, 0, RESUME + 4 * (len(after) - 1), MSTATUS_M | 0x80)]
        self.assertEqual(self.log(rtl.trace), expected)
        self.assertEqual((emulator.console, rtl.console), ("", ""))  # no byte reached the console
        self.assertEqual(stored(rtl.trace, DUMP), 1 << 11)           # MEIP from the input; no MSIP
        self.assertEqual(stored(rtl.trace, DUMP + 4), 1)             # the priority as machine mode set it
        self.assertEqual(stored(rtl.trace, DUMP + 8), PLIC_SOURCE_INPUT)  # the event was still unclaimed

    def test_mcounteren_gates_each_counter(self):
        """Since issue #20 user mode needs the counter's bit in scounteren too."""
        reads = [RDCYCLE(5), RDTIME(6), RDINSTRET(7), RDCYCLEH(8), RDTIMEH(9), RDINSTRETH(10)]
        for mcounteren, scounteren in ((0b000, 0b111), (0b010, 0b111), (0b111, 0b111), (0b111, 0b101), (0b110, 0b011)):
            with self.subTest(mcounteren=mcounteren, scounteren=scounteren):
                machine = set_pmp(ALL) + [CSRRWI(0, MCOUNTEREN, mcounteren), CSRRWI(0, SCOUNTEREN, scounteren)]
                emulator, rtl = self.assert_same(program(machine + enter_user(), reads + [ECALL()]))
                enabled = mcounteren & scounteren
                refused = [i for i in range(len(reads)) if not (enabled >> (i % 3)) & 1]
                self.assertEqual([entry[:3] for entry in self.log(rtl.trace)][:-1],
                                 [(2, reads[i], USER + 4 * i) for i in refused])


if __name__ == "__main__":
    unittest.main()
