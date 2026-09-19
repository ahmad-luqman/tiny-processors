# Tiny Processors

Learning Verilog by building small CPUs, parallel compute hardware, and an end-to-end computer. The next direction is our own RV32I CPU and native Mac emulator running C, a small OS/runtime, Pong, and Tetris; GPU/NPU integration follows the playable machine. See [the roadmap](PLAN.md) and the [detailed full-stack plan](docs/planning/roadmap.md).

## Start here

The first lab is an eight-bit counter. Its reset and enable are sampled on the rising clock edge. Reset wins over enable; otherwise the counter increments when enabled and holds when disabled. The value wraps from 255 to 0.

```sh
make test            # Icarus: self-checking simulation
make sim             # Same tests, plus build/counter.vcd
make lint            # Verilator checks the synthesizable RTL
make synth           # Yosys synthesis, netlist, and log in build/
make test-verilator  # Build and run the same testbench through C++ simulation
make waves           # Generate the trace and show the Surfer link
```

Open `build/counter.vcd` in [Surfer](https://app.surfer-project.org/) and add `counter_tb.dut` signals: `clk`, `reset`, `enable`, `count`. Change `count` to unsigned decimal. `make waves` prints the viewer link; it does not launch an installed app.

The RTL is Verilog-2005. The testbenches use SystemVerilog's `$fatal` so failed checks return a failing process status. No Python dependencies are needed for these labs.

## Next: a combinational ALU

The [second lab](labs/02-alu/README.md) implements an eight-bit ALU with ADD, SUB, AND, OR, XOR, NOT, and one-bit logical shifts. It produces zero, negative, carry, and signed-overflow flags. Unlike the counter, it has no clock or storage. Subtraction carry means **no borrow**; flags always describe the current operation.

```sh
make test-alu            # Icarus: 524,307 checked vectors, including exhaustive inputs
make sim-alu             # Full tests, then a short build/alu.vcd waveform
make lint-alu            # Verilator RTL lint
make synth-alu           # Yosys checks; assert no flip-flops/latches; write netlist
make test-alu-verilator  # Same checks, plus build/alu-verilator.vcd
make waves-alu           # Generate the trace and print the Surfer link
```

The counter commands above are unchanged. Both labs pass simulation in Icarus and Verilator, RTL lint, and synthesis.

## A working SAP8 CPU

[SAP8](docs/sap8.md) combines the ALU with an accumulator, stored flags, program counter, output register, and three-phase controller. Its nine instructions run an addition and a loop that sums 3 + 2 + 1. A small Python assembler supports labels and separate program/data images.

```sh
make test-sap8            # Assembler tests, core regression, and assembled programs
make sim-sap8             # All tests, then addition/loop text traces and VCDs
make lint-sap8            # Verilator RTL lint
make synth-sap8           # Yosys core synthesis/checks; memories are in the testbench
make test-sap8-verilator  # Core/program checks and waveforms in Verilator
make waves-sap8           # Generate traces and print the Surfer link
```

Both simulators pass 493 core instruction checks plus the assembled addition (6 instructions, output 12) and loop (29 instructions, output 6). All 12 assembler tests pass. The assembler uses Python's standard library; no packages or virtual environment are required. Python 3.14.2 was used locally.

Start with the [ISA and memory contract](docs/sap8.md), then follow the [register, control, and waveform walkthrough](docs/sap8-to-gates.md). SAP8 uses combinational memory reads and clocked writes; memory wait states and FPGA memory integration are outside this milestone. SAP8 remains frozen as the smaller teaching CPU; the completed compute prototype is described below.

## Four-lane parallel compute

[SIMD4](docs/simd4.md) has four 16-bit lanes, four registers per lane, a shared PC/decoder, and uniform loops. Its vector-add kernel matches a Python reference at every instruction and memory transfer. One ready/valid memory port serves lanes in order and supports stalls. Launch/done, faults, and reset cancellation are tested.

```sh
make test-simd4            # Python checks and 312 Icarus cases across 1/2/4 lanes
make test-simd4-verilator  # Same cases in Verilator, plus short waveforms
make lint-simd4            # Strict RTL lint for all lane configurations
make synth-simd4           # Four-lane generic synthesis and latch check
make bench-simd4           # Compare cycle counts at fixed vector length
make waves-simd4           # Tests, no-wait/stalled traces, and the Surfer link
```

Both simulators pass 312 cases with 329 completed launches and agree on the benchmarks. For 32 elements, one lane takes 486 cycles and four lanes take 198 cycles with no memory waits: **2.45× speedup**. All still need 96 transfers through the single port. Read the [gate and performance walkthrough](docs/simd4-to-gates.md) to connect lane duplication, stalls, and measured speedup.

The [kernel builder](programs/simd4/vector_add.py), interpreter, and runner use Python's standard library. Generated reports and traces are under `build/simd4/icarus/` and `build/simd4/verilator/`. Every lane is active, so vector length must be divisible by lane count; divergent branches and multiply instructions are not implemented. The vector-add MVP is complete; multiplication and a matrix kernel are deferred until after the first playable computer.

## What to read

1. [counter.v](labs/01-counter/counter.v): the actual circuit.
2. [counter_tb.sv](labs/01-counter/counter_tb.sv): the clock, input stimulus, and expected results.
3. [Lab notes](labs/01-counter/README.md): timing walkthrough and exercises.
4. [How Verilog becomes gates](docs/verilog-to-gates.md): flip-flops, muxes, incrementer logic, and the synthesized counter.
5. [ALU operation and flag contract](labs/02-alu/README.md), then [alu.v](labs/02-alu/alu.v) and [alu_tb.sv](labs/02-alu/alu_tb.sv).
6. [How ALU RTL becomes gates](docs/alu-to-gates.md): combinational logic, carry versus overflow, annotated waveform observations, and exercises.
7. [SAP8 specification and commands](docs/sap8.md), [CPU RTL](rtl/sap8/sap8.v), and [self-checking testbench](tests/sap8_tb.sv).
8. [CPU gate/control notes](docs/sap8-to-gates.md), then [addition](programs/sap8/add.asm) and [sum loop](programs/sap8/sum_loop.asm) assembly.
9. [SIMD4 specification](docs/simd4.md), [RTL](rtl/simd4/simd4.v), [kernel](programs/simd4/vector_add.py), and [gate/performance walkthrough](docs/simd4-to-gates.md).

## Verified local tools

Apple Silicon macOS, Icarus 13.0, Verilator 5.052, Yosys 0.69+post, Apple Clang 21.0.0. These are the tested versions, not enforced minimums.

```sh
brew install icarus-verilog verilator yosys
```

An accepted Xcode license and working command-line compiler are required for the Verilator C++ build. Generated files stay in the ignored `build/` directory.
