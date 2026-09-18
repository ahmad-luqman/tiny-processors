# Tiny Processors

Learning Verilog by building small CPUs, parallel compute hardware, and eventually graphics. See [the roadmap](PLAN.md).

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

The counter commands above are unchanged. Both labs pass simulation in Icarus and Verilator, RTL lint, and synthesis. The ALU milestone is complete; CPU implementation is the next session's work.

## What to read

1. [counter.v](labs/01-counter/counter.v): the actual circuit.
2. [counter_tb.sv](labs/01-counter/counter_tb.sv): the clock, input stimulus, and expected results.
3. [Lab notes](labs/01-counter/README.md): timing walkthrough and exercises.
4. [How Verilog becomes gates](docs/verilog-to-gates.md): flip-flops, muxes, incrementer logic, and the synthesized counter.
5. [ALU operation and flag contract](labs/02-alu/README.md), then [alu.v](labs/02-alu/alu.v) and [alu_tb.sv](labs/02-alu/alu_tb.sv).
6. [How ALU RTL becomes gates](docs/alu-to-gates.md): combinational logic, carry versus overflow, annotated waveform observations, and exercises.

## Verified local tools

Apple Silicon macOS, Icarus 13.0, Verilator 5.052, Yosys 0.69+post, Apple Clang 21.0.0. These are the tested versions, not enforced minimums.

```sh
brew install icarus-verilog verilator yosys
```

An accepted Xcode license and working command-line compiler are required for the Verilator C++ build. Generated files stay in the ignored `build/` directory.
