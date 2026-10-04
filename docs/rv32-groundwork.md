# Track 0: groundwork

Track 0 of the [next tracks](planning/next-tracks.md#track-0-groundwork) is five
small steps that every later track leans on: the M extension in hardware, the
Zicntr counters, the official RISC-V architectural tests, a CoreMark and
Dhrystone baseline, and a GDB stub in the emulator. The machine becomes
RV32IMF with Zicsr and Zicntr ([contract](rv32.md#behavior-fixed-in-track-0));
RV32I firmware keeps working unchanged, and every earlier image, trace pin and
checkpoint still holds.

## Plan

The steps were taken in dependency order, each verified before the next.

1. **M extension**, emulator and RTL together, with directed differential tests
   and RV32IM builds of the existing firmware. Everything after it assumes it.
2. **Zicntr**, which CoreMark and Dhrystone need to time themselves.
3. **Architectural tests** for I, M and F on the emulator, both simulators and
   a reference, so the first two steps are checked by tests we did not write.
4. **CoreMark and Dhrystone** on the RTL, recorded before any pipelining work
   ([Track 5](planning/next-tracks.md#track-5-hardware-performance)).
5. **GDB stub**, independent of the others, built alongside them.

Done means: all selected architectural suites pass on the emulator, Icarus and
Verilator; RV32IM builds of the firmware reproduce the RV32I results on the
emulator and Verilator (the self-check and the diagnostic on Icarus too); the
benchmarks validate on the emulator and the RTL with a recorded cycle count; and
a stock gdb can debug a guest. The [acceptance record](#acceptance-record-2026-09-29)
has the evidence.

## The M extension

### Contract

The eight instructions share the OP opcode with funct7 = 1 and pick the
operation with funct3: `mul` (low 32 bits of the product), `mulh`, `mulhsu` and
`mulhu` (high 32 bits of the 64-bit product with both operands signed, rs1
signed and rs2 unsigned, and both unsigned), `div`/`divu` (quotient, rounded
toward zero) and `rem`/`remu` (remainder, with the dividend's sign). None of
them traps. Division by zero returns all ones as the quotient and the dividend
as the remainder; the one signed overflow, `INT32_MIN / -1`, returns
`INT32_MIN` and remainder 0. These are exactly the definitions
[`rt/muldiv.c`](../programs/rv32/rt/muldiv.c) already implemented in software,
which is what lets an RV32IM build of the same program produce the same output.

### Hardware: [`rv32_muldiv.v`](../rtl/rv32/rv32_muldiv.v)

One iterative unit serves all eight operations. It runs the two algorithms the
software routines use, one bit per clock:

- **Multiply: shift and add.** A 64-bit shift register holds the high half of
  the product (`hi`) and the multiplier bits not yet used (`lo`). Each cycle the
  multiplicand is added into `hi` when `lo[0]` is set, and the 65-bit
  `{carry, hi, lo}` shifts right by one. After 32 cycles `{hi, lo}` is the
  64-bit product.
- **Divide: restoring division.** The same register holds the partial remainder
  (`hi`) and the dividend bits becoming quotient bits (`lo`). Each cycle
  `{hi, lo}` shifts left by one and the divisor is subtracted from the top 33
  bits; if the difference is non-negative it replaces `hi` and a 1 enters
  `lo[0]`, otherwise `hi` keeps the shifted value and a 0 enters. After 32
  cycles `lo` is the quotient and `hi` the remainder.

As gates: the 64 register bits are 64 flip-flops, each behind a small mux
(load, shift-and-add, shift-and-subtract); one 33-bit adder serves the
multiply step and one 33-bit subtractor the divide step, whose bit 32 is the
borrow that decides "fits" (the shifted remainder is always below twice the
divisor, so 33 bits suffice). A 6-bit down-counter and one `valid` flip-flop
are the whole controller: `start` loads the counter and clears `valid`, and the
last step sets it, so `valid` never shows a stale or reset-time result. The
operands and `funct3` are latched at `start`, so the core need not hold them
stable; a `start` while the unit is stepping would abandon the old operation,
which the core never does and the testbench stops on. Around the loop sit the sign
logic: at `start` each operand's sign is removed by a conditional negator (a
two's-complement negate is an inverter row and an incrementer), chosen by the
operation's signedness, and at the end one more conditional negator puts the
sign back on the product, quotient or remainder. That is how one unsigned
datapath serves signed, mixed and unsigned operations. The two corner cases
need no special path through the loop: with a zero divisor every trial
subtraction "fits", so the quotient fills with ones and the dividend ends up
in `hi`; only the quotient's final sign fix is suppressed for a zero divisor,
so `-7 / 0` is -1 rather than +1. `INT32_MIN / -1` divides the magnitudes
0x80000000 by 1 and negates the result back to 0x80000000.

The core starts the unit from `EXECUTE` with the operands it read in `DECODE`
and waits in a new state, `MD_WAIT`, until `valid`, then latches the result and
retires it in `WRITEBACK` like any ALU result. The state register grew from
three bits to four for it. The latency is fixed: 32 step cycles plus the one
that reads the result, so an M instruction costs 4 + 33 = 37 cycles whatever
its operands, division by zero included. The testbench counts the cycles spent
in `MD_WAIT` as `md_waits` and the cycle formula becomes

`cycles = 4 × non-memory + 5 × memory + stalls + fp_waits + md_waits`,

which `tools/rv32_rtl.py` checks on every trace-mode run. A reset during
`MD_WAIT` cancels the operation: the unit's counter returns to zero and the
instruction never retires.

Why iterative: it is the smallest unit that does the job, it is the algorithm
the course already explained in software, and its cost is one number. The
whole unit synthesizes to about 1,550 generic cells with 108 flip-flops; a
single-cycle unsigned 32×32 multiplier alone is 6,405 cells in the same flow
(`assign p = a * b`), before signed variants or any divider. Early termination (skip leading zero bits) or a radix-4 step
would shorten the wait at the cost of a variable latency, which would break
the simple cycle formula. Both are [exercises](#exercises) and Track 5 material.

### Emulator

[`rv32emu_core.c`](../tools/rv32emu_core.c) computes the eight operations from
their definitions in C, with 64-bit arithmetic for the high halves and explicit
tests for the two corner cases (C leaves both undefined). Traces are unchanged
in form: an M instruction retires like `add` with `x<rd>=<value>`.

### Firmware: RV32I and RV32IM builds

The default images stay RV32I, so every earlier record and pin still applies.
`make firmware-rv32m` builds the self-check, the diagnostic, Pong and the
capstone again with `-march=rv32im` into `build/rv32m/`. The compiler then
emits `mul`/`div`/`rem` for `*`, `/` and `%`, and those images link without
`rt/muldiv.c`; the image checker's `--require-m` enforces both (M instructions
present, none of the routine symbols linked). The self-check is the exception
that proves the rule: it calls `rv32_div` and friends by name to test them, so
it keeps the routines and now also exercises the hardware through its
operators. Each RV32IM image must reproduce the RV32I image's console line and
checkpoints exactly, on the emulator and on Verilator; Icarus, far slower, runs
the self-check and the diagnostic, as the RV32I targets split them:

| Image | RV32I instructions | RV32IM instructions | RV32IM result |
| --- | ---: | ---: | --- |
| selfcheck | 33,226 | 23,847 | `PASS 807d9fad` |
| pong (200 frames) | 634,154 | 397,460 | `PASS 8fef54bc`, 200 checkpoints |
| capstone (scripted session) | 3,566,938 | 1,880,429 | `PASS ea60197e`, 68 checkpoints |

(Instruction counts are the emulator's for this toolchain, clang 18; see the
acceptance record.)

## Zicntr

| CSR | Number | Reads |
| --- | --- | --- |
| `cycle`, `cycleh` | 0xc00, 0xc80 | device ticks since reset, low and high halves |
| `time`, `timeh` | 0xc01, 0xc81 | the same count as `cycle` |
| `instret`, `instreth` | 0xc02, 0xc82 | instructions retired since reset |

The counters are read-only: a write (`csrrw`, or `csrrs`/`csrrc` with a nonzero
rs1 field or immediate) is an illegal instruction, and so is every other
counter number, including the machine-mode `mcycle`/`minstret` and the
`hpmcounter`s. A read of `instret` returns the instructions retired before the
reading one, on every backend; so does a read of `cycle` or `time` on the
emulator, counting executed instructions. On the RTL a `cycle` or `time` read
returns the clock cycles up to the reading instruction's `EXECUTE` state,
which include its own `FETCH` and `DECODE` (below).

What a "cycle" is follows the [device-time contract](rv32.md#device-time): on
the RTL a clock cycle, on the emulator an executed instruction (retired or
trapped, the same count the timer device uses). `time` reads the same counter
as `cycle` because a device tick is the machine's only time base; O1 will
source it from the CLINT's `mtime` instead. `instret` is different in kind: it
counts retirements, which do not depend on timing, so it is the same on every
backend and a program that reads only `instret` is still compared trace for
trace. `tools/rv32_rtl.py` now treats a read of `cycle` or `time` like a timer
read and requires `--compare results`.

On the RTL the core keeps two 64-bit counters. `cycle` increments on every
clock out of reset and `instret` in `WRITEBACK`; the CSR read happens in
`EXECUTE`, so with no stalls instruction *k* after reset reads
`cycle = 4k + 2` (its `FETCH` and `DECODE` came first), and each stalled fetch
adds its stall cycles. [`tests/test_rv32_m.py`](../tests/test_rv32_m.py) pins
these values on both backends. Guests read the 64-bit value with the usual
high/low/high loop ([`bench.c`](../programs/rv32/bench/bench.c)); the image
checker admits counter reads only with `--allow-counters` and never admits a
write. (The checker previously missed counter reads altogether: objdump prints
them as `rdcycle`, which no `csr*` rule matched. It now decides on the word.)

## Architectural compliance

[riscv-arch-test](https://github.com/riscv-non-isa/riscv-arch-test) is the
official architectural test suite. Each test executes one instruction across
its corner cases and stores results into a *signature* region; an
implementation passes when its signature equals the reference model's.

**Selection.** Release 3.9.1 (commit `eb66181d`), `rv32i_m`: the I suite (39
tests), M (8) and F (142), 189 tests; issue #34 added A (9 AMO tests, no LR/SC;
riscv-tests' rv32ua covers those, [A record](rv32-a.md#tests)), 198 in all. `make fetch-rv32-arch-test` checks out
only those, the headers and the licences at the pinned commit into
`third_party/riscv-arch-test` (ignored by git; the whole suite is over 500 MB,
D and Zfh being most of it). Every other suite is excluded with a reason in
[`tools/rv32_arch_test.py`](../tools/rv32_arch_test.py): B, C, D, K, Zacas, Zfh,
Zicond and the rest are not in our ISA; Zifencei is excluded because `fence.i`
is an illegal instruction in our contract; the privilege suite needs
`mstatus`, `mscratch` and the suite's full trap harness, which arrive with O1.

**The model.** A target supplies a small header of `RVMODEL_*` macros;
ours is [`tests/arch/model_test.h`](../tests/arch/model_test.h), with the link
script next to it. Halt prints the signature on the console, one word per line
as eight hex digits, then writes the pass word to the done register, so the
same bytes come out of every backend. Boot installs a trap handler for one
purpose: the F tests enable the FPU with `csrs mstatus, a0` (setting FS), and
our machine has no `mstatus` because its FPU is always on. That one word traps
as illegal; the handler checks it is exactly that word, skips it and returns.
Any other trap fails the test with code 2, because no selected test is meant to
trap. The handler has no scratch CSR, so it swaps t0 and t1 through `mtval` and
`mcause`, which it has finished reading by then.

**References.** Two independent ones:

- **QEMU** (`virt` board, generic RV32 CPU with C and D disabled, `-cpu
  rv32,c=false,d=false`) runs the same ELF: our console and done register
  coincide with its UART and test device, so its console *is* its signature.
  QEMU is its own RISC-V implementation with its own floating-point code.
- **The generator's values.** The I and M sources carry the expected result of
  every case, computed when the tests were generated. With `RVMODEL_ASSERT` the
  model compares each integer result with it and fails the test with code 3 on
  a mismatch. The F sources carry no expected values, so F relies on QEMU.

A test passes when the emulator passes (no failing assert, no unexpected trap),
its signature equals QEMU's, and each RTL simulator's console *and whole
retirement trace* equal the emulator's. The trace comparison is stronger than
the signature: every register write and memory access of every instruction
agrees, not just the values the test chose to store.

The model's own behaviour is tested without the suite in
[`tests/test_rv32_arch.py`](../tests/test_rv32_arch.py): the signature dump on
the emulator and QEMU, the skipped `mstatus` write with t0/t1 preserved, code 2
for any other trap, and code 3 for a wrong asserted value.

Commands: `make test-rv32-arch` (emulator and QEMU, about ten seconds for I and
M and three minutes with F on four cores), `make test-rv32-arch-verilator` (adds
Verilator, three minutes), `make test-rv32-arch-icarus` (adds Icarus; the F
suite retires 19.5 million instructions, so about an hour; `make
test-rv32-arch-a-icarus` runs only A on Icarus, in seconds). `--suite`, `--test
GLOB` and `--keep` narrow a run and keep its traces under `build/rv32/arch/`.
`RV32_ARCH_QEMU_CPU` overrides the QEMU CPU string.

## CoreMark and Dhrystone

Both benchmarks are vendored unmodified ([CoreMark](../third_party/coremark/README.md),
commit `1f483d5b`, Apache 2.0; [Dhrystone](../third_party/dhrystone/README.md) as
packaged in riscv-tests, BSD) and built twice, for RV32I (software multiply and
divide) and RV32IM. The port is ours, in
[`programs/rv32/bench/`](../programs/rv32/bench): CoreMark's `core_portme.h/.c`,
a small `printf` (64-bit decimals without 64-bit division, which the firmware
has no routine for), the string and memory routines the benchmarks and the
compiler's struct copies need, and freestanding stand-ins for the headers
riscv-tests' Dhrystone expects. Two details are worth knowing:

- **The clock.** A CoreMark tick is one `cycle` count. The score does not
  depend on the tick rate: the port prints the measured region as
  `bench: coremark iterations=N cycles=C instret=I` and the runner computes
  CoreMark/MHz as N × 10⁶ / C. `EE_TICKS_PER_SEC` only sets how CoreMark itself
  converts ticks to seconds, for its "Total time" line and its rule that a
  valid run lasts ten seconds. It fixes a nominal 100 kHz clock, so the rule
  asks for one million ticks. At 1 MHz it would ask for ten million, which the
  RV32IM build falls short of on the emulator (a tick is an instruction there:
  about 300,000 per iteration with clang 22, 9.0 million for 30 iterations),
  and which a faster core would miss on the RTL too. At 100 kHz the fewest
  iterations that validate are 4 (3 with the record's clang 18); CoreMark's integer "Iterations/Sec" line
  reads 0 and is not the score. Dhrystone's riscv-tests build already assumes
  `HZ` = 1 MHz.
- **Dhrystone's silence.** riscv-tests defines `debug_printf` as an empty
  function in `dhrystone.c`, which hides the final values Dhrystone prints to
  show it computed correctly. The link passes `--wrap=debug_printf` so the
  calls reach our console instead, without editing the vendored file.
  Dhrystone also times itself with `mcycle`, which we do not have; the
  stand-in `util.h` reads `cycle`, which counts the same clock.

[`tools/rv32_bench.py`](../tools/rv32_bench.py) runs each image on the chosen
backends, without traces (the RV32I CoreMark run is over 100 million cycles).
It requires CoreMark's own "Correct operation validated" and its known CRCs for
the 2K performance run (seedcrc 0xe9f5, list 0xe714, matrix 0x1fd7, state
0x8e3a), every Dhrystone value equal to its "should be" line, every backend's
console identical outside the timing lines, and `instret` identical on every
backend. Only the RTL's cycles are performance figures.

**Baseline (Verilator, no memory stalls, clang 18 `-O2`):**

| Benchmark | ISA | Iterations | Cycles | Instructions | CPI | Cycles per iteration | Score |
| --- | --- | ---: | ---: | ---: | ---: | ---: | --- |
| CoreMark | RV32I | 30 | 110,263,442 | 26,883,426 | 4.10 | 3,675,448 | 0.272 CoreMark/MHz |
| CoreMark | RV32IM | 30 | 56,672,402 | 11,144,946 | 5.09 | 1,889,080 | 0.529 CoreMark/MHz |
| Dhrystone | RV32I | 500 runs | 3,139,275 | 748,575 | 4.19 | 6,279 | 0.091 DMIPS/MHz |
| Dhrystone | RV32IM | 500 runs | 1,943,275 | 443,075 | 4.39 | 3,887 | 0.146 DMIPS/MHz |

CoreMark/MHz is iterations × 10⁶ / cycles; DMIPS/MHz is Dhrystones per
second at 1 MHz divided by 1757, the VAX 11/780's score. The CPI column is the
multicycle core's cost per instruction: 4 cycles for most, 5 with a memory
access, 37 for an M instruction.

The M extension halves CoreMark's cycles (1.95×) although it raises the CPI
from 4.10 to 5.09: the RV32IM build retires 2.41 times fewer instructions,
because each `*`, `/` and `%` that was a call to a software loop is now one
instruction, but each of those costs 37 cycles. Over the whole CoreMark run the
core spends 9,376,917 cycles, 16.5% of the total, waiting in `MD_WAIT`: some
9,500 multiplies and divides per iteration. Dhrystone multiplies and divides less and
gains 1.62×. The instruction counts are identical on the emulator and the RTL,
which the runner requires; the emulator's own "cycles" equal its instruction
counts (plus the few instructions between the two counter reads) and are not a
performance figure.

Where a pipeline would find its cycles: nearly every instruction here costs its
`FETCH`, `DECODE`, `EXECUTE` and `WRITEBACK` states in sequence, so an ideal
five-stage pipeline would approach one cycle per instruction for the RV32I build,
roughly four times this baseline, before hazards; and an M unit that finishes in
a few cycles rather than 33 would recover most of the 16.5%.

These are figures for a simulated machine under our own port, not certified
CoreMark scores (EEMBC's run rules require the reporting of an unmodified run
on real hardware, and our clock is nominal). They are the baseline a pipeline
must beat.

Commands: `make bench-rv32-emu` (emulator only, validation and instret, one
second), `make bench-rv32` (emulator and Verilator, the recorded baseline),
`make test-rv32-bench` (the runner's checks on recorded consoles).
`RV32_COREMARK_ITERATIONS` changes the iteration count (4 at least, above);
the benchmark objects are rebuilt whenever it, the optimisation level or any
other benchmark flag changes.

## GDB stub

`rv32emu --gdb PORT` serves the GDB remote protocol: registers (the integer
and floating files, pc, fcsr and the CSRs including the new counters, described
to gdb by a target description), memory, software breakpoints kept in a table
rather than patched into guest memory, single-step, continue and Ctrl-C.
Debugger memory access reaches RAM and the framebuffer only, never a device
register, so inspecting memory cannot pop the input queue. A guest halt becomes
gdb's process exit with the emulator's status. `make debug-rv32-gdb` starts a
session; the [stub's document](rv32-gdb.md) has the protocol, the design and its
own acceptance record.

## Commands

| Command | What it checks |
| --- | --- |
| `make test-rv32-m`, `make test-rv32-m-verilator` | M and Zicntr on each simulator: hand-computed anchors, 812 vectors (a 512-case edge grid and 300 seeded) against a Python reference, x0 and aliasing, fixed cost, reset mid-divide, counter semantics and illegal writes |
| `make check-rv32m-image`, `run-rv32m-emu`, `run-rv32m-rtl`, `run-rv32m-rtl-verilator` | the RV32IM builds of the four images on the emulator and Verilator, the self-check and the diagnostic on Icarus |
| `make test-rv32-arch-model`, `test-rv32-arch`, `test-rv32-arch-verilator`, `test-rv32-arch-icarus` | the architectural-test model, then the suite on each backend |
| `make bench-rv32-emu`, `bench-rv32`, `test-rv32-bench` | the benchmarks and the runner's checks |
| `make test-rv32-gdb` | the GDB stub |
| `make debug-rv32-gdb` | an interactive session: the emulator waits for gdb on port 3333 |

All of them are prerequisites of `make test-rv32` except `debug-rv32-gdb`, which is interactive, and the two long measurements, `make bench-rv32` and `make test-rv32-arch-icarus`, which are run for a record like the other `bench-*` targets.

## Exercises

1. Run `mulhu` of 0xffffffff by itself through the shift-and-add loop by hand
   for the first four steps. Which bits of `hi` and `lo` are final after step
   *k*, and why does the carry need a 33rd bit only for one cycle?
2. Trace `divu 7, 0` through restoring division. Why does every trial
   subtraction fit, and where does the dividend end up?
3. Make the unit skip leading zero multiplier bits. What does it save on
   CoreMark's `mul`s, and what would you have to change in the testbench's
   cycle formula and in `test_cost_is_fixed_and_independent_of_the_operands`?
4. Read `cycle` twice in a row with `--stall 3` and predict the difference
   before `tests/test_rv32_m.py` tells you. Then read `instret` the same way.
   Which of the two may a trace-mode test compare, and why?
5. In the arch-test model, why can the trap handler use `mtval` and `mcause`
   as scratch registers but not `mepc`? What would break if a test trapped
   inside the handler?
6. The RV32I CoreMark build spends most of its extra instructions in
   `__mulsi3`. Find them in `build/rv32bench/coremark-i.lst` and estimate the
   cycles per software multiply from the table above.

## Acceptance record (2026-09-29)

Run in a Linux container (x86-64, four cores), not on the author's Mac:
Ubuntu clang and lld 18.1.3 through `RV32_LLVM=/usr/bin RV32_LD=/usr/bin/ld.lld`,
Icarus 13.0 and Verilator 5.040 built from their release tags, Yosys 0.33,
QEMU 8.2.2, gdb-multiarch 15.1, Python 3.11. Instruction counts differ from the
Mac records because clang 18 and clang 22 generate different code; results,
PASS words and checkpoints do not.

- **M and Zicntr:** `test-rv32-m` passes on Icarus and on Verilator (9 tests
  at the time): hand-computed anchors, 812 vectors, x0 and aliasing, the fixed 33-cycle
  wait, reset at five points of a divide, `instret` trace-comparable, `cycle`
  reading 4k + 2 (plus stalls) on the RTL and k on the emulator, ten illegal
  counter accesses. The decode sweep, the illegal-encoding and trap tests in
  `test_rv32_rtl.py` pass with `funct7 = 2` as their unused OP word.
- **RV32IM firmware:** the four images pass `check-rv32m-image` (the
  diagnostic, Pong and the capstone with `--require-m`; the self-check, which
  keeps the software routines, with `--allow-m`) and reproduce `PASS 807d9fad`,
  `PASS 8bd87e9a` with its two checkpoints, `PASS 8fef54bc` with 200
  checkpoints and `PASS ea60197e` with 68 checkpoints on the emulator and
  Verilator, and the self-check and diagnostic on Icarus. On Verilator the
  self-check, Pong and the capstone ran with one stall cycle per request,
  traces identical and the cycle formula exact with `md_waits`; the diagnostic,
  which reads the timer, ran unstalled and was compared at the results level.
- **Architectural tests:** `test-rv32-arch-model` 5 tests. All 189 selected
  tests (I 39, M 8, F 142) pass on the emulator against QEMU and on Verilator
  with identical traces, 3 minutes on four cores. On Icarus I and M pass
  (47/47, 2 min 45 s) and F passes 142/142 with identical traces (split over two runs by a container
  restart: 94 tests, then the remaining 48 in 13 min 45 s on three jobs).
- **Benchmarks:** the table above (`make bench-rv32`, 7 minutes); CoreMark
  validates with its known CRCs and Dhrystone's 20 checked values match on the
  emulator and Verilator, with identical instret. `test-rv32-bench` 5 tests.
- **GDB stub:** `test-rv32-gdb` 26 tests at the time (33 after review), including a real gdb-multiarch session
  ([record](rv32-gdb.md#acceptance-record-2026-09-29)).
- **Hardware cost:** `make synth-rv32` is 50,532 generic cells (47,885 before
  Track 0 with the same Yosys), latch-free: the M unit 1,690 cells with 107
  flip-flops, the `rv32` module 4,715 cells against 3,704 (the two 64-bit
  counters and their CSR mux). Cell counts depend on the Yosys version: after
  review, Yosys 0.69 on macOS gives 44,381 in total, the M unit 1,540 cells
  with 108 flip-flops (the registered `valid`) and `rv32` 4,159; per-module
  counts there move by about ±100 cells between runs of unrelated edits. `lint-rv32` and `lint-rv32-soc` are clean.
- **Regression:** `test-rv32-tools` 22, `test-rv32-rt` 6, `test-rv32-emu` 33,
  `test-rv32-f-verilator` 11, SIMD4 14 and G1 8 tests pass; the self-check, Pong,
  the capstone and the three F images are trace-identical on Verilator, and the
  A2 and G1 diagnostics results-identical.

Failures seen here that reproduce identically on the unmodified base commit in
the same container, and so belong to the environment rather than to Track 0:
`test_selfcheck_image_when_built` pins the clang 22 trace length (32,610; clang
18 gives 33,226); `test_results_mode_...` uses BSD `sed -i ""`; on Icarus the
diagnostic and Pong image tests exceed the tests' 120-second timeout;
`run-rv32-diag-rtl-verilator` hits the testbench's default 10-million-cycle
limit after the same 1,995,674 instructions (clang 18's diagnostic runs longer); the host-side tests that build `-shared` libraries
(`test_software_multiply_helper...`, `test-rv32-capstone`) fail to link on
Linux; and `run-rv32-qemu` needs QEMU's `rv32i` CPU model, which QEMU 8.2 lacks.
Issue #20 later fixed three of these: the self-check test derives its counts
from the emulator's trace, the runner test edits with a portable `sed`, and
the diagnostic's RTL targets pass `--max-cycles 40000000`.
The native window (SDL3) and the long S1 runs were not run here.

