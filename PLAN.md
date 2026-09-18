# Tiny Processors: proposed learning plan

Research date: 2026-09-17. Implementation status updated 2026-09-19: counter, eight-bit combinational ALU, SAP8 CPU, and SIMD4 vector-add prototype implemented and verified. The next compute item is multiplication and a small matrix kernel; RISC-V and graphics remain later work.

## Direction

Build and understand a small processor at each step: Verilog refresher → SAP-inspired CPU → parallel compute prototype → multicycle RISC-V CPU → graphics. Pipelining and FPGA deployment are optional follow-on tracks.

The starting point is prior logic-design, computer-architecture, and breadboard SAP experience. Focus on recovering RTL fluency and learning verification, rather than repeating a full introductory digital-logic course.

Confirmed preferences: a quick refresher and working prototype first; both parallel compute and graphics, starting with compute. Proposed approach: simulation first, with a working CPU and compute engine before deepening the RISC-V implementation. Each session is roughly 1–2 hours; estimates are planning ranges, not deadlines.

## Research shortlist

| Resource | How we would use it |
| --- | --- |
| [HDLBits](https://hdlbits.01xz.net/wiki/Main_Page) | Selected Verilog exercises for combinational logic, registers, counters, and FSMs. |
| [From Blinker to RISC-V / FemtoRV](https://github.com/BrunoLevy/learn-fpga/blob/master/FemtoRV/TUTORIALS/FROM_BLINKER_TO_RISCV/README.md) | Main CPU tutorial reference: incremental Verilog design, simulation without a board, assembly and C examples. |
| [PicoRV32](https://github.com/YosysHQ/picorv32) | Later study of a compact configurable core and memory interfaces. Repository marked archived September 8, 2026; useful reference, not an actively developed dependency. |
| [SERV](https://github.com/olofk/serv) | Later comparison of a bit-serial RISC-V design: explore area versus execution time. |
| [Ibex](https://github.com/lowRISC/ibex) | Later SystemVerilog and verification reference; substantially beyond our first CPU. |
| [Project F graphics](https://projectf.io/posts/fpga-graphics/) | Optional graphics path: display timing and drawing in hardware. |

### The two GPU repositories

[adam-maj/tiny-gpu](https://github.com/adam-maj/tiny-gpu) is the stronger conceptual starting point for our purposes. It describes a small compute GPU with 8-bit data, 16-bit instructions, thread-local state, memory arbitration, and matrix-add/multiply examples. Branches assume convergence; cache is marked WIP. Its documented simulation flow uses Icarus, cocotb, and sv2v. This is an educational compute architecture, not a graphics card.

[cp024s/Tiny-GPU](https://github.com/cp024s/Tiny-GPU) is a fork with an assembler, reorganized RTL, and documented Verilator/cocotb regressions. Its README lists SIMD execution, warp scheduling, caches, and FPGA deployment as unimplemented. The [status document](https://github.com/cp024s/Tiny-GPU/blob/master/docs/PROJECT_STATUS.md) describes some warp infrastructure but incomplete architectural scoreboarding. Treat feature claims as project documentation until we inspect RTL and reproduce tests. We have not run either repository.

Use both for comparison. Build our own small modules, then explain differences. Before adapting code, check the license of the exact files and preserve required attribution.

## macOS toolchain

Local verification: Apple Silicon (`arm64`), macOS 26.6.2; Icarus 13.0, Verilator 5.052, Yosys 0.69+post, and Apple clang 21.0.0. The user installed the HDL tools and accepted the Xcode license. The counter lab passes Icarus and compiled Verilator simulation (264 checked edges each), Verilator RTL lint, and Yosys synthesis/checks. The ALU passes both simulators (524,307 checked vectors each: 19 directed plus all 524,288 binary input combinations), RTL lint, and synthesis/checks with an assertion against inferred flip-flops/latches. Both simulators generate VCD waveforms. The user has inspected the counter waveform in Surfer; cocotb setup remains future work.

SAP8 passes both simulators: 493 core instruction checks across 275 reset scenarios, plus assembled addition and sum-loop programs checked against an independent instruction interpreter. Verilator lint and Yosys synthesis/checks pass. The Python standard-library assembler passes 12 tests on Python 3.14.2. No Python packages are required yet.

SIMD4 passes both simulators: 312 cases and 329 completed launches each, with Python reference snapshots, transfer checks, and complete final-memory comparisons. Six Python model/kernel tests pass; lint passes for 1/2/4 lanes; four-lane synthesis/checks pass without latches. Benchmarks and no-wait/stalled waveforms agree between simulators. Counter, ALU, and SAP8 regressions remain passing.

| Layer | Proposed tool | Purpose |
| --- | --- | --- |
| RTL | Verilog-2005 initially | Recover the language using portable synthesizable modules. Introduce a small SystemVerilog subset later when useful. |
| Edit | Existing editor | Syntax highlighting initially; language-server setup can follow. |
| Small simulations | [Icarus Verilog](https://formulae.brew.sh/formula/icarus-verilog) | Compile with `iverilog`, run with `vvp`; simple HDL testbenches. |
| Lint / larger simulations | [Verilator](https://formulae.brew.sh/formula/verilator) | RTL warnings and compiled simulation; useful as designs grow. |
| Automated verification | [cocotb](https://www.cocotb.org/) + pytest in a uv environment | Drive RTL and compare results against Python models. Cocotb requires a simulator. |
| Waveforms | [Surfer](https://surfer-project.org/) | Inspect VCD/FST traces; official site links a macOS ARM build and browser version. |
| Synthesis | [Yosys](https://formulae.brew.sh/formula/yosys) | Check hardware inference and inspect cell statistics. |
| Program toolchain, later | [RISC-V GCC](https://formulae.brew.sh/formula/riscv64-elf-gcc) and binutils | Assemble/link programs and later compile freestanding C. |
| Automation | Make + Git | Short, repeatable commands and milestone history. |

Proposed first installation command, for the implementation session:

```sh
brew install icarus-verilog verilator yosys
```

Use the official Surfer download link instead of assuming a Homebrew cask exists. The standard [GTKWave cask](https://formulae.brew.sh/cask/gtkwave) is currently disabled.

Pin Python dependencies and record simulator versions after a working smoke test. Match cocotb to its [simulator compatibility requirements](https://docs.cocotb.org/en/stable/simulator_support.html); older tutorial tests may need their own environment or API updates. Verify the actual Apple compiler tools by compiling a tiny Verilator simulation.

For RISC-V, explicitly select RV32I/ILP32 and validate the generated ELF/disassembly. Verify RV32 support in the installed compiler and runtime libraries before relying on C; a tool's `riscv64` name alone does not settle that. Provide our own startup code and linker script. An incomplete ISA supports only programs restricted to its implemented instructions.

The flows are separate:

```text
RTL + testbench → simulator → assertions, execution trace, waveforms
Assembly/C → assembler/compiler + linker → memory image → simulated CPU
RTL → synthesis → netlist → board-specific place-and-route → FPGA bitstream
```

Start natively on macOS. Choose FPGA hardware and its supported build/programming tools only when we reach that milestone. Simulation checks behavior; synthesis alone does not establish board timing or maximum frequency.

## Milestones and completion criteria

| Stage | Proposed scope | Complete when | Rough sessions |
| --- | --- | --- | --- |
| 0. RTL refresher (complete) | Counter and ALU; introduce registers/FSMs while building the CPU | Self-checking tests pass; reset, overflow, and clocked updates documented with waveforms | 1–2 |
| 1. `sap8` (complete) | 8-bit accumulator CPU, 8-bit address space, fixed 16-bit instructions, separate program/data memories, multicycle control | Addition and sum loop pass; traces show fetch/decode/execute and memory effects | 2–3 |
| 2a. `simd4` vector MVP (complete) | Four 16-bit integer lanes, shared PC/decode and uniform loops, per-lane registers, lane ID, ready/valid load/store, launch/done | Vector addition matches Python; stalls and cycle counts measured for 1/2/4 lanes | 3–5 for vector-add MVP |
| 2b. SIMD matrix kernel (next) | Define multiplication width/overflow, add multiply, and build a small matrix kernel on the shared-memory interface | Matrix results match Python; memory traffic, stalls, and cycle counts are explained | Plan next session |
| 3. `rv32-multi` | 32-bit registers, RISC-V instruction decoding, multicycle control, explicit memory handshake; grow from a documented subset toward RV32I | Supported instructions match a reference model at retirement; programs survive memory wait states | 8–12 for broader ISA coverage |
| 4. CPU + accelerator | Memory-mapped command/status registers and a simple ownership protocol for shared memory | CPU launches work, observes completion, and checks results | 3–5 |

### Stage 0: recover hardware thinking

Cover blocking versus nonblocking assignment, concurrent processes, combinational defaults, latch inference, widths, signedness, reset, and simulation scheduling. Begin with a small HDL testbench to understand clocks and stimulus; add cocotb once that is clear. Inspect unknown/uninitialized behavior with Icarus as well as running Verilator lint.

Completed: an 8-bit counter with reset/enable/wraparound tests, followed by an [8-bit combinational ALU](labs/02-alu/README.md) with ADD, SUB, AND, OR, XOR, NOT, SHL, and SHR. Zero/sign flags apply to every result; carry means no borrow for subtraction and the discarded bit for shifts; signed overflow applies only to ADD/SUB. Tests exhaust every binary operand/opcode combination. Short directed traces, gate-mapping notes, and exercises are in [docs/alu-to-gates.md](docs/alu-to-gates.md).

The counter commands remain unchanged; the ALU uses `make test-alu`, `make sim-alu`, `make waves-alu`, `make lint-alu`, `make synth-alu`, and `make test-alu-verilator`. Registers, flag capture, and FSM control are now demonstrated in the SAP8 milestone below.

### Stage 1: connect to the breadboard experience

Completed and frozen: `LDI`, `LDA`, `STA`, `ADD`, `SUB`, `JMP`, `JZ`, `OUT`, `HLT`, with an eight-bit opcode and eight-bit operand. Each instruction takes FETCH/DECODE/EXECUTE clocks. Loads update Z/N and clear C/V; arithmetic captures ALU flags; other instructions preserve flags. PC/arithmetic wrap at eight bits. Illegal opcodes halt with a fault and no retirement. Memories have combinational reads and edge-triggered data writes; the core has no wait-state interface.

The hand-encoded addition outputs 12. A label-aware assembler emits separate program/data images; the sum loop outputs 6 after 29 instructions and leaves its counter at zero. Tests compare architectural state and all data-memory bytes at each phase, check all illegal opcodes, PC wrap, reset/store interaction, flag behavior, halt/fault recovery, and reproducible mixed programs. Text and VCD traces are generated for both examples.

Use `make test-sap8`, `make sim-sap8`, `make waves-sap8`, `make lint-sap8`, `make synth-sap8`, and `make test-sap8-verilator`. Read [the specification](docs/sap8.md) and [the gate/control walkthrough](docs/sap8-to-gates.md). The original labs and commands remain intact. Stop at this completed scalar CPU milestone; do not extend its ISA while starting the next project.

### Stage 2: understand parallel compute

Completed vector MVP: four 16-bit lanes with four registers each, one PC/decoder, lane IDs, a shared loop counter, and an external ready/valid data port. Loads and stores serialize lanes 0 through 3, holding requests stable under backpressure. Launch clears lane state/counters; done/fault are sticky; reset cancels pending requests and preserves completed stores. Addition wraps modulo 65536, and program/data addresses wrap modulo 256. Every lane remains active; no divergent branches or tail masks.

The vector kernel covers up to 64 elements in uniform groups and matches both an instruction interpreter and direct Python array addition. Tests cover arithmetic/address/PC wrap, register aliases, store collisions, all illegal opcodes, invalid loops, relaunch, starts while busy, partial-load/store reset, backpressure, and deterministic mixed programs. The benchmark holds length at 32: 486/294/198 cycles for 1/2/4 lanes without stalls, each making 96 memory transfers. Three extra waits per transfer change those totals to 774/582/486, showing the bandwidth limit.

Use `make test-simd4`, `make test-simd4-verilator`, `make lint-simd4`, `make synth-simd4`, `make bench-simd4`, and `make waves-simd4`. See [the contract](docs/simd4.md), [kernel](programs/simd4/vector_add.py), and [gate/performance notes](docs/simd4-to-gates.md). All prior commands are preserved. Pause at the completed vector-add MVP.

Next: define multiplication result width and overflow/truncation before adding it, then implement a small matrix kernel and compare with Python. Keep the shared-memory bottleneck visible in the measurements. Wider memory ports and active masks can be separate experiments after that.

This stage builds a SIMD engine. A later GPU extension adds batches of logical threads, scheduling, active masks, and divergence/reconvergence. Avoid treating those mechanisms as already present. Graphics follows as a distinct next milestone: generate a framebuffer image in simulation, then build line/triangle rasterization. Physical display output can come later.

### Stage 3: build a useful scalar foundation

Start with a small explicitly documented instruction subset, then fill in integer arithmetic, signed/unsigned comparisons, branches, jumps, and byte/halfword/word accesses. Specify alignment and illegal-instruction behavior. Keep caches, multiply/divide extensions, interrupts, an OS, and pipelining out of the first version.

Expose instruction retirement: PC, instruction, register write, and memory effects. Compare each retired instruction against an independently written Python model. Directed edge cases must include x0 writes, negative immediates, signed comparisons, taken/untaken branches, and memory stalls. Later add architectural tests and formal checks before making compliance claims.

## Subsequent depth after the first working CPU/GPU

- Pipeline the scalar CPU: forwarding, load-use stalls, branch flushes, and CPI measurements against the multicycle baseline.
- Extend compute execution: multiple warps, latency hiding, masks, divergence, and memory coalescing.
- Render images: framebuffer → lines/triangles → rasterization; export images from simulation before adding a physical display.
- Target an FPGA: select a board/toolchain, infer synchronous RAM, handle reset/clocking, constrain timing, and measure resource usage.
- Add formal verification first to small blocks, then processor invariants.

## Make the repository a learning resource

For each milestone keep a short specification, datapath sketch, small runnable program, self-checking test, annotated trace, and a few exercises. Work in increments: explain the circuit → predict behavior → implement a small piece → test → inspect the mismatch → explain the fix.

Proposed directories, created only as needed:

```text
labs/          # small RTL exercises
rtl/           # sap8, rv32, simd, shared blocks
tests/         # HDL/cocotb tests and independent models
programs/      # assembly and later freestanding C
tools/         # assembler and trace utilities
docs/          # specifications and learning notes
build/         # ignored generated outputs
```

Proposed commands: `make test`, `make lint`, `make sim`, `make waves`, `make synth`. A trace viewer can follow once the trace format is useful; initial text traces and Surfer are sufficient.

Decision still open: whether FPGA deployment should be an eventual requirement. This does not block the simulation-first prototypes.
