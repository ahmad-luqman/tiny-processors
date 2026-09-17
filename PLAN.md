# Tiny Processors: proposed learning plan

Research date: 2026-09-17. Status: first counter lab implemented and verified; later milestones remain proposed.

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

Local verification: Apple Silicon (`arm64`), macOS 26.6.2; Icarus 13.0, Verilator 5.052, Yosys 0.69+post, and Apple clang 21.0.0. The user installed the HDL tools and accepted the Xcode license. The counter lab passes Icarus and compiled Verilator simulation (264 checked edges each), Verilator RTL lint, and Yosys synthesis/checks. Both simulators generate VCD waveforms. Surfer and cocotb setup remain future work.

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
| 0. RTL refresher | Counter and ALU; introduce registers/FSMs while building the CPU | Self-checking tests pass; explain reset, overflow, and clocked updates from a waveform | 1–2 |
| 1. `sap8` | 8-bit accumulator CPU, 8-bit address space, fixed 16-bit instructions, separate program/data memories, multicycle control | Arithmetic and a loop run correctly; trace shows fetch/decode/execute and memory effects | 2–3 |
| 2. `simd4` prototype | Four 16-bit integer lanes, one shared PC/decode unit, per-lane registers, lane ID, load/store, launch/done interface | Vector addition matches Python; stalls and cycle counts are reported; matrix multiply follows | 3–5 for vector-add MVP |
| 3. `rv32-multi` | 32-bit registers, RISC-V instruction decoding, multicycle control, explicit memory handshake; grow from a documented subset toward RV32I | Supported instructions match a reference model at retirement; programs survive memory wait states | 8–12 for broader ISA coverage |
| 4. CPU + accelerator | Memory-mapped command/status registers and a simple ownership protocol for shared memory | CPU launches work, observes completion, and checks results | 3–5 |

### Stage 0: recover hardware thinking

Cover blocking versus nonblocking assignment, concurrent processes, combinational defaults, latch inference, widths, signedness, reset, and simulation scheduling. Begin with a small HDL testbench to understand clocks and stimulus; add cocotb once that is clear. Inspect unknown/uninitialized behavior with Icarus as well as running Verilator lint.

First session outcome: an 8-bit counter with reset/enable/wraparound tests and a waveform we can explain, followed by an ALU exercise.

### Stage 1: connect to the breadboard experience

Proposed instructions: `LDI`, `LDA`, `STA`, `ADD`, `SUB`, `JMP`, `JZ`, `OUT`, `HLT`. Define instruction encoding, flag updates, wraparound, and invalid-opcode behavior before implementation. Use an output register observable in simulation. Start with a hand-encoded addition, then a tiny assembler with labels for loops. Freeze this CPU after the refresher milestone.

### Stage 2: understand parallel compute

Start with straight-line kernels and uniform loops; explicitly disallow divergent per-lane branches. Document integer overflow and multiplication result width. Begin with serialized memory access, then vary lane count and memory bandwidth to see why four lanes do not automatically produce a fourfold speedup.

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
