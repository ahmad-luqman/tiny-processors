# SAP8: an accumulator CPU

This milestone connects the counter's clocked storage to the ALU's combinational logic. It is our own SAP-inspired teaching ISA, not binary-compatible with a particular SAP machine.

## Implementation plan (completed)

1. Freeze the ISA, flag rules, reset behavior, and memory timing below.
2. Build a three-phase CPU around the existing ALU; verify a hand-encoded addition and directed architectural edge cases in Icarus and Verilator. Lint and synthesize the core, then commit it.
3. Add an assembler with labels and example programs. Check instruction encodings independently, and run an assembled loop against an instruction-level reference model.
4. Save gate/control explanations, inspect trace samples, rerun the existing labs, and commit the completed milestone. Stop before SIMD hardware.

## Machine state and memory

The CPU has an eight-bit program counter (`pc`), eight-bit accumulator (`acc`), four stored flags (`zero`, `negative`, `carry`, `overflow`), an eight-bit output register (`out`), and a 16-bit instruction register (`instruction`). An instruction-address register tracks where the current instruction was fetched. A two-bit state register controls FETCH, DECODE, EXECUTE, and STOP.

Program addresses select **words**, not bytes: 256 words of 16 bits. Data addresses select 256 separate eight-bit bytes. The same numeric address names different locations in these two memories. PC increment and accumulator arithmetic wrap modulo 256.

The synthesizable core exposes memory ports; the simulation testbench supplies the memories. Program and data reads are combinational. Data writes occur on a rising edge with `data_write_enable=1`. There is no ready/valid handshake or support for memory wait states. The read data must settle before the sampling edge. The core alone is synthesized; inferred FPGA block RAM and a board memory wrapper are later work.

## Instruction encoding

Each instruction is `{opcode[7:0], operand[7:0]}`. ADD and SUB read a data-memory operand; they are not immediate instructions.

| Opcode | Assembly | Effect at execute edge | Flags |
| --- | --- | --- | --- |
| `00` | `LDI value` | `acc = value` | Z/N from loaded value; C/V = 0 |
| `01` | `LDA address` | `acc = data[address]` | Z/N from loaded value; C/V = 0 |
| `02` | `STA address` | `data[address] = acc` | Preserve |
| `03` | `ADD address` | `acc = acc + data[address]` | Capture all ALU flags |
| `04` | `SUB address` | `acc = acc - data[address]` | Capture all ALU flags |
| `05` | `JMP address` | `pc = address` | Preserve |
| `06` | `JZ address` | If stored Z is 1, `pc = address` | Preserve |
| `07` | `OUT` | `out = acc` | Preserve |
| `08` | `HLT` | Set halted, enter STOP | Preserve |

OUT and HLT ignore their operand byte; the assembler emits zero. All other opcodes (`09`–`ff`) set `fault` and `halted`, enter STOP, and do not retire or alter accumulator, flags, output, or data memory. PC has already advanced during fetch; the instruction register retains the offending word. There are no interrupts or fault handlers.

Z means the stored result is zero; N is its bit 7. Arithmetic C/V follow the [ALU contract](../labs/02-alu/README.md): subtraction carry means **no borrow**, and V means signed two's-complement overflow. Only JZ consumes a flag in this ISA.

## Clock and reset contract

Reset is synchronous, active high, and has priority over execution. A reset edge sets PC/acc/output/instruction and retirement metadata to zero, Z=1, N=C=V=0, clears halt/fault, and enters FETCH. Program/data memory contents are not reset by the CPU. `data_write_enable` is gated off whenever reset is asserted so a reset edge cannot commit a pending store. Architectural registers otherwise change only on rising edges.

| State before edge | Edge action | State after edge |
| --- | --- | --- |
| FETCH (0) | Capture program word and its PC; increment PC | DECODE (1) |
| DECODE (1) | Give instruction decode and operand path a visible phase | EXECUTE (2) |
| EXECUTE (2) | Commit instruction effects | FETCH (0), or STOP (3) on HLT/fault |
| STOP (3) | Hold architectural state until reset | STOP (3) |

Decode is combinational selection circuitry driven by the instruction register, active throughout the cycle. The separate DECODE state is a teaching choice, not a requirement for these asynchronous memories. Every valid instruction, including HLT, takes three clocks after reset. Branches cost the same whether taken or not; there is no speculative fetch.

`retired` pulses for one cycle following each valid execute edge. `retire_pc` and `retire_instruction` identify that instruction; accumulator, flags, output, and PC then show its committed state. On the next edge `retired` clears, even when halted. Faulting instructions do not retire. Testbench checks sample 1 ns after edges, after nonblocking assignments settle.

## Run the core

```sh
make test-sap8            # Icarus architectural and phase checks
make sim-sap8             # Tests, then addition trace and build/sap8.vcd
make test-sap8-verilator  # Same tests and build/sap8-verilator.vcd
make lint-sap8            # Verilator lint of the synthesizable core
make synth-sap8           # Yosys checks, statistics, and build/sap8.json
make waves-sap8           # Generate trace and print the Surfer link
```

The original counter and ALU targets are unchanged. `tests/sap8_tb.sv` supplies memory arrays and a separate instruction interpreter. It compares PC, accumulator, output, all flags, halt/fault, and **every data-memory byte** during each phase and after each executed instruction. It also checks store strobes, retirement metadata, three-clock timing, synchronous reset, reset cancellation of stores, and stable stopped state. Arithmetic expectations use wide integers and signed range checks, independently of the ALU expressions.

The core regression passes 493 instruction checks (including 248 faulting executions covering all 247 illegal opcodes), across 275 reset scenarios in both simulators. It includes hand-calculated arithmetic boundaries, address wraparound, taken/untaken JZ, reset in each active phase, preservation of nonzero state on faults, recovery from halt/fault, separate memory addressing, and twelve reproducible mixed programs. The small addition waveform shows six instructions and outputs decimal 12. These tests establish functional simulation behavior, not a physical clock-frequency limit.

## Assemble and run programs

The assembler uses only Python's standard library. `make test-sap8` runs its unit tests, assembles both examples, runs the core regression, then checks both assembled programs against the instruction interpreter. `make test-sap8-verilator` executes the same core and program tests in Verilator.

```sh
make test-sap8-assembler  # Encoding, label, bounds, error, and CLI tests
make programs-sap8       # Generate both program/data image pairs under build/sap8/
make sim-sap8            # All Icarus checks, plus addition and loop traces
```

Read [add.asm](../programs/sap8/add.asm) first: its emitted words match the hand-encoded smoke test. Then read [sum_loop.asm](../programs/sap8/sum_loop.asm). It sums 3 + 2 + 1 in 29 instructions (87 clocks after reset), outputs 6, and leaves `data[f1]=0`, `data[f2]=6`.

Source syntax is intentionally small:

- One instruction per line, with whitespace-separated operands; `;` starts a comment.
- Mnemonics and `.data` are case-insensitive. Labels are case-sensitive identifiers such as `loop:` and resolve to program **word** addresses, with forward references allowed.
- Operands are bytes written in decimal, hexadecimal (`0xff`), or binary (`0b1010`), or program labels. Data addresses in these examples are numeric literals.
- `.data address value` initializes a numeric data-memory address without consuming a program word. Duplicate initializations are errors. Labels cannot be attached directly to `.data` directives.
- OUT/HLT take no assembly operand. Missing operands, extra operands, unknown instructions/labels, duplicate labels, out-of-range bytes, and programs longer than 256 words fail with a line number.
- Unused program words contain `0800` (HLT); unspecified data bytes are zero. Each image has 256 hex lines for `$readmemh`.

To assemble your own source:

```sh
python3 tools/sap8_asm.py programs/sap8/sum_loop.asm \
  --program build/my-program.hex --data build/my-data.hex
vvp build/sap8.vvp +program=build/my-program.hex +data=build/my-data.hex \
  +expected=6 +instructions=29 +memory-address=241 +memory-value=0 +trace
```

Run `make test-sap8` first to build the simulator. Program mode requires expected output and instruction count, stops after at most 256 instructions, and optionally checks a final memory byte. Every instruction still undergoes full architectural and memory comparisons. `+wave=build/my-program.vcd` adds a waveform.

`make sim-sap8` saves `build/sap8.trace` and `build/sap8-loop.trace` alongside `build/sap8.vcd` and `build/sap8-loop.vcd`. Verilator produces corresponding `*-verilator.vcd` files. `make waves-sap8` prints the Surfer link; it does not launch a viewer.

## Milestone result

Completed on 2026-09-19: both simulators pass the core regression and the two assembled programs; all 12 assembler tests pass; Verilator RTL lint and Yosys synthesis/checks pass with no latches. Counter and ALU regressions still pass. The [gate/control walkthrough](sap8-to-gates.md) explains the synthesized registers and muxes, annotates both waveforms, and provides exercises. The SAP8 ISA is frozen for this milestone; SIMD work remains next.
