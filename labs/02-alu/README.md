# Lab 02: an eight-bit combinational ALU

An arithmetic logic unit selects a function of two operands. This lab has eight-bit inputs `a` and `b`, a three-bit `op`, an eight-bit `result`, and four one-bit flags. There is no clock, reset, enable, carry-in, or stored state. Outputs describe the current inputs after the logic settles.

## Operation contract

Arithmetic wraps modulo 256. The same eight bits can represent unsigned 0–255 or two's-complement −128–127; the flags let us interpret the result either way.

| `op` | Operation | Result | Carry (`C`) | Overflow (`V`) |
| --- | --- | --- | --- | --- |
| `000` (0) | ADD | `a + b` | Ninth sum bit | Signed sum outside −128…127 |
| `001` (1) | SUB | `a - b` | 1 if `a >= b` unsigned (**no borrow**) | Signed difference outside −128…127 |
| `010` (2) | AND | `a & b` | 0 | 0 |
| `011` (3) | OR | `a \| b` | 0 | 0 |
| `100` (4) | XOR | `a ^ b` | 0 | 0 |
| `101` (5) | NOT | `~a` | 0 | 0 |
| `110` (6) | SHL | Shift `a` left by one, insert zero | Old `a[7]` | 0 |
| `111` (7) | SHR | Shift `a` right by one, insert zero | Old `a[0]` | 0 |

For **every** operation, `zero` (`Z`) is 1 exactly when `result` is zero, and `negative` (`N`) equals `result[7]`. `N` reports the result's sign bit; it is not an unsigned less-than test. NOT and both shifts ignore `b`. SHR is logical: `8'h80` becomes `8'h40`, not `8'hc0`. Shift overflow is deliberately cleared even if shifting changes the sign bit.

All eight binary opcodes are assigned. The interface contract assumes known 0/1 inputs. An X/Z opcode falls through to the RTL defaults in four-state simulation; this is not an invalid-opcode detector in physical hardware.

Flags are recomputed on every operation. A future CPU must explicitly capture any result or flags it wants to retain across instructions.

## Run it

From the repository root:

```sh
make test-alu            # Icarus: 19 examples + all 524,288 input combinations
make sim-alu             # Full tests, then short example trace in build/alu.vcd
make lint-alu            # Verilator lint of Verilog-2005 RTL
make synth-alu           # Yosys check, gate statistics, and build/alu.json
make test-alu-verilator  # Same tests; build/alu-verilator.vcd
make waves-alu           # Generate Icarus trace and print the Surfer link
```

The original counter commands (`make test`, `make sim`, etc.) retain their behavior. No Python packages are needed.

The testbench checks the result **and all four flags** with case inequality (`!==`), so unexpected X/Z output bits fail too. Its exhaustive arithmetic model uses wide integers, unsigned comparisons, and signed range checks rather than copying the RTL carry/overflow equations. Hand-calculated examples provide a second check of the model. Every `a`, `b`, and `op` combination is exercised, including all values of the ignored `b` operand for unary operations. This exhausts binary inputs, not X/Z combinations, physical delays, or every possible transition history.

Inputs are unspecified before 10 ns. The waveform commands run the full tests without tracing, then run the 19 examples again with `+examples-only +wave=...`, ending at 200 ns. This keeps both simulators' traces small without relying on `$dumpoff` behavior. Read the [gate and waveform walkthrough](../../docs/alu-to-gates.md) for observations and exercises.
