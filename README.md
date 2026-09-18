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

The RTL is Verilog-2005. The testbench uses SystemVerilog's `$fatal` so failed checks return a failing process status. No Python dependencies are needed for this lab.

## What to read

1. [counter.v](labs/01-counter/counter.v): the actual circuit.
2. [counter_tb.sv](labs/01-counter/counter_tb.sv): the clock, input stimulus, and expected results.
3. [Lab notes](labs/01-counter/README.md): timing walkthrough and exercises.
4. [How Verilog becomes gates](docs/verilog-to-gates.md): flip-flops, muxes, incrementer logic, and the synthesized counter.

## Verified local tools

Apple Silicon macOS, Icarus 13.0, Verilator 5.052, Yosys 0.69+post, Apple Clang 21.0.0. These are the tested versions, not enforced minimums.

```sh
brew install icarus-verilog verilator yosys
```

An accepted Xcode license and working command-line compiler are required for the Verilator C++ build. Generated files stay in the ignored `build/` directory.
