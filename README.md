# Tiny Processors

Learning Verilog by building small CPUs, parallel compute hardware, and an end-to-end computer. The next direction is our own RV32I CPU and native Mac emulator running C, a small OS/runtime, Pong, and Tetris; FP32 hardware and CPU F-extension integration follow Tetris, before programmable 3D; GPU/NPU integration also follows the playable machine. See [the roadmap](PLAN.md) and the [detailed full-stack plan](docs/planning/roadmap.md).

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

## RV32I firmware on a reference runner

The [RV32 machine contract](docs/rv32.md) fixes reset, the address map, the console and done-register protocol, and the ILP32 firmware ABI for our RISC-V computer. A freestanding C self-check with our own startup code, linker script, and multiply/divide runtime compiles with Homebrew Clang 22, links with lld, passes a standard-library ELF checker, and runs on QEMU's `virt` board, whose UART and test device sit at the contract's three addresses.

```sh
make test-rv32          # Tool tests, host runtime tests, image checks, the QEMU run, the emulator checks, the RTL tests, the self-check on both simulators, lint, and synthesis
make check-rv32-image   # Build ELF/listing/bin/hex and verify them against the contract
make run-rv32-qemu      # Run on qemu-system-riscv32; console line and exit status must agree
make disasm-rv32        # Print the annotated listing
make test-rv32-emu      # 30 hand-computed edge tests against our C emulator
make run-rv32-emu       # Run the same image on our emulator with a retirement trace
make diff-rv32-qemu     # Emulator and QEMU must execute the same PC sequence
```

QEMU boots the image with the bare `rv32i` CPU model; all 28 checks pass, the guest prints `PASS 807d9fad`, and QEMU exits with status 0. The 17 tool tests and 6 host runtime tests use only the standard library. Read the [C to instructions to memory walkthrough](docs/c-to-instructions.md). QEMU is a reference runner, not our machine.

## RV32 headless emulator

[tools/rv32emu.c](tools/rv32emu.c) is our own machine: one C file that loads the flattened image, executes RV32I with the contract's trap, alignment, console, and done-register rules, and writes a retirement trace whose format the RTL testbench will reproduce. It runs the self-check to `PASS 807d9fad` in 32,610 instructions and executes exactly the PC sequence QEMU logs; 30 tests with an independent instruction encoder pin the hand-computed edges (signed boundaries, shifts by 31, `x0`, sub-word stores, misaligned and out-of-map traps, `ecall`/`mret`, double faults, every device edge). Untraced it runs about 400 M instructions/s. Read the [design, trace contract, and trace walkthrough](docs/rv32-emulator.md).

## RV32 multicycle RTL CPU

[rtl/rv32/](rtl/rv32/) is the hardware for the machine: a register file, an ALU with a barrel shifter and one subtractor's comparison flags, an immediate decoder, the four trap CSRs, and a five-state controller driving the contract's ready/valid memory port with byte strobes in both directions. It runs all of RV32I plus `csrr*` and `mret`; illegal encodings and faults trap through `mtvec` exactly as the emulator's do, and a double fault halts both backends the same way. The testbench holds the bus to model stalls and prints the emulator's retirement trace, so a Python test diffs the two backends line for line, and the M1 C self-check runs on the core to `PASS 807d9fad` with the emulator's 32,610-line trace.

```sh
make test-rv32-rtl            # 35 tests: emulator vs Icarus, differential, traps, devices, harness, fixed and random stalls
make test-rv32-rtl-verilator  # the same tests on a Verilator build of the testbench
make run-rv32-rtl             # the C self-check on the RTL: PASS 807d9fad, identical trace, cycle count
make run-rv32-rtl-verilator   # the same on Verilator with one stall cycle per request
make lint-rv32                # verilator --Wall on the core
make synth-rv32               # yosys: no latches; 8,175 cells, 1,457 flip-flops
make waves-rv32               # the loop and the M4 program with two stall cycles per request as VCDs
make bench-rv32-rtl           # cycles, stalls, and transfers for the loop at each stall depth
```

The 78-instruction loop produces identical traces on both backends at every stall setting; 336 cycles unstalled, 102 more per stall cycle. The self-check takes 138,495 cycles for 32,610 instructions, 4.25 per instruction, and 40,665 more per stall cycle. Read the [RTL contract and coverage table](docs/rv32-rtl.md) and the [gates, waveform, and cycle walkthrough](docs/rv32-to-gates.md). Every earlier target is unchanged, and `make test-rv32` includes the RTL tests, the self-check on both simulators, lint, and synthesis.

## RV32 machine: bus, devices, and the diagnostic

[rtl/rv32/rv32_soc.v](rtl/rv32/rv32_soc.v) wires the core to a bus decoder (one comparator per window, a one-hot read mux, fetches refused outside RAM) and to the machine's memories and devices: RAM, the console, the done register, a timer, a 16-event input queue with a held-key mask, a display controller, and a 320×240 framebuffer of 8-bit pixels. [tools/rv32emu.c](tools/rv32emu.c) models the same windows with the same fault edges, schedules key events from a script by frame, hashes the framebuffer into a checkpoint at each present, and writes frames as PPM files. A tick is a clock cycle on the RTL and an executed instruction on the emulator, so the [device diagnostic](programs/rv32/diag.c), which exercises every device and reads the timer, is compared at the results level: five identical console lines ending `PASS 8bd87e9a` and identical checkpoints on the emulator, Icarus, and Verilator, with the frame hash and the checksum derived independently in Python. Everything that never reads the timer stays trace-identical.

```sh
make run-rv32-diag-emu        # the diagnostic on the emulator: PASS, checkpoints, build/rv32/frames/*.ppm
make run-rv32-diag-rtl        # the same on Icarus, results compared with the emulator (1,666,415 cycles, about 16 s)
make run-rv32-diag-rtl-verilator  # the same on Verilator with one stall per request
make lint-rv32-soc            # verilator --Wall on the whole machine
make synth-rv32-soc           # yosys on the machine with 64-word memories: 24,141 cells, no latches
```

Read the [SoC record](docs/rv32-soc.md): the decoder as comparators and muxes, each device, the testbench as host, device time in practice, three waveform observations, the synthesis table, and exercises.

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
10. [RV32 machine contract](docs/rv32.md), then [start.S](programs/rv32/start.S), [link.ld](programs/rv32/link.ld), [selfcheck.c](programs/rv32/selfcheck.c), and the [C to instructions walkthrough](docs/c-to-instructions.md).
11. [RV32 emulator](docs/rv32-emulator.md), then [rv32emu.c](tools/rv32emu.c) and [test_rv32_emu.py](tests/test_rv32_emu.py); run `make trace-rv32-emu` and follow the walkthrough in the trace.
12. [RV32 RTL contract](docs/rv32-rtl.md), then [rv32.v](rtl/rv32/rv32.v) with its three submodules, [rv32_tb.sv](tests/rv32_tb.sv), and [test_rv32_rtl.py](tests/test_rv32_rtl.py); run `make waves-rv32` and follow the [gates walkthrough](docs/rv32-to-gates.md) in the waveform.
13. [RV32 SoC record](docs/rv32-soc.md), then [rv32_bus.v](rtl/rv32/rv32_bus.v), the device modules, [diag.c](programs/rv32/diag.c), and [rv32_devices.py](tools/rv32_devices.py); run `make run-rv32-diag-emu` and look at `build/rv32/frames/frame-0002.ppm`, then `make waves-rv32` for `devices.vcd`.

## Verified local tools

Apple Silicon macOS, Icarus 13.0, Verilator 5.052, Yosys 0.69+post, Apple Clang 21.0.0, Homebrew LLVM 22.1.8 (`llvm@22`, keg-only), lld 23.1.1, QEMU 11.1.1, Python 3.14.2. These are the tested versions, not enforced minimums. The RV32 targets find the keg-only LLVM and lld by absolute path; nothing has to be on `PATH`.

```sh
brew install icarus-verilog verilator yosys llvm@22 lld qemu
```

An accepted Xcode license and working command-line compiler are required for the Verilator C++ build. Generated files stay in the ignored `build/` directory.
