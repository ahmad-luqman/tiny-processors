# RV32 RTL CPU: the first multicycle slice

Our first hardware implementation of the [RV32 machine](rv32.md): a multicycle RV32I core in [rtl/rv32/](../rtl/rv32/) that drives the contract's ready/valid memory port, runs a documented subset of the instruction set, and retires instructions in exactly the form the [emulator's trace contract](rv32-emulator.md#retirement-trace-contract) fixed in M2. The testbench in [tests/rv32_tb.sv](../tests/rv32_tb.sv) is the memory and the devices; it prints the same trace text, so `diff` compares the two backends line for line, with and without memory stalls. The gate-level walkthrough is [rv32-to-gates.md](rv32-to-gates.md).

## Implementation plan (M3)

1. Preserve every existing command; start a branch and end with one pull request, as M2 did.
2. Write this contract before any Verilog: controller states, port activity per state, the instruction subset, and what happens to every other encoding.
3. Implement the register file, ALU, immediate decoder, and the controller in `rtl/rv32/`; write the testbench memory with `$readmemh` images and configurable stalls.
4. Print the retirement trace in the emulator's format; add a Python test that assembles a short loop with the shared encoder, runs both backends, and diffs the traces under several stall settings.
5. Lint with Verilator `--Wall`, synthesize with Yosys asserting no latches, record the cell and flip-flop counts, capture one waveform with a held request and a single retirement.
6. Write the walkthrough, update the roadmap, stop before broadening to full RV32I (M4).

## What the RTL is

The emulator applies one instruction's effect per loop iteration and knows nothing about time. The RTL describes storage and logic that a clock advances: an instruction takes four or five rising edges plus every cycle the memory holds `ready` low, and each of those edges moves the machine through a named state. The two agree at retirement, which is why the trace is defined without cycle counts; the RTL additionally reports `cycles`, `stalls`, and `transfers`, which the emulator cannot.

## Instruction subset

The M3 core executes these eleven RV32I instructions with the semantics of the [contract](rv32.md) and the [emulator](rv32-emulator.md#machine-model). `sb` is the one addition to the roadmap's minimum: it is the only way the slice can print a console byte, and it exercises the byte strobe.

| Mnemonic | Format | `opcode` / `funct3` / `funct7` | Effect | Cycles |
| --- | --- | --- | --- | --- |
| `lui rd, imm` | U | `0110111` | `rd = imm << 12` | 4 |
| `auipc rd, imm` | U | `0010111` | `rd = pc + (imm << 12)` | 4 |
| `addi rd, rs1, imm` | I | `0010011` / `000` | `rd = rs1 + sext(imm)` | 4 |
| `add rd, rs1, rs2` | R | `0110011` / `000` / `0000000` | `rd = rs1 + rs2` | 4 |
| `sub rd, rs1, rs2` | R | `0110011` / `000` / `0100000` | `rd = rs1 - rs2` | 4 |
| `lw rd, imm(rs1)` | I | `0000011` / `010` | `rd = mem32[rs1 + sext(imm)]` | 5 |
| `sw rs2, imm(rs1)` | S | `0100011` / `010` | `mem32[rs1 + sext(imm)] = rs2` | 5 |
| `sb rs2, imm(rs1)` | S | `0100011` / `000` | `mem8[rs1 + sext(imm)] = rs2[7:0]` | 5 |
| `beq rs1, rs2, off` | B | `1100011` / `000` | `if rs1 == rs2: pc += sext(off)` | 4 |
| `bne rs1, rs2, off` | B | `1100011` / `001` | `if rs1 != rs2: pc += sext(off)` | 4 |
| `jal rd, off` | J | `1101111` | `rd = pc + 4; pc += sext(off)` | 4 |

Writes to `x0` are discarded and never appear in the trace. Cycles are counted without stalls; every cycle the memory holds `ready` low adds one.

### Every other encoding

The core distinguishes two kinds of instruction it will not execute, and the testbench reports them differently. Neither has any architectural effect.

- **Illegal on the machine, terminal fault.** An encoding the emulator also rejects (`mul` and every other M-extension word, `fence.i`, an unused `funct3`/`funct7`, an unknown opcode, `mstatus` and other CSRs the machine lacks) stops the core with `fault = 1`, `fault_cause = 2`, and `fault_value` holding the instruction word, exactly the `mcause`/`mtval` pair the emulator writes. `ecall` (cause 11, value 0) and `ebreak` (cause 3, value the PC) stop the core the same way, since they trap on the machine and the slice has nowhere to vector. The testbench prints the emulator's trap line and halts with `halt=fault`.
- **Valid RV32I the slice does not implement, unsupported halt.** `jalr`, `lb`/`lh`/`lbu`/`lhu`, `sh`, `blt`/`bge`/`bltu`/`bgeu`, `slti`/`sltiu`/`xori`/`ori`/`andi`, the shifts, `sll`/`slt`/`sltu`/`xor`/`srl`/`sra`/`or`/`and`, `fence`, `csrrw`/`csrrs`/`csrrc` and their immediate forms on the four existing CSRs, and `mret` stop the core with `unsupported = 1`. The emulator executes these, so no trace line is printed for them: faking a trap would make the two backends disagree about the machine. The testbench halts with `halt=unsupported pc=<pc> word=<word>`, and the differential test checks that every earlier line matches the emulator and that the emulator's trace continues past that point. M4 removes this category.

The decoder therefore has two outputs. `illegal` is written arm by arm to mirror the emulator's `goto illegal` paths; `unsupported` is derived as "neither executed nor illegal", so an encoding that no arm names halts rather than retiring as a no-op. The illegal check is the one M4 keeps.

## Faults are terminal in M3

The core has no CSRs and no `mtvec`: a trap cannot vector anywhere, so every trap the contract defines stops the core with `fault = 1` and the emulator's `mcause` in `fault_cause` and `mtval` in `fault_value`:

| `fault_cause` | Raised by | `fault_value` |
| --- | --- | --- |
| 0 | `jal` or a taken branch whose target is not word aligned | the target |
| 1 | fetch with `error` (outside RAM, or a device address) | the PC |
| 2 | illegal encoding (above) | the instruction word |
| 3 | `ebreak` | the PC |
| 4, 6 | `lw`, `sw` address not word aligned (`sb` is always aligned) | the effective address |
| 5, 7 | `lw`, `sw`/`sb` with `error` at acceptance | the effective address |
| 11 | `ecall` | 0 |

Misalignment is checked before the request is issued, so a misaligned access to an unmapped address reports misalignment and never reaches the bus, as in the emulator. The testbench prints `<step> <pc> <word> trap <cause> <value>` (word `00000000` for a fetch fault) and halts. The emulator with `mtvec` at its reset value prints that same line, then one more line for the fetch of the missing handler at PC 0 (`trap 1 00000000`), then halts as a double fault; the RTL trace therefore equals the emulator trace's prefix through the first trap line. M4 adds the CSRs, `mret`, and vectoring, after which a trap is an ordinary control transfer on both backends.

## Reset

`reset` is synchronous and active high. After it: PC `0x8000_0000`, the state `FETCH`, every register `x1`–`x31` zero (the emulator resets them to zero too, and a simulator would otherwise print `xxxxxxxx` for any read before write), no request on the bus, `halted`/`fault`/`unsupported` low. The 31 resettable 32-bit registers are most of the core's flip-flops; the walkthrough counts them.

## Memory port

The core drives the port from the [memory transaction contract](rv32.md#memory-transaction-contract), one request at a time:

| Signal | Direction | Meaning |
| --- | --- | --- |
| `mem_valid` | out | A request is presented and held stable until the cycle in which `mem_ready` is high. |
| `mem_addr[31:0]` | out | Byte address. Fetches and `lw`/`sw` are word aligned; `sb` carries the byte address and the slave uses `mem_wstrb`. |
| `mem_we` | out | 1 for a store. |
| `mem_wstrb[3:0]` | out | Byte lanes written: `1111` for `sw`, one bit selected by `mem_addr[1:0]` for `sb`, `0000` for reads. |
| `mem_wdata[31:0]` | out | Store data. For `sb` the byte is replicated on all four lanes so the strobe alone selects it. |
| `mem_ready` | in | Acceptance; sampled on the rising edge together with `mem_rdata` and `mem_error`. |
| `mem_rdata[31:0]` | in | The full aligned word for a read. |
| `mem_error` | in | The access was refused: the core raises cause 1, 5, or 7 and issues nothing further. |
| `mem_fetch` | out | Sideband, not part of the contract: 1 while a request is presented and it is an instruction fetch. The testbench uses it to keep device side effects off fetches and to attribute data transactions to the retiring instruction. |

A write takes effect exactly once, at acceptance. The core never changes `mem_addr`, `mem_we`, `mem_wstrb`, or `mem_wdata` while `mem_valid` is high and `mem_ready` is low; the testbench checks this on every cycle of a stall and fails the run if it is violated.

## Controller

Five states in a 3-bit register plus a terminal `HALT`. One instruction visits four or five of them:

| State | On this rising edge | Port | Next |
| --- | --- | --- | --- |
| `FETCH` | Wait for `mem_ready`; capture `mem_rdata` into `ir` and the PC into `ir_pc`. `mem_error` → fault 1. | `mem_valid=1, mem_addr=pc, mem_fetch=1` | `DECODE` |
| `DECODE` | Read `rs1`/`rs2` from the register file into `a`/`b`; the immediate and control fields are combinational from `ir`. `illegal` → fault 2; `unsupported` → halt; `ecall`/`ebreak` → fault 11/3. | idle | `EXECUTE` |
| `EXECUTE` | The ALU computes `a op b`, `a + imm`, or `pc + imm` (the branch or jump target); a separate equality comparator decides a branch; capture `alu_out` and `taken`. A misaligned load/store address → fault 4/6; a misaligned taken target → fault 0. | idle | `MEM` for `lw`/`sw`/`sb`, else `WRITEBACK` |
| `MEM` | Wait for `mem_ready`; capture `mem_rdata` into `mdr`. `mem_error` → fault 5/7. | `mem_valid=1, mem_addr=alu_out, mem_we/mem_wstrb/mem_wdata` for stores | `WRITEBACK` |
| `WRITEBACK` | Write `rd` (unless `x0`), update the PC (`pc+4`, the branch/jump target), pulse `retire` for one cycle. | idle | `FETCH` |
| `HALT` | Hold everything. Only `reset` leaves. | idle | `HALT` |

A fault or an unsupported instruction enters `HALT` from the state that detected it, before any register, PC, or memory write of that instruction. A store's write happens at acceptance in `MEM`, which is before the instruction retires in `WRITEBACK`; if the slave accepted the store it cannot fault afterwards, so the exactly-once write and the retirement always agree.

## Retirement port

An RVFI-style sideband for the testbench, separate from the memory port:

| Signal | Meaning |
| --- | --- |
| `retire` | High for the one cycle after the `WRITEBACK` edge. |
| `retire_pc`, `retire_insn` | The retired instruction's PC and word. While halted they hold the instruction that stopped the core. |
| `retire_rd_we`, `retire_rd`, `retire_rd_value` | The register written, if any. `retire_rd_we` is low for `x0`, stores, and branches. |
| `halted` | The core is in `HALT`. |
| `fault`, `fault_cause[3:0]`, `fault_value[31:0]` | Terminal fault, as tabulated above. |
| `unsupported` | Halted on a valid but unimplemented encoding. |
| `state[2:0]`, `pc[31:0]` | For waveforms. |

Memory effects are not duplicated here. The testbench is the memory, so it records the last accepted data transaction (address, strobe, write data, read data) and prints it after the register field at the next `retire`: a store shows the address, the value narrowed to the strobed lanes, and the width in bytes; a load shows the raw word and `/4`. That is the same information the emulator prints, produced from the bus rather than from inside the core.

## Counters

The testbench keeps three counters and prints them in its halt line; the walkthrough relates them to the instruction count:

- `cycles`: rising edges from the first edge with `reset` low through the edge on which the last instruction retires or the core halts, inclusive.
- `transfers`: accepted port transactions, fetches included. A retired instruction costs one fetch, plus one data transaction for `lw`/`sw`/`sb`.
- `stalls`: rising edges on which `mem_valid` was high and `mem_ready` low.

With a fixed stall of `N` cycles per request, `cycles = 4 × (retired non-memory instructions) + 5 × (retired memory instructions) + N × transfers`; the differential test asserts this.

## Testbench: memory, devices, and outputs

[tests/rv32_tb.sv](../tests/rv32_tb.sv) instantiates the core and provides everything else on the bus, with the emulator's address decode:

- **RAM**: `RAM_WORDS` words (default the contract's 4 MiB) at `0x8000_0000`, zero-filled, then loaded by `$readmemh` from `+image=FILE` in the one-word-per-line format `tools/rv32_image.py --hex` writes. Sub-word stores merge by strobe.
- **Console** `0x1000_0000`: a byte store to +0 prints the byte on stdout. A read whose address is +5 returns the status byte `0x20` in lane 1 of the aligned word, which is how a byte load of the status register would arrive; the M3 core issues only word loads, so this path is unreachable until M4 and the contract's read side still lacks a width (a byte load and a word load of +4 are the same request). Every other access to the eight bytes is an `error`.
- **Done register** `0x0010_0000`: a word store ends the run after that store's instruction retires. Other widths and loads are an `error`. The word is reported in the halt line the way the emulator reports it: `0x5555` is `pass`, `(code << 16) | 0x3333` is `fail=<code>`, and anything else is an error.
- **Everything else**, and any fetch from a device address, is an `error` at acceptance.
- **Stalls**: `+stall=N` holds `ready` low for `N` cycles on every request; `+stall-seed=S` draws 0 to 3 cycles per request from `$random(S)`. `ready` is driven on the falling edge, away from the sampling edge.
- **Outputs**: stdout carries console bytes only; the testbench stops the clock instead of calling `$finish`, because Verilator prints a `$finish` banner on stdout, and the runner passes `+verilator+quiet` to a Verilator build. `+trace=FILE` receives the retirement trace. `+wave=FILE` dumps a VCD of the core. `+max-cycles=N` (default 1,000,000) turns a runaway into `halt=limit`. The last stderr line is authoritative:

```
rv32_tb: halt=<done|fault|unsupported|limit> cycles=N steps=N stalls=N transfers=N [done=WORD] [cause=C tval=V] [pc=P word=W] <pass|fail=<code>|error=<reason>>
```

`steps` is the number of trace lines written, so it counts a terminal fault as the emulator's `steps` does. Guest outcomes stop the clock after this line. `$fatal` is reserved for harness violations: a request on the bus during reset, a request changing while stalled, a plusarg that is not a decimal (`+stall=abc`, `+max-cycles=0`, `+stall` together with `+stall-seed`), an unwritable `+wave` or `+trace` path, a missing image or one with a token that is not an eight-digit hex word, or a data transaction with no retirement after it. Both simulators print those diagnostics on stdout, so the Python side treats any diagnostic line there as a failed run whatever the exit status.

## Run and verify

```sh
make test-rv32-rtl             # Python differential tests: emulator vs Icarus, all stall settings
make test-rv32-rtl-verilator   # the same tests on a Verilator build of the testbench
make lint-rv32                 # verilator --Wall on the core
make synth-rv32                # yosys: no latches; cell and flip-flop counts in build/rv32-synth.log
make waves-rv32                # the loop with +stall=2 into build/rv32/rtl/loop.vcd
make bench-rv32-rtl            # cycles, stalls, and transfers for the loop at several stall depths
make test-rv32                 # M1 firmware, M2 emulator, and M3 RTL together
```

The loop program is `program_loop` in [tools/rv32_asm.py](../tools/rv32_asm.py): sum 1..10 through a memory word, a backward `bne`, a `jal` over a skipped instruction, a byte store into a word the image never wrote, and both `beq` outcomes, then the pass word. [tools/rv32_rtl.py](../tools/rv32_rtl.py) assembles it, runs both backends, and diffs the traces; the tests in [tests/test_rv32_rtl.py](../tests/test_rv32_rtl.py) add directed programs for every instruction and every kind of stop.

## Verification

- `make test-rv32-rtl`: 19 tests in [tests/test_rv32_rtl.py](../tests/test_rv32_rtl.py), about 50 s including compiling the emulator and the testbench into a temporary directory. The loop trace equals the emulator's line for line with 0, 1, and 3 stall cycles per request and with seeded random stalls, and the cycle formula holds. A directed program covers every subset instruction with hand-computed anchors (a negative immediate, `sub` wraparound, `auipc` at a non-zero PC, a discarded `x0` write, both branch outcomes, a backward `jal`, four `sb` lanes read back as one word, a load of a word the image never wrote reading zero). Five illegal encodings plus `ecall` and `ebreak` fault with the emulator's cause and value; seventeen valid but unimplemented encodings halt as `unsupported` with no trace line while the emulator carries on; fifteen memory and target faults (outside the map, misaligned, seven console and done-register misuses, a `jal` and a taken branch to a non-word target, a fetch outside RAM) match the emulator's trap line, and the last RAM word is reachable; the split J, B, S, and I immediate fields are each exercised with an offset that sets one high bit, plus the extreme U and I immediates; a not-taken branch to a misaligned target does not fault; an opcode × funct3 sweep (with every funct7 class where it matters) agrees with the emulator on illegal, executed, or unsupported for 143 words; console bytes reach stdout, a byte above 0x7f is compared without crashing the host, and each done-word outcome including the 255/256 boundary is reported as the emulator reports it; a runaway loop hits `+max-cycles`, a run whose terminal edge equals the limit is still reported as `done` or `fault`, once, and a limit one cycle before the done store retires is a limit; bad plusargs, bad images, and an unwritable wave path fail on both simulators. The seeded run must actually stall. Six helper tests pin the trace diff, the halt-line parser and its per-reason key validation, the simulator command lines and the diagnostic filter, `check_passed`, the stale-trace truncation, and the encoder's bounds and helpers with literal values and no simulator.
- `make test-rv32-rtl-verilator`: the same 19 tests on the Verilator build of the testbench (about 3 s once built).
- `make lint-rv32`: Verilator `--Wall` on the four core files, no warnings.
- `make synth-rv32`: Yosys `check -assert` and no latches; 5,777 cells, 1,331 flip-flops, counted in the [walkthrough](rv32-to-gates.md#7-what-synthesis-actually-built).
- `make waves-rv32` and `make bench-rv32-rtl`: the loop under `+stall=2` as a VCD, and the cycle table for stalls 0 to 3 and a seeded run. Both require the simulator to exit cleanly and report `halt=done ... pass`; a matching trace alone is not success.
- The testbench fails the run if the core presents a request while `reset` is high, and reports `halt=limit ... error=limit` even when a done store was accepted but not yet retired when the limit hit.
- `make test-rv32` runs M1, M2, and these, on both simulators, after one another; `make test` is still the counter.

What is not verified: the console status read (unreachable without byte loads), a fetch from a device address (unreachable without `jalr`), and any timing of the port beyond the testbench's stall generator. All three are M4 work.

## Milestone result

Completed on 2026-09-20 on branch `m3-rv32-rtl`. From a clean `build/`:

- `make test-rv32-rtl`: 19 tests pass on Icarus; `make test-rv32-rtl-verilator`: the same 19 on Verilator.
- The loop: 78 instructions on both backends, identical traces; 336 cycles unstalled, 102 transfers, and 102 more cycles per stall cycle.
- `make lint-rv32` clean, `make synth-rv32` 5,777 cells with no latches.
- `make test-rv32` (QEMU, emulator, QEMU differential, RTL, lint, synthesis) passes; counter, ALU, SAP8, and SIMD4 targets unchanged and passing.

Limitations that remain: eleven instructions; faults halt instead of vectoring; no CSRs; word loads only; the testbench is the only memory model; one memory port with no overlap between fetch and execution. M4 broadens the core to full RV32I and runs the C self-check on it.
