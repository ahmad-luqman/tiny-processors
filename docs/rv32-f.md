# F2: RV32F architectural contract

F2 integrates the F1 unit into the existing machine. The ISA is RV32IF with
Zicsr and the existing four trap CSRs/MRET, not a full privileged platform.
F is always enabled; FS, context switching, D, compressed instructions and
accelerators are outside this milestone. Existing RV32I firmware remains valid.

## State and instructions

There are 32 independent 32-bit floating registers; **f0 is writable**.
Reset clears all floating registers and fcsr. FLW/FSW use integer base addresses,
signed 12-bit offsets, and the existing word alignment/access/device rules.
Loads, stores, FMV.X.W, FMV.W.X and FSGNJ/N/X preserve NaN payload bits.
FCLASS writes the ten standard classification bits without setting flags.

Arithmetic includes FADD/FSUB/FMUL/FDIV/FSQRT.S, FMADD/FMSUB/FNMSUB/FNMADD.S,
FMIN/FMAX.S, FEQ/FLT/FLE.S and FCVT.W[U].S/FCVT.S.W[U]. The F1 numeric
contract applies, including canonical arithmetic NaNs, subnormals, tininess
after rounding, one-rounding FMA and saturated invalid integer conversions.

CSR addresses 0x001, 0x002, 0x003 alias fflags[4:0], frm[2:0], fcsr[7:0].
Other bits read zero and ignore writes. All six Zicsr forms operate atomically;
CSRRW[I] with rd=x0 suppresses reads; CSRRS/RC with rs1=x0 and immediate
set/clear with uimm=0 suppress writes. Nonzero register indices containing zero
still perform writes. Existing trap CSR semantics remain unchanged.

An instruction with an rm field accepts modes 0..4, or 7 resolving frm.
Reserved static modes 5/6 or resolved dynamic modes 5..7 trap as illegal
instructions before issuing work. Writing a reserved frm is legal. Instructions
without an rm field ignore frm, and use RNE for the standalone FPU interface.
Accrued IEEE flags are ORed into fflags at retirement, even when an integer
destination is x0. IEEE exceptions do not trap. Illegal instructions and failed
loads commit no floating result or flags; existing traps report the fault.

## Execution and observation

The CPU has one instruction and one FPU request outstanding. Operands and
resolved rounding are held through acceptance; the CPU waits for the response,
then commits the result and flags in WRITEBACK. Reset cancels both units and
prevents stale completion. No younger instruction executes during this wait.

The retirement port adds floating write enable/index/value, floating CSR write
enable and the resulting fcsr byte. Trace effects are ordered integer write,
floating write (` fN=XXXXXXXX`), floating CSR update (` fcsr=XX`), memory effect.
A floating CSR write is recorded even if the value is unchanged; arithmetic
records an update when its per-operation flags are nonzero. Integer-only trace
lines remain byte-identical. State dumps include f0..f31 and fcsr.

Float firmware uses `-march=rv32if_zicsr -mabi=ilp32` with separate build
objects. Arguments/results cross C call boundaries in integer ABI registers;
the compiler moves them into floating registers for arithmetic. No incompatible
hard-float ABI objects or host libraries are linked into guest firmware. The
separate `floatsoft` benchmark compiles the vendored generic SoftFloat C sources
as RV32I guest code, with four compiler libcalls and a 32-bit-limb multiply helper.
This benchmark never participates in hardware execution or the emulator adapter.

The emulator uses pinned Berkeley SoftFloat as host arithmetic, with independent
ISA decode and per-machine accrued state. It sets rounding, tininess and clears
temporary flags for every operation. Its synchronous API is single-threaded.
Literal expected results and F1's separate oracle protocol anchor differential
tests. The protocol runner and emulator share the same `rv32_fp()` adapter;
this is one software arithmetic implementation, not two independent oracles.

References: [F 2.2](https://docs.riscv.org/reference/isa/v20260120/unpriv/f-st-ext.html),
[Zicsr 2.0](https://docs.riscv.org/reference/isa/v20260120/unpriv/zicsr.html),
[F1 numeric/handshake contract](fp32.md).


## Commands and coverage

Prerequisites are the existing LLVM 22/LLD, host C compiler, Icarus, Verilator,
Yosys and QEMU tools. Sources for software arithmetic are already vendored.
`make test` remains the counter; `make test-rv32` includes F2 acceptance.

```sh
make test-rv32-f test-rv32-f-verilator # architectural differential, both simulators
make test-rv32-f-tools               # ISA/ABI checks, changed inputs, multiply glue
make run-rv32-f-emu                  # both C float programs and software benchmark
make bench-rv32-f                   # same images, full traces and RTL cycles
make run-rv32-f-soft-qemu            # independent RV32I software benchmark run
make waves-rv32-f                   # short arithmetic and reset VCDs
make lint-rv32 lint-rv32-soc synth-rv32 synth-rv32-soc
```

| Instruction/state group | Verification |
| --- | --- |
| FLW, FSW | Raw signed-zero/NaN bits, negative offsets, word stalls, alignment/unmapped/read-only faults |
| FADD/FSUB/FMUL/FDIV/FSQRT.S | F1 literal result/flag anchors and seeded independent oracle requests at CPU retirement |
| FMADD/FMSUB/FNMSUB/FNMADD.S | Fused cancellation anchors, signed variants, all source/destination registers and aliasing |
| FCVT.W.S, FCVT.WU.S, FCVT.S.W, FCVT.S.WU | Boundary/tie/invalid anchors, all rounding modes, compiled C conversions |
| FMIN/FMAX.S, FEQ/FLT/FLE.S | Zero/NaN rules, flags with integer destination x0, independence from frm |
| FMV.X.W, FMV.W.X, FSGNJ/N/X.S, FCLASS.S | Literal payload/sign checks, all ten classifications, no flag changes |
| fflags, frm, fcsr | All six Zicsr forms, masks, aliases, suppression, accrued flags and clearing |
| Illegal/trap/reset | Reserved format/rm/rs2/function fields, trap handler and MRET preserving FP state, canceled issue/wait/response/writeback |

The seeded CPU corpus has 1,080 requests: all 18 standalone operation mappings,
five modes, and 12 edge/random operand triples per combination, seed 20260922.
Static and dynamic literal anchors run separately. Known instruction encodings
and the C/RTL operation enums are statically pinned. The 1/3 division test pins
34 FPU issue/wait cycles, and compiled-image runners pin 5,081 / 89 / 0 waits
for arithmetic / conversion / software, so latency drift cannot hide inside
the accounting identity. The F1 suite retains its
much larger arithmetic corpus; CPU tests concentrate on state and integration.
The testbench asserts one issue per completion and one completion per arithmetic
retirement, resetting both tokens when work is canceled. Every failing trace,
nonzero backend status, missing completion record and malformed halt record
remains a test failure.

The architectural trace is an effects interface, not a dump of all registers
per instruction. `retire_fd_we` and `retire_fcsr_we` are meaningful with
`retire`. `retire_fd[4:0]` and `retire_fd_value[31:0]` additionally require
`retire_fd_we`; `retire_fcsr[7:0]` additionally requires `retire_fcsr_we`. The
SoC passes them through to the testbench. `fp_waits` in the testbench halt record
counts cycles spent in FP_ISSUE/FP_WAIT and is omitted when zero, preserving
integer halt records. For trap-free runs:

`cycles = 4 × non-memory retirements + 5 × memory retirements + memory stalls + fp_waits`.

The counters describe RTL cycles, not emulator speed. Traps and reset replays
remain outside this whole-program identity, as in earlier milestones.

## C calls, software comparison, and gates

`float_work(a,b,c)` computes `((a*b)+c)/b-a`. It is a separate translation unit
with runtime volatile inputs, `-ffp-contract=off`, and no fast-math. Under ILP32,
a0/a1/a2 carry the float bits into the call. FMV.W.X transfers them into fa3/fa4/fa5;
FMUL.S, FADD.S, FDIV.S and FSUB.S execute through the FPU. FMV.X.W returns the
result bits in a0. The caller stores and checks them as integers. Input changes
are tested by patching the image's data: the self-check must then fail.

The software variant compiles the same kernel as RV32I. Calls to __mulsf3,
__addsf3, __divsf3 and __subsf3 execute the pinned generic SoftFloat routines
**on the guest CPU**. The small ABI adapter also supplies a 64-bit multiply
using 32-bit limbs; 1,036 host extreme/random products check it, and QEMU runs
the complete software guest. Host SoftFloat objects never enter these images.
This is a correctness-oriented software baseline, not an optimized libgcc
performance claim. Including unused vendor functions increases its image size;
we make no code-size optimization claim.

| Whole firmware | Retirements | RTL cycles, no stalls | RTL cycles, one stall/transfer | Result |
| --- | ---: | ---: | ---: | --- |
| F arithmetic (`floatcheck`) | 223 | 6,037 | 6,324 | PASS c0800000 |
| Same C / RV32I SoftFloat (`floatsoft`) | 20,146 | 80,924 | 101,410 | PASS c0800000 |
| C conversion/flags (`floatconvert`) | 178 | 859 | 1,095 | PASS 4f800003 |

These totals include startup, self-checks and console output; they are not
isolated arithmetic throughput. The unstalled whole arithmetic run is about
13.4× faster by RTL cycle count with F. Emulator wall time is not a hardware cycle
measurement. Every image is compared trace for trace on emulator/Icarus/Verilator.

The new floating register file contributes 1,024 explicit storage bits and
three read muxes; f0 has no write-discard gate. Captured operands retain three
32-bit values while the FPU runs. The existing three-bit CPU state register now
uses codes 6 and 7 for issue and wait. Classification uses zero/all-ones
exponent detectors plus a fraction-zero detector. Sign injection is a sign-bit
mux/XOR, not an arithmetic FPU request. FCSR accrual is five OR gates with
retirement-controlled enables. The F1 arithmetic datapath is unchanged.

Synthesis uses the existing Yosys generic-cell flow, with 64-word RAM and
framebuffer instances for the SoC storage demonstration (not the full simulated
memory sizes). Strict lint, `check -assert` and no-latch assertions pass.

| Synthesized unit | Generic cells | Flip-flops |
| --- | ---: | ---: |
| F1 standalone FPU, unchanged | 25,685 | 1,530 |
| F2 CPU including FPU | 42,034 | 4,162 |
| F2 SoC with small memories | 58,062 | 8,915 |

The pre-F2 CPU was 8,175 cells/1,457 flip-flops. Module-context optimization
means the integrated FPU's mapped count can differ from standalone synthesis;
do not add standalone counts and treat the sum as the measured SoC. These are
logical generic cells, not silicon area, clock timing or FPGA resource claims.

## Inspected waveforms

`make waves-rv32-f` writes `build/rv32/f-waves/arithmetic.vcd` and `reset.vcd`.
Signals are under `rv32_tb.dut.core`, including `state`, `fcsr`, `retire`,
`retire_fd_we/value` and `fpu.req_valid/ready`, `fpu.resp_valid/ready`.
Payload ports have meaning only with their enables, so unrelated changes on
`retire_fd_value` are not floating writes.

- Arithmetic: frm becomes RUP (`fcsr=60`) at 295 ns. The request for 1/3 is
  presented at 325 ns and accepted at 335 ns. Response valid rises at 655 ns;
  the CPU consumes it at 665 ns and retires at 675 ns, writing f0=3eaaaaab and
  fcsr=61 together. The 91-cycle run has 14 retirements.
- Reset: the first request is accepted at 335 ns. Reset asserts at 417 ns
  during execution; fcsr clears at 425 ns. No first-request result retires.
  After restart, the second request is accepted at 755 ns and retires at
  1,095 ns with the same f0/flags. The 131-cycle run has 21 retirements, seven
  before reset and fourteen after it.

## Exercises

1. Follow a0 through FMV.W.X, the held FPU operand, FMV.X.W and the C return.
   Which register file owns each value, and why is writing f0 observable?
2. Predict the result and NX flag for 1/3 under RDN and RUP. Change frm in the
   short wave program and find the one-bit result difference at retirement.
3. Compare a fused cancellation anchor against separate multiply/add operations.
   Identify the information lost at the intermediate rounding boundary.
4. Reset just after response acceptance but before WRITEBACK. Explain why neither
   the destination register nor accrued flags may survive that cancellation.
5. Derive the 6,037-cycle arithmetic total from 223 retirements, 64 memory
   operations and 5,081 FPU issue/wait cycles; add one stall per 287 transfers.


## Acceptance record (2026-09-22)

- Eleven architectural tests pass on each simulator: 49 literal anchors in both
  static and dynamic modes, the 1,080-request seeded corpus, all register roles,
  traps, memory faults and reset positions across short/iterative operations.
- Eight F2 tool tests pass, including explicit ISA/ILP32 checks, disassembly,
  patched runtime inputs, complete floating state dumps and multiply glue.
- All three compiled images pass on emulator and both RTL simulators. The
  software image independently passes QEMU RV32I. Short VCDs were inspected.
- `make test-rv32` passes: 39 existing RTL tests on each simulator, 33 emulator
  tests, firmware/device/window tests and both games' scripted acceptance. The
  capstone retains 2,220,509 identical trace lines and 68 checkpoints, ending
  `PASS ea60197e` on both RTL backends.
- F1 retains 70,407 exact result/flag/error vectors on each simulator, 38
  protocol checks, its 15 tooling tests and standalone synthesis counts.
  Counter, ALU, SAP8 and SIMD4 tests pass on both simulators.

Counts describe this acceptance run, not a formal ISA compliance certification.
F2 completes the roadmap's F instruction/state contract within this machine's
minimal trap environment. A1 is next; FPU datapath optimization remains separate.
