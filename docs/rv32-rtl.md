# RV32 RTL CPU: full RV32I, vectored traps, and the C self-check on hardware

Our hardware implementation of the [RV32 machine](rv32.md): a multicycle RV32I core in [rtl/rv32/](../rtl/rv32/) that drives the contract's ready/valid memory port, executes every RV32I instruction plus the four trap CSRs and `mret`, vectors traps through `mtvec`, and retires instructions in exactly the form the [emulator's trace contract](rv32-emulator.md#retirement-trace-contract) fixed in M2. Since M5 the core is one instance inside [rv32_soc.v](../rtl/rv32/rv32_soc.v), with the RAM and the devices behind a bus decoder ([SoC record](rv32-soc.md)); the testbench in [tests/rv32_tb.sv](../tests/rv32_tb.sv) is the host: it loads the image, holds the bus to model stalls, takes the console bytes and the done word, and prints the same trace text, so `diff` compares the two backends line for line, and the M1 C self-check runs on the machine to `PASS 807d9fad` with the emulator's 32,610-line trace. The gate-level walkthrough is [rv32-to-gates.md](rv32-to-gates.md).

## Implementation plan (M3)

1. Preserve every existing command; start a branch and end with one pull request, as M2 did.
2. Write this contract before any Verilog: controller states, port activity per state, the instruction subset, and what happens to every other encoding.
3. Implement the register file, ALU, immediate decoder, and the controller in `rtl/rv32/`; write the testbench memory with `$readmemh` images and configurable stalls.
4. Print the retirement trace in the emulator's format; add a Python test that assembles a short loop with the shared encoder, runs both backends, and diffs the traces under several stall settings.
5. Lint with Verilator `--Wall`, synthesize with Yosys asserting no latches, record the cell and flip-flop counts, capture one waveform with a held request and a single retirement.
6. Write the walkthrough, update the roadmap, stop before broadening to full RV32I (M4).

## Implementation plan (M4)

1. Preserve every M3 command; one branch, one pull request.
2. Fix the read side of the memory port in the [contract](rv32.md#memory-transaction-contract) before the Verilog: the strobe names the byte lanes of reads as well as writes, so a byte load of the console status is distinguishable from a word load.
3. Broaden the core to full RV32I in verified increments: sub-word loads and stores, the eight ALU operations and shifts, the four ordered branches, `jalr`, `fence`; keep the decoder's illegal check and remove the `unsupported` halt as each class lands.
4. Add the four CSRs, the CSR instructions, `mret`, and trap vectoring, with the emulator's double-fault rule decided and tested first.
5. Run `build/rv32/selfcheck.hex` on both simulators, with and without stalls, requiring `PASS 807d9fad` and the identical trace; relate its cycle count to its instruction count.
6. Lint, synthesize, record the counts against M3's, capture the waves the roadmap asks for, publish the coverage and limitations table, update the roadmap, stop before M5's devices.

## What the RTL is

The emulator applies one instruction's effect per loop iteration and knows nothing about time. The RTL describes storage and logic that a clock advances: an instruction takes four or five rising edges plus every cycle the memory holds `ready` low, and each of those edges moves the machine through a named state. The two agree at retirement, which is why the trace is defined without cycle counts; the RTL additionally reports `cycles`, `stalls`, and `transfers`, which the emulator cannot.

## Instruction set

The core executes all of RV32I and the M2 trap subset with the semantics of the [contract](rv32.md) and the [emulator](rv32-emulator.md#machine-model). Cycles are counted without stalls; every cycle the memory holds `ready` low adds one. Writes to `x0` are discarded and never appear in the trace.

| Class | Mnemonics | Effect | Cycles |
| --- | --- | --- | --- |
| Upper immediate | `lui`, `auipc` | `rd = imm << 12`, `rd = pc + (imm << 12)` | 4 |
| Immediate ALU | `addi`, `slti`, `sltiu`, `xori`, `ori`, `andi`, `slli`, `srli`, `srai` | `rd = rs1 op sext(imm)`; shifts use `imm[4:0]` | 4 |
| Register ALU | `add`, `sub`, `sll`, `slt`, `sltu`, `xor`, `srl`, `sra`, `or`, `and` | `rd = rs1 op rs2`; shifts use `rs2[4:0]` | 4 |
| Loads | `lb`, `lh`, `lw`, `lbu`, `lhu` | `rd = ext(mem[rs1 + sext(imm)])`, sign extension for `lb`/`lh`, zero for `lbu`/`lhu` | 5 |
| Stores | `sb`, `sh`, `sw` | `mem[rs1 + sext(imm)] = rs2` narrowed to the width | 5 |
| Branches | `beq`, `bne`, `blt`, `bge`, `bltu`, `bgeu` | `if cond: pc += sext(off)` | 4 |
| Jumps | `jal`, `jalr` | `rd = pc + 4; pc = target` (`jalr` clears bit 0 of `rs1 + sext(imm)`) | 4 |
| Fence | `fence` | nothing: one hart, no caches | 4 |
| CSR | `csrrw`, `csrrs`, `csrrc`, `csrrwi`, `csrrsi`, `csrrci` on `mtvec`, `mepc`, `mcause`, `mtval` | `rd = old; csr = new` (`csrrs`/`csrrc` write only when the `rs1` field is nonzero; `mtvec` and `mepc` keep bits [1:0] zero) | 4 |
| Return | `mret` | `pc = mepc` | 4 |
| Traps | `ecall`, `ebreak` | trap with cause 11 / 3 (below) | none: the instruction does not retire |

### Every other encoding

An encoding the emulator rejects (`mul` and every other M-extension word, `fence.i`, an unused `funct3`/`funct7`, an unknown opcode, `wfi`, `sret`, `mstatus` and every other CSR the machine lacks) traps with cause 2 and the instruction word in `mtval`, exactly the emulator's `illegal` path. The decoder's `illegal` output is written arm by arm to mirror `goto illegal` in [tools/rv32emu.c](../tools/rv32emu.c); every valid word sets exactly one class flag. The M3 `unsupported` halt no longer exists: there is no valid RV32I word the core refuses.

## Traps

A trap is a control transfer, as in the emulator: the instruction has no effect, `mepc` receives its PC (the fetch address for a fetch fault), `mcause` and `mtval` the values below, and the next fetch is from `mtvec`. The testbench prints the emulator's trap line `<step> <pc> <word> trap <cause> <value>` (word `00000000` for a fetch fault) and execution continues.

| `mcause` | Raised by | `mtval` |
| --- | --- | --- |
| 0 | `jal`, `jalr`, or a taken branch whose target is not word aligned (`jalr` clears bit 0 first) | the target |
| 1 | fetch with `error` (outside RAM, or a device address) | the PC |
| 2 | illegal encoding (above) | the instruction word |
| 3 | `ebreak` | the PC |
| 4, 6 | load, store not naturally aligned (`lh`/`lhu`/`sh` on an odd address, `lw`/`sw` off a word boundary) | the effective address |
| 5, 7 | load, store with `error` at acceptance | the effective address |
| 11 | `ecall` | 0 |

Misalignment is checked before the request is issued, so a misaligned access to an unmapped address reports misalignment and never reaches the bus, as in the emulator. A refused load or store was presented on the bus and accepted with `error`; the testbench records that transaction as it records any other, and drops it when the trap line is printed, so the next retirement does not inherit it.

**Double fault.** The core keeps one bit, `in_trap`, set by a trap and cleared when an instruction retires. A trap while it is set cannot be delivered: the core prints its line (the `trap` pulse still fires) and enters `HALT` with the first trap's CSRs intact, and the testbench reports `halt=double-fault` with the second trap's cause and value. This is the emulator's rule, so with `mtvec` at its reset value 0 both backends print the first trap line, then `trap 1 00000000` for the fetch of the missing handler, then stop; every trace, with or without a handler, is identical on both backends. A trap taken after the handler has retired at least one instruction is an ordinary nested trap and overwrites the CSRs.

## Reset

`reset` is synchronous and active high. After it: PC `0x8000_0000`, the state `FETCH`, every register `x1`–`x31` zero (the emulator resets them to zero too, and a simulator would otherwise print `xxxxxxxx` for any read before write), the four CSRs zero, `in_trap` clear, no request on the bus, `halted` low. The 31 resettable 32-bit registers are most of the core's flip-flops; the walkthrough counts them.

## Memory port

The core drives the port from the [memory transaction contract](rv32.md#memory-transaction-contract), one request at a time:

| Signal | Direction | Meaning |
| --- | --- | --- |
| `mem_valid` | out | A request is presented and held stable until the cycle in which `mem_ready` is high. |
| `mem_addr[31:0]` | out | Byte address of the access. Fetches and word accesses are word aligned; halfword accesses are even; a byte access carries its byte address. |
| `mem_we` | out | 1 for a store. |
| `mem_strb[3:0]` | out | The byte lanes of the access within the aligned word, for reads and writes: `1111` for a word, `0011` or `1100` for a halfword, one bit for a byte; `0000` while no request is presented. A device that is only byte-addressable checks it: the console status is readable only as the byte at +5 (`mem_addr` +5, strobe `0010`). |
| `mem_wdata[31:0]` | out | Store data. A halfword or byte is replicated across the lanes so the strobe alone selects it. |
| `mem_ready` | in | Acceptance; sampled on the rising edge together with `mem_rdata` and `mem_error`. |
| `mem_rdata[31:0]` | in | The full aligned word for a read; the core selects the strobed lanes and extends them. |
| `mem_error` | in | The access was refused: the core raises cause 1, 5, or 7 and issues nothing further for that instruction. |
| `mem_fetch` | out | Part of the contract since M5: 1 while the presented request is an instruction fetch. The bus decoder refuses fetches outside RAM with `error`; the testbench uses it to attribute data transactions to the retiring instruction. |

A write takes effect exactly once, at acceptance, on the strobed lanes. The core never changes `mem_addr`, `mem_we`, `mem_strb`, or `mem_wdata` while `mem_valid` is high and `mem_ready` is low; the testbench checks this on every cycle of a stall and fails the run if it is violated.

## Controller

Five states in a 3-bit register plus a terminal `HALT`. One instruction visits four or five of them:

| State | On this rising edge | Port | Next |
| --- | --- | --- | --- |
| `FETCH` | Wait for `mem_ready`; capture `mem_rdata` into `ir` and the PC into `ir_pc`. `mem_error` → trap 1. | `mem_valid=1, mem_addr=pc, mem_strb=1111, mem_fetch=1` | `DECODE` |
| `DECODE` | Read `rs1`/`rs2` from the register file into `a`/`b`; the immediate and control fields are combinational from `ir`. `illegal` → trap 2; `ecall`/`ebreak` → trap 11/3. | idle | `EXECUTE` |
| `EXECUTE` | The ALU computes `a op b` or `a op imm` (results, effective addresses, the `jalr` target) and, for a branch, compares `a` with `b`; a separate adder forms `pc + imm` for `auipc`, `jal`, and branch targets; a CSR instruction reads the old value. Capture the result into `alu_out` and the branch decision into `taken`. A misaligned load/store address → trap 4/6; a misaligned taken target → trap 0. | idle | `MEM` for loads and stores, else `WRITEBACK` |
| `MEM` | Wait for `mem_ready`; capture `mem_rdata` into `mdr`. `mem_error` → trap 5/7. | `mem_valid=1, mem_addr=alu_out, mem_strb` from the width and `alu_out[1:0]`, `mem_we/mem_wdata` for stores | `WRITEBACK` |
| `WRITEBACK` | Write `rd` (unless `x0`) and, for a CSR instruction, the CSR; update the PC (`pc+4`, the branch/jump target, or `mepc` for `mret`); clear `in_trap`; pulse `retire` for one cycle. | idle | `FETCH` |
| `HALT` | Hold everything. Only `reset` leaves. | idle | `HALT` |

A trap leaves for `FETCH` (or `HALT`, on a double fault) from the state that detected it, before any register, CSR, PC, or memory write of that instruction. A store's write happens at acceptance in `MEM`, which is before the instruction retires in `WRITEBACK`; if the slave accepted the store without `error` it cannot trap afterwards, so the exactly-once write and the retirement always agree.

## Retirement port

An RVFI-style sideband for the testbench, separate from the memory port:

| Signal | Meaning |
| --- | --- |
| `retire` | High for the one cycle after the `WRITEBACK` edge. |
| `retire_pc`, `retire_insn` | The PC and word of the instruction in flight, valid on `retire` and on `trap`. |
| `retire_rd_we`, `retire_rd`, `retire_rd_value` | The register written, if any. `retire_rd_we` is low for `x0`, stores, branches, `fence`, and `mret`. |
| `trap`, `trap_cause[3:0]`, `trap_value[31:0]` | High for the one cycle after the edge that took a trap, with its `mcause` and `mtval`. |
| `halted` | The core is in `HALT`: a double fault. |
| `state[2:0]`, `pc[31:0]`, `mtvec`, `mepc`, `mcause`, `mtval` | For waveforms. |

Memory effects are not duplicated here. The testbench observes the machine's memory port, so it records the last accepted data transaction (address, strobe, write data, read data) and prints it after the register field at the next `retire`: a store shows the address, the value narrowed to the strobed lanes, and the width in bytes; a load shows the strobed lanes of the word read, before extension, and the same width. That is the same information the emulator prints, produced from the bus rather than from inside the core.

## Counters

The testbench keeps three counters and prints them in its halt line; the walkthrough relates them to the instruction count:

- `cycles`: rising edges from the first edge with `reset` low through the edge on which the last instruction retires or the core halts, inclusive.
- `transfers`: accepted port transactions, fetches included. A retired instruction costs one fetch, plus one data transaction for a load or store. A trapped instruction costs its fetch, plus the refused data transaction for a load or store fault.
- `stalls`: rising edges on which `mem_valid` was high and `mem_ready` low.

With a fixed stall of `N` cycles per request and no traps, `cycles = 4 × (retired non-memory instructions) + 5 × (retired memory instructions) + N × transfers`; `tools/rv32_rtl.py` prints this relation for every run, fails the run when it does not hold, and the tests assert it for the loop and the self-check. A trap costs the cycles up to the state that raised it, stalls included: one edge for a fetch fault (the refused fetch), two for an illegal word, `ecall`, or `ebreak` (fetch, `DECODE`), three for a misaligned address or target (through `EXECUTE`), four for a refused load or store (through the `MEM` edge that returned `error`). A run with trap lines is reported without the equality, and the runner refuses it unless `--allow-traps` says traps are expected.

## Testbench: the host of the machine

[tests/rv32_tb.sv](../tests/rv32_tb.sv) instantiates `rv32_soc` (the core, the bus decoder, the RAM, and every device; [SoC record](rv32-soc.md)) and provides what a machine needs from outside:

- **The image**: `RAM_WORDS` words (default the contract's 4 MiB) are zero-filled and loaded through the hierarchy (`$readmemh` into `dut.ram.mem`) from `+image=FILE` in the one-word-per-line format `tools/rv32_image.py --hex` writes; the framebuffer memory is zero-filled too. The RAM module has no initial block, so synthesis never sees a file.
- **Stalls**: `+stall=N` holds the bus (`mem_hold`) for `N` cycles on every request; `+stall-seed=S` draws 0 to 3 cycles per request from `$random(S)`. The hold is driven on the falling edge, away from the sampling edge, and keeps every slave from seeing the request, so nothing takes effect while it lasts. A device may add stalls of its own: the input device while the host pushes, the console when built with `CONSOLE_BUSY`.
- **Devices**: console bytes and the done word arrive as strobes with their data; `+input=FILE` schedules key events by frame and pushes them one per cycle when their frame is reached; `+checkpoints=FILE` receives `frame N <hash>` at each present, hashed over `dut.fb.mem`; `+reset-at=N` asserts reset again after counted cycle `N` for a reset-during-transfer test. Decoding, widths, and every `error` now live in the RTL, with the same rules as the emulator ([contract](rv32.md#devices)).
- **Outputs**: the testbench stops the clock instead of calling `$finish`, because Verilator prints a `$finish` banner on stdout, and the runner passes `+verilator+quiet` to a Verilator build. `+trace=FILE` receives the retirement trace. `+console=FILE` sends the guest's console bytes to a file instead of stdout; the runner always passes it, so a simulator's stdout is then its own messages only. `+wave=FILE` dumps a VCD of the whole machine (the core's signals are under `rv32_tb.dut.core`). `+max-cycles=N` (default 10,000,000; the diagnostic needs about two million) turns a runaway into `halt=limit`. The last stderr line is authoritative:

```
rv32_tb: halt=<done|double-fault|limit> cycles=N steps=N stalls=N transfers=N [done=WORD] [cause=C tval=V] <pass|fail=<code>|error=<reason>>
```

`steps` is the number of trace lines written, so it counts trap lines as the emulator's `steps` does; `cause` and `tval` on a double fault are the trap that could not be delivered, and the first trap is in the core's CSRs. Guest outcomes stop the clock after this line. `$fatal` is reserved for harness violations: a request on the bus during reset, a request changing while stalled, a plusarg that is not a decimal (`+stall=abc`, `+max-cycles=0`, `+stall` together with `+stall-seed`), an unwritable `+wave`, `+trace`, or `+checkpoints` path, a missing image or one with a token that is not an eight-digit hex word, an input script line that is not `frame N down|up KEY` with frames in order, a script that cannot be read, a scripted event that was dropped or never delivered on a run that passed (reported after the halt line, unless `+allow-lost-events`), two data transactions with no retirement or trap between them, or a trap after a write the machine accepted without `error` (the instruction would have had an effect). Both simulators print those diagnostics on stdout, where a guest could print anything, so the runner sends the console to a file with `+console` and treats any stdout output as a failed run, whatever the exit status; the one line it ignores is Icarus's `VCD info:` notice, which no guest can produce on that channel.

## Run and verify

```sh
make test-rv32-rtl             # Python differential tests: emulator vs Icarus, all stall settings
make test-rv32-rtl-verilator   # the same tests on a Verilator build of the testbench
make run-rv32-rtl              # the C self-check on Icarus: PASS 807d9fad, the emulator's trace, the cycle relation
make run-rv32-rtl-verilator    # the same on Verilator with one stall cycle per request
make lint-rv32                 # verilator --Wall on the core (lint-rv32-soc: the whole machine)
make synth-rv32                # yosys: no latches; cell and flip-flop counts in build/rv32-synth.log (synth-rv32-soc: the machine)
make waves-rv32                # the loop and the M4 program with +stall=2, and the M5 devices program, into build/rv32/rtl/*.vcd
make bench-rv32-rtl            # cycles, stalls, and transfers for the loop at several stall depths
make test-rv32                 # M1 firmware, M2 emulator, and the RTL together
```

[tools/rv32_rtl.py](../tools/rv32_rtl.py) assembles a program (`--program loop`, the M3 loop, `--program full`, the M4 waves program, or `--program devices`, the M5 one, in [tools/rv32_asm.py](../tools/rv32_asm.py)) or takes a flat image (`--image build/rv32/selfcheck.bin`), runs both backends, diffs the traces, the console text, and the checkpoint lines, and prints how the cycle count follows from the trace; `--mode bench` also checks that a fixed stall stalled every transfer. `--compare results` (M5) skips the trace diff for a program that reads the timer and compares the console, the outcome, the checkpoints, and the trap records without their step numbers instead; `--input` gives both backends a script, `--expect-last-line` and `--expect-checkpoint` pin the diagnostic's results, and `--backend emulator` runs one side. The tests in [tests/test_rv32_rtl.py](../tests/test_rv32_rtl.py) add directed programs for every instruction class, every trap, every device, and every kind of stop.

## Verification

- `make test-rv32-rtl`: 36 tests in [tests/test_rv32_rtl.py](../tests/test_rv32_rtl.py), about 90 s including compiling the emulator and the testbench into a temporary directory. M5's additions are listed in the [SoC record](rv32-soc.md#run-and-verify); the M4 families below are unchanged and still trace-identical. The loop trace equals the emulator's line for line with 0, 1, and 3 stall cycles per request and with seeded random stalls, and the cycle formula holds. Directed programs with hand-computed anchors cover: the M3 subset (a negative immediate, `sub` wraparound, `auipc` at a non-zero PC, a discarded `x0` write, both branch outcomes, a backward `jal`, four `sb` lanes read back as one word); sub-word loads and stores with byte order, sign and zero extension, halves assembled into a word, and the console status byte; the ALU edges (`0x7fffffff + 1`, `sltiu` against −1, signed versus unsigned `slt`, shifts by 31 and by 33, `and`/`or`/`xor` with the sign bit, `sub` of the minimum, `fence`); the six branches at the signed boundary in both directions and when equal; `jalr` with bit 0 set, with `rd == rs1`, and as `ret` after a `jal` call; the split J, B, S, and I immediate fields with one high bit set, plus the extreme U and I immediates; a not-taken branch to a misaligned target. Traps: five illegal encodings plus `ecall` and `ebreak`; 24 M4 memory and target faults (outside the map, misaligned words and halfwords, device widths, `jalr` targets, a fetch from a device address; 47 rows since M5), plus the last word, halfword, and byte of RAM stored and read back without one; eight traps of seven causes through one handler that reads the CSRs, counts, steps `mepc`, and returns with `mret`, including a refused load followed by an ordinary store and a misaligned store whose word is read back untouched; a nested trap after the handler retired an instruction (the handler re-enters itself on `ebreak` and reads the new `mcause` and `mepc`) versus a double fault on a handler whose first word is illegal or whose first load is refused, and `mret` as a handler's first instruction; CSR reads, writes, the `mtvec`/`mepc` masks, `csrrw`/`csrrwi` writing with a zero operand, and the zero-extended five-bit immediate; `mret` with `mepc` at reset. The 143-word opcode × funct3 (× funct7 where it matters) sweep now demands whole-trace equality and the same halt reason. The C self-check (a prerequisite of the Make targets; the test skips only when the suite is run by hand without the image, and derives the hex from the `.bin` so both backends run the same bytes) reaches `PASS 807d9fad` on the RTL with the emulator's 32,610 lines, unstalled, with one stall cycle, and seeded, and its cycle count follows the formula. Console bytes reach the console file, a byte above 0x7f is compared without crashing the host, guest text that looks like a simulator message is still guest text, and each done-word outcome including the 255/256 boundary is reported as the emulator reports it. A runaway loop hits `+max-cycles`; a terminal outcome on the limit's edge is still reported as that outcome, once; a trap line written on the limit's edge is kept. Bad plusargs, bad images, and an unwritable wave path fail on both simulators. The seeded run must actually stall. Nine helper tests pin the trace diff, the halt-line parser and its per-reason key validation (the M3 `fault` and `unsupported` reasons are now rejected), the simulator command lines and the diagnostic filter, `check_passed`, the cycle relation, the waveform check, the stale-trace truncation, and the encoder's bounds and helpers with literal values and no simulator.
- `make test-rv32-rtl-verilator`: the same 36 tests on the Verilator build of the testbench (about 8 s once built).
- `make run-rv32-rtl` and `make run-rv32-rtl-verilator`: the self-check, 32,610 instructions, 138,495 cycles unstalled and 40,665 transfers (32,610 fetches and 8,055 data accesses); one stall cycle per request adds 40,665 cycles. About one second on Icarus.
- `make lint-rv32`: Verilator `--Wall` on the four core files, no warnings; `make lint-rv32-soc` the same on the machine. Unused inputs of a device are consumed by one `wire unused_ok = &{1'b0, ...}` per module, the convention Verilator's default `*unused*` rule accepts.
- `make synth-rv32`: Yosys `check -assert` and no latches; 8,175 cells, 1,457 flip-flops, counted in the [walkthrough](rv32-to-gates.md#8-what-synthesis-actually-built); `make synth-rv32-soc` the machine with 64-word memories, tabulated in the [SoC record](rv32-soc.md#what-synthesis-built).
- `make waves-rv32` and `make bench-rv32-rtl`: the loop and the M4 program under `+stall=2` and the M5 devices program unstalled as VCDs, and the cycle table for stalls 0 to 3 and a seeded run. Both require the simulator to exit cleanly and report `halt=done ... pass`; a matching trace alone is not success.
- `make test-rv32` runs M1, M2, and these, on both simulators, after one another; `make test` is still the counter.

## Coverage and limitations

| Area | Implemented | Not implemented |
| --- | --- | --- |
| RV32I | all 37 computational, memory, and control instructions, `fence`, `ecall`, `ebreak` | `fence.i` (illegal on the machine) |
| Privileged | `mtvec` (direct mode), `mepc`, `mcause`, `mtval`, the six CSR instructions, `mret`, the trap causes 0–7 and 11, the double-fault halt | `mstatus`, `misa`, CSR counters (the timer is a device), `wfi`, `sret`, interrupts, nested trap state beyond the CSRs (a nested trap overwrites them, as in the emulator) |
| Extensions | none | M (illegal on the machine), A, F, C |
| Memory | one port, one request outstanding, byte strobes both ways, misalignment checked before the request is issued, exactly-once writes; RAM and framebuffer as RTL memories behind the bus decoder | overlap of fetch and data, caches, misaligned access emulation, memory macros in synthesis |
| Devices | console, done register, timer, input queue, display, and framebuffer as RTL peripherals with the contract's fault edges; fetches refused outside RAM; a device may hold `ready` (M5, [record](rv32-soc.md)) | a palette, interrupts, DMA, a host-time timer mode |
| Timing | 4 or 5 cycles per instruction plus stalls, whether from the host's hold or a device; the formula is asserted; a reset mid-transfer abandons the request | pipelining, a real memory model's latency, any frequency claim |

Both simulators run every test, the self-check, and the stalled self-check; nothing is Icarus- or Verilator-only.

## Milestone results

**M3**, completed on 2026-09-20 on branch `m3-rv32-rtl`: eleven instructions, 20 tests, the loop at 336 cycles unstalled and 102 more per stall cycle, 5,777 cells and 1,331 flip-flops. Faults halted the core and valid unimplemented words stopped it as `unsupported`; the trace equalled the emulator's up to the first trap line.

**M4**, completed on 2026-09-21 on branch `m4-rv32-full-isa`. From a clean `build/`:

- `make test-rv32-rtl`: 29 tests pass on Icarus; `make test-rv32-rtl-verilator`: the same 29 on Verilator.
- `make run-rv32-rtl` and `make run-rv32-rtl-verilator`: `PASS 807d9fad`, 32,610 identical trace lines, 138,495 cycles unstalled (4.25 per instruction), 179,160 with one stall cycle per request.
- The loop: 78 instructions on both backends, identical traces; 336 cycles unstalled, 102 transfers, and 102 more cycles per stall cycle, unchanged from M3.
- `make lint-rv32` clean, `make synth-rv32` 8,175 cells with no latches: 2,398 cells and 126 flip-flops more than M3 for the shifter, the comparisons, the lane muxes, and the CSRs.
- `make test-rv32` (QEMU, emulator, QEMU differential, RTL tests, the self-check on both simulators, lint, synthesis) passes; counter, ALU, SAP8, and SIMD4 targets unchanged and passing.

**M5**, completed on 2026-09-21 on branch `m5-rv32-devices`: the core unchanged and the machine around it; 36 tests on each simulator, the self-check numbers above unchanged, the diagnostic at 1,666,436 cycles unstalled. Its record is [rv32-soc.md](rv32-soc.md).

Limitations that remain are tabulated above. M6 builds the native window, software drawing, and Pong on this machine.
