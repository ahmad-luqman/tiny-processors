# A2: RV32-commanded SIMD4

The RV32 CPU loads private accelerator memories through its bus, launches the
four-lane A1 engine, polls for completion, and reads the result. There is no DMA,
interrupt, shared-RAM arbitration or coherent cache. The standalone engine's
instruction and arithmetic contract in [simd4.md](simd4.md) is unchanged.

## Device contract

All accesses are aligned 32-bit words and no window is executable. Misalignment
traps before decode; other invalid accesses are load/store access faults without
side effects. Unlisted offsets and directions are invalid.

| Address | Direction | Meaning |
| --- | --- | --- |
| `0x20004000` | write | COMMAND: exactly 1 START, exactly 2 RESET |
| `0x20004004` | read | STATUS: bit 0 BUSY, bit 1 DONE, bit 2 FAULT; other bits zero |
| `0x20004008` | read/write | ENTRY: program word index 0–255; larger writes fault |
| `0x2000400c` | read | CYCLES: busy device ticks, including final halt/fault |
| `0x20004010` | read | STALLS: ticks waiting on the engine's data port; always zero on the emulator |
| `0x20004014` | read | TRANSFERS: accepted engine reads and writes |
| `0x20004018` | read | INSTRUCTIONS: successful engine retirements, including HLT |
| `0x20005000`–`0x200053ff` | read/write | 256 program words, four bytes per word |
| `0x20006000`–`0x200063ff` | read/write | 256 data slots, four bytes per 16-bit word |

Data-slot reads zero-extend; writes discard the upper 16 bits. This CPU slot
addressing does not change the SIMD4 engine's eight-bit word addresses.

START captures ENTRY and clears previous DONE/FAULT, counters, registers,
accumulators, loop and execution state. It is accepted only while idle. While
busy, CPU reads/writes of either memory and writes to ENTRY or START fault;
reads of registers and RESET remain available. A request on the completion edge
still observes busy and is rejected. The next request can access the buffers.
Invalid CPU accesses do not change device status. Engine illegal/invalid-loop
instructions finish with DONE and FAULT; successful HLT sets DONE. Both bits
remain until START or RESET. Counters wrap modulo 2^32.

Global reset and COMMAND RESET clear ENTRY and execution/status/counters. Reset
has priority over any engine transfer on that edge, cancels an unaccepted
request, and never rolls back accepted stores. Neither reset clears program or
data memory. Power-up contents are unspecified; firmware initializes every
location it uses. The harness starts memories at zero to match emulator storage.

Side effects occur only at acceptance: CPU `valid && ready && !error`, engine
`mem_valid && mem_ready`. While stalled, request fields stay stable. Each lane's
store is a separate transfer, so a reset/fault may leave partial output.

## Device time and comparison

RTL advances on each clock. The emulator advances one FETCH/EXECUTE/MEMORY phase
at the end of each executed CPU instruction, including traps, with one lane
transfer per MEMORY tick. A successful START or RESET suppresses advancement
for that CPU instruction. CPU loads observe state before that tick. Emulator
memory has zero waits; RTL test inputs can delay CPU and engine memory
independently. Performance counters measure device ticks, not CPU instructions.

Completion polling produces different CPU traces across backends. Result-level
comparison requires observed timer reads or accelerator register accesses;
console, completion and ordered trap records must still match. Existing
trace-identical firmware keeps strict retirement comparisons.

## Run and inspect

```sh
make test-rv32-simd4                  # C model + Icarus protocol and CPU integration
make test-rv32-simd4-verilator        # Same suite on Verilator
make run-rv32-simd4-emu               # Same linked guest image
make run-rv32-simd4-rtl
make run-rv32-simd4-rtl-verilator
make lint-rv32-soc synth-rv32-soc
make waves-rv32-simd4
```

The diagnostic ends with `vector OK`, `matrix signed/unsigned OK`, `recovery OK`
and `PASS A2`. It checks all 32 vector outputs, all 16 outputs of each matrix,
the known `fffc` high half of the overflowing first dot product, transfer and
instruction counts, a faulting launch, relaunch without reset, rejected driver
calls while busy, and a nonzero polling timeout followed by recovery. `simd4_wait` resets the
device on timeout, preserving accepted stores but clearing ENTRY/status/counters;
a zero poll budget means immediate abort. Counter reads return a boolean and
leave the output unchanged on an invalid index. Matrix
reference products use guest RV32I arithmetic with unsigned accumulation to
avoid C signed-overflow undefined behavior. The upper 16 bits are checked
without implementation-defined signed shifts.

`tools/rv32_simd4_kernels.py` produces a build-only header using the existing A1
builders, including a MACU variant of the signed matrix kernel. The emulator's
C implementation is independent of the Python interpreter. For 248 images
(three guest kernels, 242 illegal bytes, two invalid loops and an arithmetic
sequence), tests compare C retirements, ordered transfers, final buffers and
counters against that interpreter. The resulting fixtures replay every device
edge in RTL, checking MMIO responses, all lane registers/accumulators, PC/loop,
status/counters, every accepted transfer and held requests. The harness also
asserts that the physical data write enable equals an accepted CPU/engine store. Every final data slot is also read back.
Fixtures and logs are under `build/rv32/simd4-tests/{icarus,verilator}/`.

Fourteen test methods per simulator additionally cover CPU decode/access faults,
all forbidden register directions and subword widths, boundary slots/gaps,
entry 255 and PC wrap, rejected launch while busy, the HLT ownership boundary,
software/global reset, partial loads/stores, reset before a live RDA, fault after
stores, relaunch, malformed-fixture rejection, and invalid runner options.
A six-trap CPU program proves busy-access faults go through the real SoC and
matching emulator traps, with work still running. Main diagnostic runs use
unstalled memory, three CPU wait cycles plus two engine wait cycles, and
independent random stall seeds. The runner refuses trace comparison for
observed accelerator-register accesses; result comparison still checks each
backend passed and compares console, checkpoints and ordered trap records. `--compare-stores` adds
ordered store comparison (PC, instruction, address, value and width, excluding
step numbers); A2 run targets opt in. It is not automatic for other accelerator
programs: a poll loop can store a varying iteration count. Timer-reading `diag`
continues to use its original results comparison.

## From a CPU store to gates

1. The guest stores kernel words at PROGRAM and operands at DATA while idle.
   Bus comparators select this peripheral, and word-width/permission logic
   either asserts error or enables exactly one write edge.
2. The ENTRY register is eight flip-flops. An accepted START is a combinational
   pulse into the engine on that edge, not a command bit left set in storage.
   The engine captures entry, clears its lane state, and becomes busy.
3. Busy selects the engine's program/data addresses before the memory arrays.
   There is one read port per array and one write port per array. CPU accesses
   are refused while busy, so no arbiter or second port is necessary. The data
   write mux selects a CPU word's low half or the current lane's store value.
4. Engine memory requests hold address, direction, lane and data through wait
   cycles. Only the accepting edge enables the memory write or fills a lane.
   Software RESET gates off the accepting edge just like global reset.
5. HLT makes the engine idle with sticky DONE. The CPU observes completion,
   then loads result slots. An engine fault follows the same ownership return
   with FAULT set; it never rolls back earlier stores.

Synthesis uses the repository's 64-word RAM/framebuffer configuration but full
256-word accelerator memories. Current SoC totals are in [G1](rv32-gfx.md#from-commands-to-gates). The A2 milestone SoC had **112,374 generic cells and
21,840 flip-flops**, with no latches. The wrapper plus SIMD4 accounts for
53,867 cells and 12,925 flip-flops: 12,288 memory bits, eight ENTRY bits and the
629-flop engine. These are generic flattened-memory costs, not FPGA block-RAM
utilization or a clock-frequency prediction. Ownership muxes select addresses
before the memories, avoiding duplicate read ports; the full protocol suite
verifies both owners through these ports. Reproduce current whole-SoC counts with
`make synth-rv32-soc` (`build/rv32-soc-synth.log` and `build/rv32-soc.json`).

## Measured device work

| Job | Instructions | Transfers | Zero-wait ticks | Two waits per transfer |
| --- | ---: | ---: | ---: | ---: |
| 32-element vector add | 51 | 96 | 198 | 390 |
| 4×4 signed matrix, shift 16 | 77 | 144 | 298 | 586 |
| 4×4 unsigned matrix, shift 16 | 77 | 144 | 298 | 586 |

For successful jobs, `cycles = 2 × instructions + transfers + stalls`.
`measurements.json` in each simulator's test directory records these counters
separately from CPU instructions and whole-machine cycles, including load,
poll and CPU-reference overhead. Random seeds are reproducible within each
simulator; Icarus and Verilator's `$random` streams need not coincide.
There is no claim of a CPU speedup from this small, copy-driven workload.

## Three reset waveform observations

The focused protocol wave is generated by either integration test target at
`build/rv32/simd4-tests/<simulator>/protocol.vcd`. Its edges are deliberately
10 ns apart; these are testbench timestamps, not physical timing claims.

- At **5,144 ns (fixture row 514)**, START transfers ownership. At **5,294 ns (row 529)**, a second START is
  rejected while a store waits. Busy stays high and the transfer count stays
  zero. At **5,334 ns (row 533)**, RESET cancels that store even though memory hold is
  released on the same edge; the destination still reads `4321`.
- At **5,434 ns (row 543)**, lane zero's store of `abcd` is accepted. Lane one's store is
  held, then global reset at **5,454 ns (row 545)** cancels it. Reads show `abcd` only in
  the first output slot and `4321` in the other three: reset is not rollback.
- At **10,884 ns (row 1088)**, the first lane of a LOAD captures `7654`. RESET at
  **10,904 ns (row 1090)** clears all lane registers and counters, preserving memory.
  The subsequent launch completes normally.

Optional exercises on the verified baseline: predict the two-wait counts from
the transfer totals; move reset one edge earlier/later and predict which stores
survive; trace why the CPU slot address `DATA + 4*128` becomes engine word
address `0x80`. Then inspect which costs G1 must address before accelerating
framebuffer operations: this device only accesses its own 256 data words.

## Acceptance record (2026-09-22)

- `make test-rv32` passes end to end, including A2 on both simulators, the native
  window tests, QEMU checks, and the preserved RV32I/F and device/game replays.
  Pong retains 478,797 identical retirement lines; the capstone retains
  2,220,509 identical lines and 68 checkpoints.
- The A2 diagnostic prints the same four lines on all backends. After the review
  fixes, unstalled RTL recorded 36,467 CPU instructions in 154,369 cycles and
  the emulator recorded 37,367 instructions because completion polling observes
  a different clock. CPU totals are measurements, not architectural assertions.
- Counter, ALU, SAP8 and standalone SIMD4 tests pass on both simulators, with
  their lint/synthesis checks. SIMD4 retains its 395 cases and 423 launches.
- Standalone FP32 passes 70,407 vectors and 38 protocol checks on each simulator;
  its lint/synthesis checks also pass. SoC synthesis is latch-free with the
  storage/cell counts above. Both the short protocol wave and whole-machine
  wave are generated and checked.

N1 added block data-slot writers to this driver and two dense kernels to the
replay corpus without changing the device contract or the RTL; see
[the digit record](rv32-digit.md).
DMA, interrupts, coherent caches and framebuffer acceleration remain
outside A2. Shared buffers here mean memories accessible to both owners in turn,
not arbitrary RV32 RAM addresses.

## Claude review follow-up

The expanded suite independently checks all four counters, the emulator's
201-instruction vector launch-to-completion observation, and advancement on a
trapped instruction. Seeded engine stalls must be nonzero and bounded by three
waits per transfer; the busy-fault test is explicitly control-only and makes no
engine-stall claim. Negative controls reject malformed and over-wide fixtures
in Python and in both simulators, including unknown digits on Verilator. Named
register offsets, status masks and commands are pinned across guest C, emulator
C, Python and RTL. The public guest header participates in generic object
dependencies, and A2 run targets honor emulator/simulator overrides.

All three diagnostic stall configurations compare 4,093 ordered stores as well
as the console, traps and outcome. The new timing assertions were checked with
two intentionally broken emulator builds: omitting the trap tick exposes BUSY
in the handler, and doubling the normal tick changes the observed vector
completion distance from 201 to 101 instructions. Fixture guards also pass
under `python -O`. The review rerun passes all 14 integration tests on each
simulator and the full RV32 regression.
