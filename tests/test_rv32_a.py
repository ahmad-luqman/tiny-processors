"""Issue #34: the A extension (LR.W, SC.W and the nine AMOs) on both backends.

Every program runs on the emulator and the RTL in step-tick mode, so interrupts and devices land
on the same instruction, and must retire identical traces. Results are also asserted against a
Python reference of the AMOs and hand-written expectations of the reservation (docs/rv32-a.md), so
neither backend is checked only against the other.
"""
import unittest

import test_rv32_mmu as mmu
from rv32_step_case import DISARM_TIMER, SAVE, StepTicksCase, at_handler, dump, set_timer, stored
from test_rv32_m import signed
from test_rv32_rtl import effects
from tools.rv32_asm import *  # noqa: F401,F403
from tools.rv32_image import valid_a_word

MASK = 0xFFFFFFFF
DATA = RAM + 0x1000   # scratch words past every image here


def amo_reference(op, old, operand):
    """What an AMO writes, from the specification's definitions in Python integers."""
    return {
        AMOSWAP_W: operand,
        AMOADD_W: (old + operand) & MASK,
        AMOXOR_W: old ^ operand,
        AMOAND_W: old & operand,
        AMOOR_W: old | operand,
        AMOMIN_W: old if signed(old) < signed(operand) else operand,
        AMOMAX_W: old if signed(old) > signed(operand) else operand,
        AMOMINU_W: min(old, operand),
        AMOMAXU_W: max(old, operand),
    }[op]


EDGES = (0, 1, 0x7FFFFFFF, 0x80000000, 0xFFFFFFFF, 0x55555555, 0xAAAAAAAA, 0x00010000)


def skip_and_return():
    """A machine-mode handler that stores mcause and mtval at SAVE and counts at SAVE + 8, then
    returns with a jump, not mret, so a reservation that ends was ended by the trap itself: past
    the trapping instruction after an exception, to the interrupted one after an interrupt (with
    the timer disarmed). The jump leaves MIE clear, which no body here relies on."""
    return LI(28, SAVE) + [
        CSRRS(29, MCAUSE, 0), SW(29, 28, 0),
        CSRRS(29, MTVAL, 0), SW(29, 28, 4),
        LW(29, 28, 8), ADDI(29, 29, 1), SW(29, 28, 8),
        CSRRS(30, MEPC, 0), CSRRS(29, MCAUSE, 0), BLT(29, 0, 4 * 3),
        ADDI(30, 30, 4), JALR(0, 30, 0),
    ] + DISARM_TIMER + [JALR(0, 30, 0)]


def poll_until_idle(base, status, busy):
    """Spin until the device's status word at base + status has the busy bit clear (x8-x10)."""
    return LI(8, base) + [LW(10, 8, status), ANDI(10, 10, busy), BNE(10, 0, -8)]


class AtomicTest(StepTicksCase):

    def test_every_amo_against_the_reference(self):
        """Each AMO on each pair of edge values: rd gets the old word, memory the reference's value,
        and the trace line carries both effects, the write first. rd rotates, sometimes onto rs2."""
        words, checks = [], []
        index = 0
        for op in AMO_OPS:
            for i, old in enumerate(EDGES):
                operand = EDGES[(i * 3 + 1) % len(EDGES)]
                rd = 8 + index % 20
                rs2 = rd if index % 5 == 2 else 6  # sometimes rd is rs2 as well
                address = DATA + 4 * (index % 16)
                words += LI(7, address) + LI(6, old) + [SW(6, 7, 0)] + LI(rs2, operand)
                words.append(op(rd, rs2, 7, aq=index & 1, rl=(index >> 1) & 1))
                new = amo_reference(op, old, operand)
                checks.append((len(words) - 1, f"x{rd}={old:08x} mem[{address:08x}]<-{new:08x}/4 mem[{address:08x}]->{old:08x}/4",
                               (op.__name__, hex(old), hex(operand))))
                index += 1
        last = new
        words += LI(6, 0x10) + [AMOADD_W(0, 6, 7)]  # rd = x0: memory still changes, no register effect
        checks.append((len(words) - 1, f"mem[{address:08x}]<-{(last + 0x10) & MASK:08x}/4 mem[{address:08x}]->{last:08x}/4",
                       "rd=x0"))
        alias = DATA + 0x40  # rd = rs1: the address is read before rd is written
        words += LI(9, alias) + LI(6, 0x30) + [SW(6, 9, 0)] + LI(6, 0x12) + [AMOADD_W(9, 6, 9)]
        checks.append((len(words) - 1, f"x9=00000030 mem[{alias:08x}]<-00000042/4 mem[{alias:08x}]->00000030/4", "rd=rs1"))
        emulator, rtl = self.assert_same(words + FINISH())
        for line, expected, case in checks:
            self.assertEqual(effects(rtl.trace[line]), expected, case)
        self.assert_relation(rtl)

    def test_cycles_and_transfers_with_stalls(self):
        """An AMO costs 4 + 2 cycles (one per data access) and 3 transfers (its fetch, read and
        write); a failed SC 4 cycles and its fetch alone."""
        words = LI(7, DATA) + [AMOSWAP_W(5, 7, 7), LR_W(6, 7), SC_W(8, 0, 7), SC_W(9, 0, 7)] + FINISH()
        for stall in (0, 2):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same(words, stall=stall)
                self.assert_relation(rtl)
                steps = len(rtl.trace)
                data = sum(line.count("mem[") for line in rtl.trace)
                self.assertEqual(data, 2 + 1 + 1 + 0 + 1, "AMO 2, LR 1, SC 1, failed SC 0, the done store 1")
                self.assertEqual(rtl.halt["transfers"], steps + data)
                self.assertEqual(rtl.halt["cycles"], 4 * steps + data + stall * (steps + data))

    def test_amos_stay_whole_under_interrupts_and_stalls(self):
        """Cycle ticks with a stalled bus: the timer interrupts a loop of 300 AMOs many times on
        clock time, yet it is taken only between instructions, never between an AMO's read and
        its write, so every increment lands; the testbench checks each AMO's pair of accesses."""
        handler = LI(28, SAVE) + [LW(29, 28, 12), ADDI(29, 29, 1), SW(29, 28, 12)]
        handler += LI(27, MTIME) + [LW(26, 27, 0), ADDI(26, 26, 97)] + LI(27, MTIMECMP) + [SW(26, 27, 0), MRET()]
        body = LI(7, DATA) + [SW(0, 7, 0), ADDI(6, 0, 1)] + set_timer(150) + LI(1, 1 << 7) + [CSRRW(0, MIE_CSR, 1)]
        body += LI(9, 300) + [CSRRSI(0, MSTATUS, 8), AMOADD_W(0, 6, 7), ADDI(9, 9, -1), BNE(9, 0, -8)]
        body += [CSRRCI(0, MSTATUS, 8), LW(10, 7, 0)] + LI(28, SAVE) + [LW(11, 28, 12)] + dump(10, 11)
        emulator, rtl = self.run_both(at_handler(body, handler), ticks="cycles", stall=3)
        for run in (emulator, rtl):
            self.assertEqual(run.halt["outcome"], "pass")
            self.assertEqual(stored(run.trace, SAVE + 0x40), 300)
        self.assertGreaterEqual(rtl.halt["interrupts"], 10, "the RTL's timer interrupted the loop")
        self.assertEqual(stored(rtl.trace, SAVE + 0x44), rtl.halt["interrupts"])

    def test_lr_and_sc(self):
        a, b = DATA, DATA + 4
        words = LI(7, a) + LI(8, b) + LI(5, 0x1234) + [SW(5, 7, 0)]
        words += [LR_W(10, 7), SC_W(11, 5, 7)]                   # x11=0: stored, x10=1234
        words += [SC_W(12, 0, 7)]                                # x12=1: SC after SC
        words += [LR_W(13, 7, aq=1, rl=1), SC_W(14, 0, 8)]       # x14=1: another word; b unchanged
        words += [SC_W(15, 0, 7)]                                # x15=1: SC failed and cleared
        words += [LR_W(16, 8), SW(0, 8, 0), SC_W(17, 5, 8, aq=1)]  # x17=0: a plain store keeps it
        words += [LR_W(18, 7), LR_W(19, 8), SC_W(20, 0, 7)]      # x20=1: the second LR moved it
        emulator, rtl = self.assert_same(words + dump(10, 11, 12, 13, 14, 15, 16, 17, 18, 20))
        got = [stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(10)]
        self.assertEqual(got, [0x1234, 0, 1, 0x1234, 1, 1, 0, 0, 0x1234, 1])
        sc_lines = [line for line in rtl.trace if int(line.split()[2], 16) & 0x7F == 0x2F
                    and int(line.split()[2], 16) >> 27 == SC_W(0, 0, 0) >> 27]
        self.assertEqual([effects(line) for line in sc_lines],
                         [f"x11=00000000 mem[{a:08x}]<-00001234/4", "x12=00000001", "x14=00000001", "x15=00000001",
                          f"x17=00000000 mem[{b:08x}]<-00001234/4", "x20=00000001"],
                         "a failed SC has no memory effect")
        self.assert_relation(rtl)

    def test_a_trap_or_an_xret_between_lr_and_sc_fails_it(self):
        """The reservation ends at a trap (an ecall, an illegal instruction), whose handler returns
        with a jump, and at a bare mret, as on QEMU; a plain jump between LR and SC keeps it."""
        body = LI(7, DATA)
        body += [LR_W(5, 7), ECALL(), SC_W(10, 0, 7)]                       # x10=1
        body += [LR_W(5, 7), 0, SC_W(11, 0, 7)]                             # x11=1: illegal word 0
        body += [LR_W(5, 7), AUIPC(6, 0), ADDI(6, 6, 28), CSRRW(0, MEPC, 6)]
        body += LI(6, 0x1800) + [CSRRS(0, MSTATUS, 6), MRET(), SC_W(12, 0, 7)]  # x12=1: mret alone
        body += [LR_W(5, 7), BEQ(0, 0, 8), 0, SC_W(13, 0, 7)]              # x13=0: a jump keeps it
        emulator, rtl = self.assert_same(at_handler(body + dump(10, 11, 12, 13), skip_and_return()))
        self.assertEqual([stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(4)], [1, 1, 1, 0])
        self.assertEqual(stored(rtl.trace, SAVE + 8), 2, "two traps")

    def test_an_interrupt_between_lr_and_sc_fails_it(self):
        """The issue's directed test: the timer interrupts between LR and SC; the SC fails, writes
        nothing, and memory keeps the word."""
        body = LI(7, DATA) + LI(5, 0x77) + [SW(5, 7, 0)]
        body += set_timer(0) + LI(1, 1 << 7) + [CSRRW(0, MIE_CSR, 1)]
        body += [LR_W(5, 7), CSRRSI(0, MSTATUS, 8),   # the interrupt comes after this
                 CSRRCI(0, MSTATUS, 8), SC_W(10, 0, 7), LW(11, 7, 0)]
        emulator, rtl = self.assert_same(at_handler(body + dump(10, 11), skip_and_return()))
        self.assertEqual(sum(" interrupt 7" in line for line in rtl.trace), 1)
        self.assertEqual([stored(rtl.trace, SAVE + 0x40), stored(rtl.trace, SAVE + 0x44)], [1, 0x77])
        sc = next(line for line in rtl.trace if line.split()[2] == f"{SC_W(10, 0, 7):08x}")
        self.assertEqual(effects(sc), "x10=00000001")

    def test_a_translation_change_ends_the_reservation(self):
        """A satp write or sfence.vma between LR and SC fails the SC: after either, the virtual word
        may name another page. Machine mode may do both, and satp's MODE stays Bare here."""
        body = LI(7, DATA)
        body += [LR_W(5, 7), CSRRW(0, SATP, 0), SC_W(10, 0, 7)]
        body += [LR_W(5, 7), SFENCE_VMA(), SC_W(11, 0, 7)]
        body += [LR_W(5, 7), CSRRS(0, SATP, 0), SC_W(12, 0, 7)]  # a read of satp writes nothing
        emulator, rtl = self.assert_same(body + dump(10, 11, 12))
        self.assertEqual([stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(3)], [1, 1, 0])

    def test_a_device_write_ends_the_reservation(self):
        """Codex P1: a device's write to the reserved word fails the SC. virtio-blk reads sector 0
        into `data` during the notify store (O3); an LR on a word it writes fails, one on a word it
        does not touch succeeds. The notify itself is a plain store, which keeps a reservation."""
        ring = RAM + 0x3000
        desc, avail, used, header, data, status = ring, ring + 0x100, ring + 0x200, ring + 0x300, ring + 0x400, ring + 0x600
        for target, outcome in ((data + 8, 1), (ring + 0x800, 0)):
            with self.subTest(target=hex(target)):
                words = []

                def put(address, value):
                    words.extend(LI(1, address) + LI(2, value) + [SW(2, 1, 0)])

                for i, (address, length, flags) in enumerate(((header, 16, 1 | 1 << 16), (data, 512, 3 | 2 << 16),
                                                              (status, 1, 2))):
                    put(desc + 16 * i, address)
                    put(desc + 16 * i + 8, length)
                    put(desc + 16 * i + 12, flags)
                put(avail, 1 << 16)
                for offset, value in ((0x070, 0), (0x070, 1), (0x070, 3), (0x070, 11), (0x030, 0), (0x038, 8),
                                      (0x080, desc), (0x090, avail), (0x0A0, used), (0x044, 1), (0x070, 15)):
                    put(VIRTIO + offset, value)
                words += LI(7, target) + [LR_W(5, 7)]
                put(VIRTIO + 0x050, 0)                    # notify: the DMA runs before this store retires
                words += [SC_W(10, 0, 7)]
                emulator, rtl = self.assert_same(words + dump(10))
                self.assertEqual(stored(rtl.trace, SAVE + 0x40), outcome)

    def test_an_engine_write_ends_the_reservation(self):
        """The same for G2: CLEAR_Z writes every word of the depth buffer, so an LR on one of them
        fails its SC once the clear has run; an LR outside the buffer succeeds. G2 runs on clock
        time in the RTL even with step ticks, so the polling loop's length differs between the
        backends and only the results are compared."""
        zbase = RAM + 0x100000
        for target, outcome in ((zbase + 0x40, 1), (DATA, 0)):
            with self.subTest(target=hex(target)):
                body = LI(7, target) + [LR_W(5, 7)]
                body += LI(8, G3D_BASE) + LI(9, zbase) + [SW(9, 8, G3D_ZBASE), ADDI(9, 0, G3D_CLEAR_Z), SW(9, 8, G3D_COMMAND)]
                body += poll_until_idle(G3D_BASE, G3D_STATUS, G3D_BUSY) + [SC_W(11, 0, 7), LW(12, 7, 0)]
                emulator, rtl = self.run_both(body + dump(11, 12), limit=400000)
                for run in (emulator, rtl):
                    self.assertEqual(run.halt["outcome"], "pass")
                    self.assertEqual(stored(run.trace, SAVE + 0x40), outcome)
                    self.assertEqual(stored(run.trace, SAVE + 0x44), 0xFFFFFFFF if outcome else 0,
                                     "the clear's word survives the failed SC")

    def test_an_engine_framebuffer_write_ends_the_reservation(self):
        """Codex P1, third round: the framebuffer is memory LR.W can reserve while the engines are
        idle. A G1 fill over the reserved pixel word fails the SC once it has run; a word the fill
        does not touch keeps its reservation. G1, like G2, runs on clock time, so only results are
        compared."""
        params = lambda index: GPU_BASE + GPU_PARAMS + 4 * index  # noqa: E731
        for target, outcome in ((FB, 1), (FB + 0x40, 0)):
            with self.subTest(target=hex(target)):
                body = LI(7, target) + [LR_W(5, 7)]
                for index, value in ((0, GPU_FILL), (1, 0x5A), (2, 0), (3, 0), (8, 4), (9, 1)):  # OP COLOR X0 Y0 W H
                    body += LI(1, params(index)) + LI(2, value) + [SW(2, 1, 0)]
                body += LI(1, GPU_BASE + GPU_COMMAND) + [ADDI(2, 0, GPU_START), SW(2, 1, 0)]
                body += poll_until_idle(GPU_BASE, GPU_STATUS, GPU_BUSY) + [SC_W(11, 0, 7), LW(12, 7, 0)]
                emulator, rtl = self.run_both(body + dump(11, 12))
                for run in (emulator, rtl):
                    self.assertEqual(run.halt["outcome"], "pass")
                    self.assertEqual(stored(run.trace, SAVE + 0x40), outcome)
                    self.assertEqual(stored(run.trace, SAVE + 0x44), 0x5A5A5A5A if outcome else 0,
                                     "the fill's pixels survive the failed SC")

    def test_no_engine_write_lands_inside_an_atomic_access(self):
        """Codex P1, second round: G2 clears its depth buffer, with seeded waits on its RAM port and
        on the CPU's, while the CPU runs LR/SC pairs on depth words near the clear and AMOs on a
        word outside it. While G2 runs, an SC there must fail or take a store fault, never succeed,
        whether the clear's write lands before EXECUTE, during the walk to MEM (the SC then fails in
        MEM with no access) or would land while the store waits (the core's lock keeps it out). The
        testbench enforces the rest on every cycle: no device write while the core presents an SC
        or an AMO access, and no SC store after its reservation ended."""
        zbase = RAM + 0x100000
        body = LI(8, G3D_BASE) + LI(9, zbase) + [SW(9, 8, G3D_ZBASE), ADDI(9, 0, G3D_CLEAR_Z), SW(9, 8, G3D_COMMAND)]
        body += LI(7, zbase) + LI(21, DATA) + [ADDI(20, 0, 0), ADDI(22, 0, 1)] + LI(23, 200)
        loop = [ADDI(11, 0, 2), LR_W(5, 7), SC_W(11, 0, 7),          # x11 stays 2 if the SC traps
                BNE(11, 0, 8), ADDI(20, 20, 1),                      # count successes
                AMOADD_W(0, 22, 21), ADDI(7, 7, 32), ADDI(23, 23, -1), BNE(23, 0, -32)]
        body += loop + [LW(24, 8, G3D_STATUS)] + LI(28, SAVE) + [LW(25, 28, 8), LW(26, 21, 0)] + dump(20, 24, 25, 26)
        for gpu_seed in (3, 11):
            with self.subTest(gpu_seed=gpu_seed):
                emulator, rtl = self.run_both(at_handler(body, skip_and_return()), ticks="cycles", seed=gpu_seed + 1,
                                              gpu_seed=gpu_seed, limit=400000)
                for run in (emulator, rtl):
                    self.assertEqual(run.halt["outcome"], "pass")
                    self.assertEqual(stored(run.trace, SAVE + 0x40), 0, "no SC succeeded while G2 ran")
                    self.assertEqual(stored(run.trace, SAVE + 0x44) & G3D_BUSY, G3D_BUSY, "G2 outlasted the loop")
                    self.assertEqual(stored(run.trace, SAVE + 0x4C), 200, "every AMO landed")
                self.assertGreater(stored(rtl.trace, SAVE + 0x48), 0, "some SCs faulted on the locked buffer")

    def test_every_funct5(self):
        """All 32 funct5 values under opcode 0x2f (funct3 2, rs2 x6, and LR.W with rs2 x0 too): the
        backends trap exactly on the words valid_a_word refuses, and those are exactly the ones
        the assembler cannot encode. With the reservation never held, SC.W fails without a fault."""
        words = [r_type(0x2F, 5, 2, 7, 6, funct5 << 2) for funct5 in range(32)] + [LR_W(5, 7)]
        body = LI(7, DATA) + LI(6, 3) + words + FINISH()
        emulator, rtl = self.assert_same(at_handler(body, skip_and_return()))
        trapped = {int(line.split()[2], 16) for line in rtl.trace if line.endswith(f" trap 2 {line.split()[2]}")}
        self.assertEqual(trapped, {word for word in words if not valid_a_word(word)})
        encodable = {op(5, 6, 7) for op in AMO_OPS} | {SC_W(5, 6, 7), LR_W(5, 7)}
        self.assertEqual({word for word in words if valid_a_word(word)}, encodable)
        self.assertEqual(len(encodable), 11)

    def test_faults(self):
        """Misaligned: LR is a load (4), SC and the AMOs are stores (6), SC even without a
        reservation. Unmapped: LR 5, an AMO 7; a failing SC there has no access and so no fault.
        Each trap reports the address in mtval."""
        cases = [
            (LI(7, DATA + 2) + [LR_W(5, 7)], 4, DATA + 2),
            (LI(7, DATA + 1) + [SC_W(5, 0, 7)], 6, DATA + 1),
            (LI(8, DATA) + LI(7, DATA + 2) + [LR_W(5, 8), SC_W(5, 0, 7)], 6, DATA + 2),
            (LI(7, DATA + 3) + [AMOADD_W(5, 0, 7)], 6, DATA + 3),
            (LI(7, 0x00200000) + [LR_W(5, 7)], 5, 0x00200000),
            (LI(7, 0x00200000) + [AMOOR_W(5, 0, 7)], 7, 0x00200000),
            (LI(7, RAM + 0x01000000) + [AMOSWAP_W(5, 0, 7)], 7, RAM + 0x01000000),  # one past RAM
            (LI(7, CONSOLE) + [AMOSWAP_W(5, 0, 7)], 7, CONSOLE),      # the console takes no words
            (LI(7, DONE) + [AMOSWAP_W(5, 0, 7)], 7, DONE),            # write-only: the read is refused
            (LI(7, BOOTROM) + [AMOSWAP_W(5, 0, 7)], 7, BOOTROM),      # read-only: the write is refused
        ]
        for body, cause, value in cases:
            with self.subTest(cause=cause, value=hex(value)):
                emulator, rtl = self.assert_same(at_handler(body + FINISH(), skip_and_return()))
                self.assertEqual([stored(rtl.trace, SAVE), stored(rtl.trace, SAVE + 4)], [cause, value])
        emulator, rtl = self.assert_same(LI(7, 0x00200000) + [SC_W(5, 0, 7)] + dump(5))
        self.assertEqual(stored(rtl.trace, SAVE + 0x40), 1, "no reservation: no access, no fault")

    def test_pmp_asks_an_amo_for_read_and_write(self):
        """Locked PMP entries bind machine mode too: on a word PMP grants only R, LR reads, while an
        AMO and a reserved SC are store access faults (7) before any bus access; on a word that
        grants R and W both work. An AMO checked as a load (R only) would pass on the first word. W
        without R is a reserved encoding PMP stores as neither, so no entry grants W alone and R and
        W together are, in effect, W: the R half has no case to test."""
        read_only, read_write = DATA, DATA + 4
        locked_na4 = 0x80 | 0x10
        body = LI(5, read_only >> 2) + [CSRRW(0, PMPADDR0, 5)]
        body += LI(5, read_write >> 2) + [CSRRW(0, PMPADDR0 + 1, 5)]
        body += LI(5, (locked_na4 | 0x3) << 8 | (locked_na4 | 0x1)) + [CSRRW(0, PMPCFG0, 5)]
        body += LI(7, read_only) + [LR_W(10, 7), AMOADD_W(11, 7, 7), LR_W(10, 7), SC_W(12, 0, 7)]
        body += LI(7, read_write) + [AMOSWAP_W(13, 7, 7), LR_W(14, 7), SC_W(15, 0, 7), LW(16, 7, 0)]
        emulator, rtl = self.assert_same(at_handler(body + dump(13, 15, 16), skip_and_return()))
        traps = [line.split()[-2:] for line in rtl.trace if " trap " in line]
        self.assertEqual(traps, [["7", f"{read_only:08x}"], ["7", f"{read_only:08x}"]], "the AMO and the SC")
        self.assertEqual([stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(3)], [0, 0, 0],
                         "the AMO read 0 and stored the address, then the SC stored 0")
        swap = next(line for line in rtl.trace if line.split()[2] == f"{AMOSWAP_W(13, 7, 7):08x}")
        self.assertEqual(effects(swap), f"x13=00000000 mem[{read_write:08x}]<-{read_write:08x}/4 mem[{read_write:08x}]->00000000/4")

    def test_illegal_encodings(self):
        for word in (LR_W(5, 7) | 3 << 20,                 # LR.W with rs2 != 0
                     AMOADD_W(5, 6, 7) ^ 1 << 12,          # funct3 3: a doubleword
                     AMOADD_W(5, 6, 7) & ~(7 << 12),       # funct3 0
                     r_type(0x2F, 5, 2, 7, 6, 0b00101 << 2),  # AMOCAS.W (Zacas)
                     r_type(0x2F, 5, 2, 7, 6, 0b11111 << 2)):
            with self.subTest(word=f"{word:08x}"):
                emulator, rtl = self.assert_same(at_handler(LI(7, DATA) + [word] + FINISH(), skip_and_return()))
                self.assertEqual([stored(rtl.trace, SAVE), stored(rtl.trace, SAVE + 4)], [2, word])

    def test_an_amo_on_devices(self):
        """A device sees the read and then the write, under its own rules: CLINT's mtimecmp takes
        both; the input queue's read pops an event and its refused write faults, on both backends."""
        body = LI(7, MTIMECMP) + LI(6, 5) + [SW(6, 7, 0), AMOADD_W(10, 6, 7), LW(11, 7, 0)]
        body += LI(7, INPUT) + [AMOSWAP_W(12, 0, 7)]   # traps; x12 untouched
        body += LI(7, INPUT + 4) + [LW(13, 7, 0)]        # the queue's count afterwards
        script = "frame 0 down LEFT\nframe 0 down RIGHT\n"
        words = at_handler(body + dump(10, 11, 13), skip_and_return())
        emulator, rtl = self.assert_same(words, input_script=script)
        self.assertEqual([stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(3)], [5, 10, 1], "one event popped")
        self.assertEqual([stored(rtl.trace, SAVE), stored(rtl.trace, SAVE + 4)], [7, INPUT])

    def test_sv32_store_permission(self):
        """Under Sv32 an AMO and an SC walk as stores: a page without W, or with D clear, is a store
        page fault (15); LR walks as a load and reads it. On the read-only page the AMO's fault ends
        the LR's reservation, so the SC after it fails with no access and no fault."""
        read_only = mmu.TEST_VA + 0x4000   # V R A
        clean = mmu.TEST_VA + 0x9000       # V R W A, D clear
        supervisor = LI(10, read_only) + [LR_W(11, 10), AMOADD_W(12, 0, 10), SC_W(13, 0, 10)]
        supervisor += LI(10, clean) + [LR_W(14, 10), SC_W(15, 0, 10), AMOSWAP_W(16, 0, 10)]
        supervisor += LI(10, mmu.TEST_VA) + [AMOADD_W(17, 10, 10)]  # a writable page: works
        supervisor += LI(16, mmu.DUMP) + [SW(11, 16, 0), SW(14, 16, 4), SW(17, 16, 8), SW(13, 16, 12)] + FINISH()
        emulator, rtl = self.assert_same(mmu.program(mmu.pmp(), supervisor))
        log = [entry[:2] for entry in mmu.trap_log(rtl.trace)]
        self.assertEqual(log, [(15, read_only), (15, clean), (15, clean)])
        self.assertEqual([stored(rtl.trace, mmu.DUMP + 4 * i) for i in range(4)], [mmu.pte(mmu.PAGE_S, PTE_V | PTE_R | PTE_A), 0x5000, 0x5000, 1],
                         "LR reads the guarded page's entries and PAGE_S, the AMO returns its old word, the SC failed")

    def test_sv32_lr_sc_and_amo_succeed(self):
        """Under Sv32, where the virtual and physical words differ: an LR/SC pair stores, an AMO
        that walks (a TLB miss, through a second virtual page of the same physical page) and one
        that hits each update the word, and loads through the physical address see every write.
        Trap-free, so the cycle relation holds, with and without stalls."""
        word = mmu.TEST_VA + 0x20                 # PAGE_S + 0x20, which holds 0x5008
        other = mmu.TEST_VA + 0x5000 + 0x24       # PAGE_S + 0x24 again, 0x5009, through level0[5]
        supervisor = LI(10, word) + [LR_W(11, 10)] + LI(12, 0x1111) + [SC_W(13, 12, 10)]
        supervisor += LI(14, other) + [AMOADD_W(15, 12, 14), AMOADD_W(16, 12, 14)]
        supervisor += LI(17, mmu.PAGE_S + 0x20) + [LW(18, 17, 0), LW(19, 17, 4)]
        supervisor += LI(20, mmu.DUMP) + [SW(reg, 20, 4 * i) for i, reg in enumerate((11, 13, 15, 16, 18, 19))] + FINISH()
        for stall in (0, 2):
            with self.subTest(stall=stall):
                emulator, rtl = self.assert_same(mmu.program(mmu.pmp(), supervisor), stall=stall)
                self.assertEqual([stored(rtl.trace, mmu.DUMP + 4 * i) for i in range(6)],
                                 [0x5008, 0, 0x5009, 0x611A, 0x1111, 0x722B])
                self.assertEqual(mmu.trap_log(rtl.trace), [])
                self.assert_relation(rtl)

    def test_an_mprv_change_between_lr_and_sc_fails_it(self):
        """Codex P2, fourth round: machine mode with MPRV and MPP = S reserves a word through Sv32
        (TEST_VA + 0x20, PAGE_S + 0x20); with MPRV cleared the same virtual address is untranslated
        and names another word (the console's), so the SC compares physical words and fails without
        an access. The control, with MPRV still set, stores."""
        word = mmu.TEST_VA + 0x20
        mprv_s = MSTATUS_MPRV | 0x800                # MPRV, MPP = S
        machine = mmu.pmp() + LI(5, SATP_SV32 | mmu.ROOT >> 12) + [CSRRW(0, SATP, 5), SFENCE_VMA()]
        machine += LI(6, 0x1800) + [CSRRC(0, MSTATUS, 6)] + LI(6, mprv_s) + [CSRRS(0, MSTATUS, 6)]
        machine += LI(10, word) + LI(12, 0x77) + [LR_W(11, 10), SC_W(13, 12, 10)]      # translated: stores
        machine += [LR_W(11, 10)] + LI(6, MSTATUS_MPRV) + [CSRRC(0, MSTATUS, 6), SC_W(14, 12, 10)]
        machine += LI(16, mmu.PAGE_S + 0x20) + [LW(15, 16, 0)] + dump(13, 14, 15)
        emulator, rtl = self.assert_same(mmu.program(machine, []))
        self.assertEqual([stored(rtl.trace, SAVE + 0x40 + 4 * i) for i in range(3)], [0, 1, 0x77])
        sc = [line for line in rtl.trace if line.split()[2] == f"{SC_W(14, 12, 10):08x}"]
        self.assertEqual([effects(line) for line in sc], ["x14=00000001"], "no access at the console")

    def test_sv32_faults(self):
        """An AMO that is the first access to a page without W, or with D clear, faults (15) on the
        walk, as a store; LR on an invalid page is a load page fault (13); a misaligned SC or AMO
        there is misaligned (6) before any translation; an SC without the reservation does not
        translate, so it fails there with no fault."""
        read_only, clean, invalid = mmu.TEST_VA + 0x4000, mmu.TEST_VA + 0x9000, mmu.TEST_VA + 0x2000
        supervisor = LI(10, read_only) + [AMOADD_W(11, 0, 10)]
        supervisor += LI(10, clean) + [AMOSWAP_W(11, 0, 10)]
        supervisor += LI(10, invalid) + [LR_W(11, 10)]
        supervisor += LI(10, invalid + 2) + [AMOOR_W(11, 0, 10)]
        supervisor += LI(10, invalid + 1) + [SC_W(11, 0, 10)]
        supervisor += LI(10, invalid) + [SC_W(12, 0, 10)]
        supervisor += LI(16, mmu.DUMP) + [SW(12, 16, 0)] + FINISH()
        emulator, rtl = self.assert_same(mmu.program(mmu.pmp(), supervisor))
        self.assertEqual([entry[:2] for entry in mmu.trap_log(rtl.trace)],
                         [(15, read_only), (15, clean), (13, invalid), (6, invalid + 2), (6, invalid + 1)])
        self.assertEqual(stored(rtl.trace, mmu.DUMP), 1)

    def test_sret_ends_the_reservation(self):
        """LR in S mode, then sret to U mode: the SC there fails on the same virtual word, so a
        reservation cannot pass from one privilege mode to another. The S-mode pair before it, on a
        user page with SUM set, succeeds."""
        va = mmu.ALIAS + 0x1800                   # RAM + 0x1800 through the user alias
        supervisor = LI(6, MSTATUS_SUM) + [CSRRS(0, SSTATUS, 6)]
        supervisor += LI(10, va) + [LR_W(11, 10), SC_W(12, 0, 10)]
        supervisor += LI(16, mmu.DUMP) + [SW(12, 16, 4), LR_W(11, 10)]
        supervisor += LI(5, mmu.ALIAS + (mmu.USER_CODE - RAM)) + [CSRRW(0, SEPC, 5)]
        supervisor += LI(6, MSTATUS_SPP) + [CSRRC(0, SSTATUS, 6), SRET()]
        user = LI(10, va) + [SC_W(13, 0, 10)] + LI(14, mmu.ALIAS + (mmu.DUMP - RAM)) + [SW(13, 14, 0), ECALL()]
        emulator, rtl = self.assert_same(mmu.program(mmu.pmp(), supervisor, user))
        # The trace shows the virtual address of each store: the user's through the alias.
        self.assertEqual([stored(rtl.trace, mmu.ALIAS + (mmu.DUMP - RAM)), stored(rtl.trace, mmu.DUMP + 4)], [1, 0])
        self.assertEqual([entry[:2] for entry in mmu.trap_log(rtl.trace)], [(8, 0)])


if __name__ == "__main__":
    unittest.main()
