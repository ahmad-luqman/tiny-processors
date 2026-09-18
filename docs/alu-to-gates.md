# From ALU expressions to combinational gates

Read the [operation and flag contract](../labs/02-alu/README.md) alongside [alu.v](../labs/02-alu/alu.v). Work through one step at a time; each ends with something to predict or inspect.

## 1. Remove the clock: what remains?

The counter contains flip-flops. Its current count survives between rising edges. The ALU contains only combinational logic: its outputs are functions of its current operands and operation selector.

```text
                         +--------------------+
a, b ---> arithmetic --->|                    |
     ---> bitwise ------>| operation selection|---> result ---> zero detector (Z)
a    ---> fixed shifts ->| controlled by op   |         +-----> bit 7 (N)
                         +--------------------+
arithmetic carry/overflow and shifted-out bits ---> flag selection ---> C, V
```

These paths describe connected hardware. They can respond concurrently; `case` selects the output to expose. It does not run eight instructions on a processor. Synthesis may share or simplify the paths.

Later, a CPU can connect register outputs to `a` and `b` and capture the ALU result into a register at a clock edge. The ALU itself still does not need a clock. Flags will need their own storage if a later instruction must use them.

**Predict:** if `a` changes while `op` stays ADD, must the result wait for an edge? No: only physical gate propagation delays stand between the input change and the settled output.

## 2. Read the Verilog as a circuit description

| Construct in this lab | Hardware meaning |
| --- | --- |
| `input wire [7:0] a` | Eight input connections, one per bit |
| `output reg [7:0] result` | A variable assigned procedurally; `reg` alone does not imply storage |
| `localparam [2:0] OP_ADD = 3'd0` | A named constant encoding, with no register |
| `always @*` | Reevaluate when a read input changes in simulation; complete assignments describe combinational logic |
| Blocking `=` | Later statements in this execution see the just-computed value; not a physical clock or delay |
| `case (op)` | Decode the operation and select result/flag paths |
| `{carry, result}` | Concatenate a one-bit and eight-bit destination into nine connections |
| `{1'b0, a}` | A nine-bit operand with a constant-zero high bit |
| `a & b`, `a \| b`, `a ^ b`, `~a` | Parallel bitwise AND, OR, XOR, and inversion functions |
| `{a[6:0], 1'b0}` | Fixed rewiring for a one-bit left shift, with a zero inserted |
| `assign negative = result[7]` | Connect the sign-bit output directly to result bit 7 |
| `assign zero = (result == 8'd0)` | A zero detector, equivalent for known bits to NOR of all result bits |

The right shift is also fixed wiring; there is no variable shift amount and no barrel shifter. Selection still needs logic to choose the shift path over other operations.

`always @*` does **not** guarantee combinational hardware on its own. Every procedurally assigned output needs a value on every path. The defaults at the top set `result`, `carry`, and `overflow`, and each selected branch overrides what it needs. Omitting the default for `carry`, for example, would require it to remember the previous arithmetic carry during AND/OR operations: a latch.

In the counter, a missing assignment intentionally holds a clocked flip-flop. In this unclocked block, that same omission can introduce level-sensitive storage. Use blocking `=` for these combinational calculations and nonblocking `<=` for the counter's edge-triggered state updates.

**Predict:** after a SUB that sets overflow, what is overflow during AND? Zero, because the default is applied on every evaluation.

## 3. Adders, widths, and two meanings of overflow

One adder bit can be built from a full-adder function:

```text
sum[i]     = a[i] XOR b[i] XOR carry_in[i]
carry_out  = (a[i] AND b[i]) OR (carry_in[i] AND (a[i] XOR b[i]))
```

Carry connections link the bit positions. Our RTL explicitly extends both operands to nine bits before addition so the carry is retained. The low eight result bits wrap modulo 256.

Subtraction uses `a + ~b + 1`. The inversion applies to the **eight-bit** `b`, then a zero is concatenated above it. For unsigned operands, the full sum is `a + (255 - b) + 1 = a - b + 256`; its ninth bit is one exactly when `a >= b`. That is why `C=1` means **no borrow** here. Other architectures can choose a different subtraction convention.

Signed and unsigned arithmetic use the same result bits. The interpretation differs:

| Expression | Eight-bit result | C | V | Reason |
| --- | --- | --- | --- | --- |
| `ff + 01` | `00` | 1 | 0 | Unsigned 255 + 1 wraps; signed −1 + 1 fits |
| `7f + 01` | `80` | 0 | 1 | Signed 127 + 1 exceeds 127 |
| `80 + 80` | `00` | 1 | 1 | Signed −128 + −128 and unsigned 128 + 128 both exceed their ranges |
| `00 - 01` | `ff` | 0 | 0 | Unsigned borrow; signed −1 fits |
| `80 - 01` | `7f` | 1 | 1 | No unsigned borrow, but signed −128 − 1 is below −128 |

For addition, overflow occurs when operands have the same sign and the result has the opposite sign. For subtraction, it occurs when operands have opposite signs and the result's sign differs from `a`. Those are the XOR/AND expressions in the RTL. Blocking assignment ensures `result[7]` in those expressions refers to the result just calculated.

The ports are unsigned bit vectors. We explicitly derive signed overflow from the bit patterns; declaring everything `signed` is unnecessary. Logical right shift always inserts zero, even when `a[7]` is one.

**Predict:** `ff + ff` should produce `fe`, `Z=0`, `N=1`, `C=1`, `V=0`. Explain it once as unsigned 255 + 255 and once as signed −1 + −1.

## 4. Read the short waveform

Run `make waves-alu`, open `build/alu.vcd` in Surfer, and add `alu_tb.dut` signals `a`, `b`, `op`, `result`, `zero`, `negative`, `carry`, and `overflow`. Use hex for operands/results, unsigned decimal for `op`, and binary for flags. The Verilator trace uses the same stimulus and is at `build/alu-verilator.vcd` after `make test-alu-verilator`.

Inputs are first driven at 10 ns, then every 10 ns. Each check occurs 1 ns after stimulus; the remaining 9 ns merely makes the trace readable. These are testbench delays, not ALU latency or a 100 MHz clock. The delay-free RTL settles through simulator events at the same displayed timestamp as the input change. Physical propagation delay and glitches require a different timing model.

| Time | Stimulus | What to notice |
| --- | --- | --- |
| 20 ns | ADD `ff, 01` | Result zero, Z and C high, V low |
| 30 ns | ADD `7f, 01` | Result `80`, N and V high, C low; only `a` changes |
| 50 ns | SUB `00, 01` | Result `ff`, C low means a borrow |
| 70 ns | SUB `03, 03` | Result zero, Z and C high: equality needs no borrow |
| 80 ns | SUB `80, 01` | Result `7f`, V high despite a nonnegative result |
| 100 ns | AND `aa, 55` | Result zero; C and V cleared |
| 110–120 ns | OR then XOR of `aa, 55` | Both produce `ff`; only `op` changes at 120 ns |
| 160 ns | SHL `81` | Result `02`, C receives old bit 7 |
| 170 ns | SHR `81` | Result `40`, C receives old bit 0; no sign extension |
| 180 ns | SHL `40` | Result `80`, N high but V stays low by contract |

Before 10 ns, input values are unspecified. Icarus's four-state simulation can expose unknowns that Verilator's normal two-state execution represents differently. Compare defined behavior after stimulus, not power-up defaults.

The first testbench version initialized all inputs to zero at declaration and immediately applied a zero vector. Icarus left the output unknown: no input transition woke the `always @*` block after it began waiting. The testbench now leaves inputs unspecified and drives its first vector after a startup delay. This is a simulation scheduling lesson, not a reason to add reset circuitry to a combinational ALU.

## 5. Inspect the synthesized circuit

`make synth-alu` writes `build/alu-synth.log` and `build/alu.json`. On the verified Yosys 0.69+post toolchain, generic synthesis produced:

| Cell | Count |
| --- | ---: |
| ANDNOT | 12 |
| AND | 61 |
| NAND | 84 |
| NOR | 2 |
| ORNOT | 15 |
| OR | 32 |
| XNOR | 5 |
| XOR | 12 |
| **Total combinational cells** | **223** |
| Flip-flops / latches | **0** |

ANDNOT means `A & ~B`; ORNOT means `A | ~B`. Arithmetic, selection, inversion, and flag logic have all been mapped and optimized into this gate network. There need not be a standalone mux or NOT cell for each corresponding source construct.

The synthesis command runs `check -assert` and `select -assert-none t:*DFF* t:*LATCH*` after generic synthesis, so connectivity problems or these inferred storage cells fail the target. The inspected netlist contains only the combinational types above. The counter, by comparison, contains eight flip-flops.

These are generic cell counts, not transistor counts, FPGA LUT usage, or a timing result. Different tool versions and mapping choices may change them. No board frequency has been established.

Verification on 2026-09-19: Icarus and Verilator each pass 524,307 result-and-flag checks; Verilator RTL lint and Yosys synthesis/checks pass. Both short VCDs end at 200 ns, and their documented waveform samples agree. The counter still passes all 264 checks on both simulators, lint, and synthesis.

## 6. Exercises

Keep the committed baseline before experimenting. Predict first, then change RTL, specification, and test expectations together.

1. Before looking at a trace, calculate all flags for `7f - ff`, `80 + ff`, and `01 >> 1`. Add these to the short directed sequence and check your predictions.
2. Make SHR arithmetic by copying `a[7]` into the vacated position. Which result changes for `a=80`, and what should happen to C? Update the reference model as well as the contract.
3. Temporarily omit the `overflow = 0` default. Run lint and synthesis and inspect the inferred-storage warning/failure; then restore it. Why would a combinational ALU need memory without that assignment?
4. Sketch an eight-bit output register connected to this ALU. Mark which signals respond between edges and which update only at a rising edge. No CPU implementation is needed yet.
