# The A extension (issue #34)

[Issue #34](https://github.com/ahmad-luqman/tiny-processors/issues/34) is Track 3
stream B's second step ([plan](planning/track3-apps.md#stream-b-bigger-programs)).
It adds RV32A to both backends: `LR.W`, `SC.W` and the nine AMOs (`.W`). Linux
needs them (#36), and the kernel could use real atomics. Like F1 and F2 it is
a hardware milestone. The OS build is unchanged.

The contract below was settled by probing QEMU 11.1.2 (`-M virt -cpu
rv32,c=false,d=false`) with a bare-metal program, before any test was pinned.

## Encoding

Opcode `0x2F`, funct3 2 (a word), the operation in funct5 (bits 31:27), then
`aq` and `rl` (bits 26:25). The address is `rs1` alone, with no offset.

| funct5 | Instruction | funct5 | Instruction |
| --- | --- | --- | --- |
| `00010` | `lr.w` (`rs2` must be 0) | `01000` | `amoor.w` |
| `00011` | `sc.w` | `01100` | `amoand.w` |
| `00001` | `amoswap.w` | `10000` | `amomin.w` |
| `00000` | `amoadd.w` | `10100` | `amomax.w` |
| `00100` | `amoxor.w` | `11000` | `amominu.w` |
| | | `11100` | `amomaxu.w` |

Any other word under `0x2F` is an illegal instruction (cause 2, `mtval` the
word): another funct3 (a doubleword, say), an undefined funct5 (`amocas.w` is
Zacas, which we do not have), or `lr.w` with `rs2` nonzero. `aq` and `rl` take
any value and change nothing, since there is one hart and no cache.

## What each instruction does

| | `lr.w` | `sc.w` | AMOs |
| --- | --- | --- | --- |
| Faults | a load's: misaligned 4, access 5, page 13 | a store's: 6, 7, 15 | a store's: 6, 7, 15 |
| Sv32 walk | a load's (R, or X with MXR) | a store's (W and D) | a store's (W and D) |
| PMP needs | R | W | R and W |
| Bus | one read | one write if it succeeds, none if it fails | a read, then a write to the same address |
| `rd` | the word read | 0 if it stored, 1 if it failed | the word read |

PMP has no W-only encoding (W without R is stored as neither), so an AMO's
"R and W" acts as W.

An AMO writes `rs2`, or the word combined with `rs2`. `min` and `max` compare as
signed, `minu` and `maxu` as unsigned. With `rd` equal to `rs2`, the AMO uses
`rs2`'s old value and then writes `rd`.

Misalignment is never emulated, as for loads and stores. QEMU agrees for LR,
SC and the AMOs. QEMU does perform a misaligned *plain* load, but our machine
has always trapped on one.

## The reservation

The reservation is a valid bit and a word, held twice: as its virtual address,
which SC.W compares, and as its physical address, which device writes are
checked against.

- **LR.W** sets it as its read is accepted.
- **SC.W** checks alignment first, whether or not a reservation is held (QEMU
  does the same).
  - If the reservation holds the same word, the SC is a store.
  - Otherwise it fails with no access at all: no translation, no PMP and no bus
    request, so it raises no page or access fault. An SC with no reservation to
    an unmapped address simply returns 1, on QEMU as here.
  - Every SC clears the reservation, whether it succeeded or failed.
- **Any trap clears it.** That covers exceptions and interrupts alike. The tests
  return from their handler with a jump, so trap entry alone is shown to end it.
- **`mret` and `sret` clear it too.** QEMU does this, and the privileged spec
  allows it. Matching QEMU lets these cases be compared with QEMU.
- **A new translation clears it:** a `satp` write or `sfence.vma`, after which
  the same virtual word may name another page. With traps and xRET clearing it
  as well, comparing virtual addresses in SC is safe.
- **A device's write to the reserved word clears it.** The A extension requires
  an SC to fail when another agent wrote the bytes its LR read. G2's depth
  buffer writes and virtio-blk's DMA are the devices that write RAM (G1 writes
  only the framebuffer). The SoC tells the core of each such write
  (`ram_snoop_write`, `ram_snoop_addr`); the emulator checks in its DMA write
  and after each G2 tick.
- **This hart's own plain store** to the reserved word does not clear it, which
  is legal: only another agent's write must.

What QEMU showed, and where we differ:

| Probe | QEMU | Ours |
| --- | --- | --- |
| misaligned `lr.w` / `sc.w` / `amoadd.w` | 4 / 6 / 6 | the same |
| misaligned `sc.w` without a reservation | 6 | the same |
| `amoadd.w` / `lr.w` at an unmapped address | 7 / 5 | the same |
| `sc.w` at an unmapped address, no reservation | no trap, `rd` = 1 | the same |
| LR, then `ecall` or an illegal word, handler returns with a jump, then SC | fails | the same |
| LR, then `mret` alone, then SC | fails | the same |
| LR, then a timer interrupt, then SC | fails | the same |
| LR on one word, SC on the next | fails | the same |
| LR, a plain store of a *different* value, SC | fails | succeeds |
| LR, a plain store of the same value, SC | succeeds | succeeds |
| `amoswap.w` on the UART's data register | stores the byte | access fault: our console takes byte accesses only |

The store-of-a-different-value row is QEMU's implementation, not the spec. QEMU
implements SC as a compare-and-swap against the value LR read. Our SC follows
the reservation alone, so that case stays out of every test compared with QEMU.

## Devices

An AMO on a device follows the existing device rules. The window sees a read
and then a write, as the RTL's bus presents them.

| The window | Result |
| --- | --- |
| refuses the read, or has no read side (the done register) | access fault 7, with no write |
| refuses the write, or has no write side (the boot ROM, the input queue) | access fault 7, after the read and its side effects |
| takes both (`mtimecmp`, say) | the AMO completes |

A refused write after a read is the same on both backends. For example, an
AMO on the input queue's EVENT register pops an event and then faults. The
emulator reads first, as the bus does, rather than checking the window's
direction before it reads.

An SC.W and an AMO are atomic against the devices that write RAM by
construction:

- **The lock.** While the core presents an SC's store or an AMO's read or write
  (`mem_lock`), the SoC grants the graphics engines no new RAM access, even
  while the host stalls the request.
- **Held grants.** The core does not present such an access while an engine
  holds an earlier grant (`ram_engine_held`).
- **The recheck.** As `MEM` first presents an SC, it checks the reservation
  again. A device write during the SC's walk, after `EXECUTE` decided, makes it
  fail there with no access.

So no device write lands between an LR's reservation and its SC's store, or
between an AMO's read and its write. virtio-blk's DMA runs only while the CPU is
stalled on its notify store. The testbench enforces both on every cycle.

## The trace

An AMO line carries both memory effects, in the order the grammar already had:
the write, then the read.

```
<step> <pc> <word> x<rd>=<old> mem[<a>]<-<new>/4 mem[<a>]-><old>/4
```

A failed SC has no `mem[` field, only `x<rd>=00000001`. A successful SC looks
like `sw` with `x<rd>=00000000` before it, and LR like `lw`. Every line without
an A instruction is byte-identical to before. The emulator keeps a read value
and a write value for the one address. Before an AMO retires, the testbench
admits exactly one read and then, in `AMO_WRITE`, one write. The write must use
the read's bus word (the physical one, when translated) and strobe `1111`, after
a read that was not refused. Any other second data transaction fails the run.

## The RTL

[`rv32_decode.v`](../rtl/rv32/rv32_decode.v) decodes `0x2F`.
- LR folds into `is_load`, and SC and the AMOs into `is_store`, so every
  existing `is_load ? load-cause : store-cause` choice and the walk's kind were
  already right.
- Each also has its own flag (`is_lr`, `is_sc`, `is_amo`).
- The immediate is zero.
- `OP_AMO` is legal only with funct3 2, the same field value as `lw` and `sw`, so
  the width, strobe and load-extension paths treat all of them as words.

The core derives `mem_reads` (`is_load || is_amo`) and `mem_writes`
(`is_store`) from these. PMP's need is `{mem_writes, mem_reads}` and `MEM` writes
only for an access that writes and does not read.

[`rv32.v`](../rtl/rv32/rv32.v) adds:
- **`AMO_WRITE` (state 12).** An AMO reads in `MEM`, then writes its result in
  `AMO_WRITE` to the same address. Translation and PMP are checked once, in
  `EXECUTE` or `XLATE`, and PMP asks for R and W. A refused write is access
  fault 7.
- **The AMO ALU.** Combinational, on `mdr` (the word read) and `b` (`rs2`), and
  muxed into `mem_wdata` in `AMO_WRITE` only. funct5 bit 4 picks min/max, bit 3
  unsigned, bit 2 max; below that, bit 0 is swap and bits 3:2 pick add, xor,
  or, and.
- **`sc_skip`.** In `EXECUTE`, after the alignment check, an SC whose word is
  not reserved goes straight to `WRITEBACK`. `data_translating` is gated by it,
  so a failed SC makes no TLB lookup either.
- **`sc_abort`, `lock_wait`, `mem_lock`.** `MEM` rechecks the reservation before
  first presenting an SC; if a device write ended it since `EXECUTE`, the SC
  fails there with no access. It does not present an SC or AMO while
  `ram_engine_held` is set. While it presents one, `mem_lock` tells the SoC's
  arbiter to grant no engine.
- **The reservation registers** `reserved`, `reservation[29:0]` (virtual) and
  `reservation_pa[29:0]` (physical), plus `sc_held`, the SC's outcome from
  `EXECUTE`. LR sets the reservation in `MEM`, as its read is accepted. These
  clear it:
  - `take_trap` and reset;
  - every SC, `mret`, `sret`, `sfence.vma` and a `satp` write, in `WRITEBACK`;
  - a snooped device write to the physical word, in any cycle.

**Cycles.** The formula is now:

```
cycles = 4 × steps + data accesses + N × transfers + fp_waits + md_waits + ptw_waits
```

This is the old `4 × (non-memory) + 5 × (memory)` term with one access per load
or store, so no existing pin moved. `tools/rv32_rtl.py` counts the `mem[`
fields, not the lines.

| Instruction | Cycles | Transfers |
| --- | --- | --- |
| AMO | 6 + 3N | fetch + read + write |
| LR, successful SC | 5 + 2N | like a load or store |
| failed SC | 4 + N | the fetch only |

An SC that a device write fails in `MEM`, after `EXECUTE` (during its walk), also
costs its walk and the `MEM` cycle with no access. An SC or AMO that waits for a
held engine grant costs those cycles too. Both need a device writing RAM, and the
formula does not cover them.

**Cost.** `make synth-rv32` (Yosys 0.69) puts the `rv32` module at 15,479 cells
and 1,775 flip-flops. On the same Yosys, main before this change (3756518) is
14,012 cells and 1,712 flip-flops: 1,467 cells and 63 flip-flops more. The
flip-flops are exactly the reservation (a valid bit and two 30-bit words),
`sc_held` and `data_waiting`. Most of the cells are the AMO ALU's adder, comparators and mux, and
the two word comparators. The 14,162 recorded for issue #33 came from an
earlier Yosys.

## Tools and the toolchain flag

- **Assembler.** [`tools/rv32_asm.py`](../tools/rv32_asm.py) has `LR_W`,
  `SC_W` and `AMO_OPS`, each with optional `aq` and `rl`.
- **Disassembler.** There is none in the repository: listings come from
  `llvm-objdump`, which already decodes A.
- **Image gate.** [`tools/rv32_image.py`](../tools/rv32_image.py) refuses
  every opcode-`0x2F` word unless `--allow-a` is given and the word is valid
  (`valid_a_word`). `--require-a` also wants at least one A word in the listing.
- **Compiler flags.** `-march=rv32ima` is `RV32A_CFLAGS` / `RV32A_LDFLAGS`, with
  a guard like RV32M's.
- **atomcheck.** [`programs/rv32/atomcheck.c`](../programs/rv32/atomcheck.c)
  uses the `__atomic` builtins. clang lowers them to all nine AMOs and to
  `lr.w`/`sc.w` loops for compare-and-swap, with `.aq`, `.rl` and `.aqrl` but
  never neither; the directed tests cover all four. `check-rv32a-image` requires
  every A mnemonic in its listing. It prints `PASS 40a0b037` on QEMU, the
  emulator, Icarus and Verilator.
- **With F.** `make check-rv32af-flags` compiles atomcheck with
  `-march=rv32imaf_zicsr -mabi=ilp32f`, to show the flags combine. A guard
  checks the substitution, and `llvm-readelf` checks the object's single-float
  ABI.
- **The OS.** Its flags do not change.
- **The device tree.** It still says `rv32imf_zicsr_zicntr`. Adding `a` would
  change the tree's bytes, and with them the platcheck and irqcheck PASS words.
  It is #36's to change, when Linux reads it.

## Tests

All of these are in `make test-rv32`:

| Target | What |
| --- | --- |
| `test-rv32-a`, `test-rv32-a-verilator` | [`tests/test_rv32_a.py`](../tests/test_rv32_a.py), on Icarus and Verilator, trace for trace, in step-tick mode. It covers:<ul><li>every AMO on edge values against a Python reference, with `rd` = `rs2`, `rd` = `rs1` and `rd` = `x0`;</li><li>the cycle formula, stalled and not;</li><li>LR/SC outcomes;</li><li>an `ecall` and an illegal word between LR and SC, the handler returning with a jump, and a bare `mret`;</li><li>the issue's directed test: a timer interrupt between LR and SC;</li><li>a `satp` write and `sfence.vma` between LR and SC;</li><li>a device's write: a virtio-blk read and a G2 depth clear over the reserved word (results only for G2, whose busy time differs between the backends);</li><li>every fault in the tables above, and every one of the 32 funct5 values against `valid_a_word`;</li><li>an AMO on `mtimecmp` and on the input queue;</li><li>Sv32: LR/SC and AMOs that succeed (a TLB miss and a hit, stalled and not, read back through the physical page), an AMO's store page fault on a first touch and on a TLB hit, LR's page fault, misalignment before translation, and an `sret` from S to U ending the reservation;</li><li>PMP, through locked entries: on an R-only word, LR reads while an AMO and a reserved SC fault;</li><li>300 AMOs under timer interrupts on clock time with a stalled bus, every increment landing;</li><li>LR/SC pairs on depth words while G2 clears them, with seeded waits on both RAM ports: no SC succeeds while G2 runs, and the testbench sees no device write inside an atomic access.</li></ul> |
| `test-rv32-arch`, `test-rv32-arch-verilator`, `test-rv32-arch-a-icarus` | riscv-arch-test 3.9.1's `rv32i_m/A`: nine AMO tests, each 140 signature words equal to QEMU's, and traces equal to the emulator's. It has no LR/SC test. |
| `test-rv32-ua`, `test-rv32-ua-icarus` | riscv-tests' `rv32ua` (10 tests run; `amocas_w` and `amocas_d`, which are Zacas, are excluded), `lrsc.S` among them. See below. |
| `test-rv32-arch-model` | Our riscv-tests environment on its own: PASS, FAIL with the case's number (1 before any case), and TRAP, on the emulator and QEMU. Four rows of the QEMU table run there on both (unmapped SC, `mret`, the next word, misaligned SC). The runner's failure paths and both suites' completeness checks run on stubs. |
| `run-rv32-atom-{qemu,emu,rtl,rtl-verilator}` | atomcheck, pinned, trace for trace on both simulators. |
| `check-rv32a-image`, `check-rv32af-flags` | The image gate and the flags. |
| `test-rv32-tools` | The A gate in `rv32_image.py`. |

**rv32ua.** [`tools/rv32_riscv_tests.py`](../tools/rv32_riscv_tests.py) runs it.
- `make fetch-rv32-riscv-tests` checks out riscv-tests at master `bcffa2b3`
  (2026-09-25; the project has no release tags) into `third_party/riscv-tests`,
  which git ignores.
- Only the licence (BSD-3-Clause), `isa/rv32ua`, `isa/rv64ua` (which the rv32
  files include) and the scalar test macros are fetched.
- In place of riscv-tests' environments, a test includes our
  [`tests/riscv-tests/riscv_test.h`](../tests/riscv-tests/riscv_test.h),
  a bare M-mode environment.
- A test must print `PASS` on every backend, and each simulator's trace must
  equal the emulator's.
- The selection is the Makefrag's `rv32ua_sc_tests` less the exclusions. A
  missing test, or an exclusion naming no listed test, stops the run, as a
  short arch suite does (each pins its test count).

## Next

#35 (Doom) does not need A. #36 (Linux without an MMU) does, and will:
- add `a` to the device tree's ISA string;
- decide whether the kernel's own locks move to AMOs.
