# RV32 emulator: design, trace contract, and walkthrough

Our headless emulator for the [RV32 machine](rv32.md). It loads the same firmware image QEMU ran in M1, executes RV32I with the contract's trap, alignment, console, and done-register behavior, and records a retirement trace that the M3 RTL testbench must reproduce. Source: [tools/rv32emu_core.c](../tools/rv32emu_core.c), the machine as a C11 library with no dependencies, and [tools/rv32emu.c](../tools/rv32emu.c), the headless main around it (one file until M6 split it so the [native window](rv32-window.md) could share the core).

## Implementation plan (M2)

1. Measure a tiny interpreter loop in C and Python to pick the language, and probe the host compiler.
2. Loader, RAM, decode, and straight-line execution with hand-computed edge tests.
3. Jumps and branches, including the instruction-address-misaligned trap.
4. Traps, the minimum CSR subset, and the console and done devices, with every "undefined" edge pinned to a fault.
5. Retirement trace; run `selfcheck.elf` and require the same `PASS 807d9fad` QEMU produced; compare the PC sequence with QEMU.
6. Documentation, trace walkthrough, roadmap update.

All six steps are complete; see [Milestone result](#milestone-result).

## Language choice

Roadmap step 1 asked for a short performance and tooling check before choosing the emulator language. The check was a three-opcode fetch/decode/execute loop (`addi`, `add`, `bne`) written the same way in C and in Python, run on the development Mac on 2026-09-19:

| Implementation | Instructions | Wall time | Rate |
| --- | --- | --- | --- |
| C, Apple clang 21, `-O2` | 200,000,000 | 0.159 s | about 1,260 M instructions/s |
| Python 3.14 | 5,000,000 | 3.44 s | about 1.5 M instructions/s |

The C figure is an upper bound: a real emulator adds address decoding, byte-lane merging, device checks, and trace output on every instruction. The Python figure is about what a straightforward Python emulator would achieve. The M1 self-check would run under either, but the first playable computer (M6) needs to clear and redraw a framebuffer many times per second on top of game logic, which a 1.5 M instructions/s core cannot do. The emulator is therefore written in C, compiled by the host `cc` that ships with Xcode's command line tools (`make toolchain-rv32-emu` checks it). The ELF loader stays in Python: `tools/rv32_image.py` already flattens the image, and the emulator reads that flat file, exactly as the RTL testbench will.

The finished emulator, measured the same way on a three-instruction loop (`addi`, `addi`, `bne`) bounded by `--max-instructions 300000000`:

| Mode | Wall time | Rate |
| --- | --- | --- |
| No trace | 0.75 s | about 400 M instructions/s |
| `--trace /dev/null` | 40.2 s | about 7.5 M instructions/s |

Tracing costs a formatted line per instruction, so it is a debugging and comparison mode, not the way to play a game. The self-check's 32,610 instructions take a few milliseconds either way.

## What the emulator is

A **CPU emulator** with device models: it reads instruction words from a byte-addressed RAM, decodes the RV32I encodings, and applies each instruction's architectural effect to 32 registers, a PC, four CSRs, RAM, and the devices (two in M2; since M5 also the timer, the input queue, the display, and the framebuffer). It knows nothing about clocks, wires, or how long anything takes; one instruction is one loop iteration. This differs from:

- A **software VM** (say a bytecode interpreter for a language we might design later) executes an instruction set that exists only in software and was designed for convenient interpretation. Ours executes the instruction set the compiler already targets and that the RTL will implement in gates, so every decision here is checked against the RISC-V specification and against QEMU rather than chosen freely.
- An **RTL simulator** (Icarus or Verilator running `rtl/`) evaluates the logic of a hardware description cycle by cycle: fetch takes cycles, a stalled memory holds a request for cycles, a register write happens at a clock edge. It reports timing; the emulator cannot. The two agree at instruction retirement, which is why the trace below is defined as a contract, and disagree about cycle counts by design.

## Machine model

The [contract](rv32.md) is the specification; this section records how the emulator implements it and the choices M2 had to make where M1 left behavior undefined.

- Registers `x1`–`x31`, `x0` reads as zero and any write to it is discarded before it reaches the register file (and never appears in the trace). PC resets to `0x8000_0000`; every other register resets to zero for determinism, but firmware must not rely on that (the contract says unspecified, and QEMU's mask ROM leaves `a1` nonzero).
- RAM is the contract's 4 MiB at `0x8000_0000`, allocated zero-filled; the 256 KiB slice is a constraint the image checker imposes on the M1 image, not a machine limit. The flat image is loaded at `--base` (default `0x8000_0000`).
- Console `0x1000_0000`: a byte store to +0 goes to stdout; a byte load from +5 returns `0x20`. Every other access to the console's eight bytes (loads from +0, halfword or word accesses, stores to +5) is an access fault.
- Done register `0x0010_0000`: a word store ends the run after the store retires. Byte and halfword stores and any load fault. The word decides the process exit status: `0x5555` exits 0; `(code << 16) | 0x3333` with code 1..255 exits `code`; the reserved `0x7777` and any other word are emulator errors, never a reset.
- Since M5 the memory map is a table of regions (base, size, a load handler, a store handler), the same windows as [rv32_bus.v](../rtl/rv32/rv32_bus.v): the window that holds every byte of the access is found first, then the device decides its offset and width rules; a window without a handler in that direction (the done register cannot be read, the input queue cannot be written) and an address in no window are access faults. Fetches still go straight to RAM. The devices themselves ([contract](rv32.md#devices)): the **timer** at `0x2000_0000` reads the count of executed instructions plus an offset that a write sets, so a load inside instruction N reads N − 1 and a read n instructions after a write of V reads V + n (device time: on the RTL the same register counts cycles); the **input** queue at `0x2000_1000` holds 16 events that `--input FILE` schedules by frame (`frame N down|up KEY`; frame 0's events are queued before the first instruction, later frames' when the present that reaches them is executed), pops one per EVENT read, reports COUNT, and keeps KEYS as events arrive, dropping and reporting on stderr when full; the **display** at `0x2000_2000` counts presents and answers WIDTH and HEIGHT, and a PRESENT store hashes the **framebuffer** (76,800 bytes at `0x3000_0000`, ordinary memory at every width) into a `frame N <hash>` line of `--checkpoints FILE` and, with `--frames DIR`, a binary PPM with the fixed RGB332 mapping.
- Everything outside RAM and those windows is an access fault, for fetches as well as data.
- Instructions: all of RV32I. `FENCE` retires with no effect; `FENCE.I` is illegal. `ECALL` and `EBREAK` trap. Because the trap contract cannot be tested without them, the emulator also implements `csrrw`, `csrrs`, `csrrc`, and their immediate forms for exactly four CSRs, `mtvec` (0x305), `mepc` (0x341), `mcause` (0x342), and `mtval` (0x343), and `mret`, which sets PC to `mepc` (machine mode only, so no privilege or interrupt state changes). Any other CSR number, including `mstatus`, and any other encoding is an illegal instruction. `mtvec` is direct mode only: bits [1:0] always read as zero. `mepc` bits [1:0] read as zero because instructions are word aligned.

### Traps

| `mcause` | Raised by | `mtval` |
| --- | --- | --- |
| 0 | `jal`, `jalr`, or a taken branch whose target is not word aligned (`jalr` clears bit 0 first) | the target |
| 1 | fetch outside RAM | the PC |
| 2 | undefined encoding, unsupported CSR, or unsupported system instruction | the instruction word |
| 3 | `ebreak` | the PC of the `ebreak` |
| 4, 6 | load, store not naturally aligned | the effective address |
| 5, 7 | load, store outside the map or with an unsupported width for a device | the effective address |
| 11 | `ecall` | 0 |

A trapping instruction does not retire: it writes no register and no memory. Trap entry sets `mepc` to the instruction's PC, `mcause`, and `mtval`, and continues at `mtvec`. Misalignment is checked before the address is decoded, so a misaligned access to an unmapped address reports misalignment.

**Double faults.** `mtvec` resets to 0 and the M1 firmware never sets it, so its first trap would fetch from address 0, which is unmapped, and fault again, forever. The emulator stops instead: if a trap is raised before the handler entered by the previous trap has retired a single instruction, the run halts as `double-fault`, the original `mcause`/`mepc`/`mtval` are left intact for the state dump, and stderr reports both traps. A trap taken after the handler has retired at least one instruction is an ordinary nested trap and overwrites the CSRs. QEMU's bare `rv32i` model has no such stop; it spins until the driver's timeout, which is why the M1 record calls a trap "a timeout".

## Command line and outputs

```
build/rv32/rv32emu --image FILE [--base ADDR] [--pc ADDR] [--trace FILE] [--dump-state FILE]
                   [--max-instructions N] [--checkpoints FILE] [--frames DIR] [--input FILE]
                   [--record FILE] [--allow-lost-events]
```

- stdout carries guest console bytes only, exactly like QEMU, so [rv32_run_emu.py](../tools/rv32_run_emu.py) checks the run with the same `classify` function as the QEMU driver.
- stderr ends with one line `rv32emu: halt=<done|double-fault|limit|stopped> steps=N (`stopped` is the window's only) retired=N traps=N loaded=N [done=WORD] <outcome>`. The outcome is `pass`, `fail=<code>`, or `error=<reason>`. `steps` counts executed instructions including trapped ones; `retired` excludes them. Nothing here is a cycle count.
- `--dump-state` writes the final PC, all registers, the four CSRs, the counters, the frame count, the number of queued events, and the halt reason, one `name value` pair per line. The halt line itself did not change in M5.
- `--checkpoints`, `--frames`, and `--input` (M5) are described under the machine model; a malformed script line, an unopenable or unreadable script (a directory opens but does not read), or an unwritable frame file is an emulator error (exit 2), and the checkpoints path may not name the image, the input script, the trace, or the state file. A scripted event the full queue dropped or the guest never reached is reported after the halt line and turns a pass into an emulator error, unless `--allow-lost-events` says it was expected; a guest that failed keeps its own code. An empty image, or one that cannot be read, is refused before the run rather than executed as an illegal instruction at the reset PC.
- `--max-instructions` (default 100,000,000) turns a runaway loop into a `limit` halt.
- `--record FILE` (M6) writes every event offered to the input queue, scripted or typed in the window, as a `frame N down|up KEY` line before the queue decides, so the file is a script that replays the run, drops included. The `stopped` halt (`error=host-stopped`, exit 2) is the window's: the headless emulator never produces it.
- Exit status: 0 for the pass word, the guest's code for a fail word, 2 for any emulator error. Because a guest `FAIL 2` also exits 2, the driver treats the stderr halt line as authoritative and requires `halt=done`.
- Output files are checked before use: the trace, state, checkpoints, and record paths may not name the image, the input script, or each other, and the frames directory may not name the image or the script (same spelling or same inode), and a write error on the console, trace, or state, such as a full disk, makes the run an emulator error even when the guest passed, so a truncated trace is never reported as a completed one. `--max-instructions` accepts only an unsigned decimal, octal, or hex count that fits 64 bits; the Python driver rejects negative values and abandons a run after `--timeout` seconds (default 60).

## Retirement trace contract

`--trace FILE` writes one line per executed instruction. M3's RTL testbench emits the same text so `diff` compares the two backends; the format is therefore fixed here.

```
<step> <pc> <word>[ x<n>=<value>][ mem[<addr>]<-<value>/<width>][ mem[<addr>]-><value>/<width>]
<step> <pc> <word> trap <mcause> <mtval>
```

- `step` is decimal and starts at 1; `pc`, `word`, addresses, and values are eight lowercase hex digits.
- `x<n>=` appears only when a register other than `x0` was written.
- A store records the effective address, the value actually written narrowed to the access width, and the width in bytes: `sb` of `0x1234ffab` shows `<-000000ab/1`. A load records the raw bytes read before sign extension; the extended value is in the register field. The RTL's word-wide bus with byte strobes carries the same information, so its testbench can print this line from the strobe and the data lanes.
- A trap line replaces the effects: nothing was written. For a fetch fault the word is `00000000` because no instruction was read.

Example, the first five lines of the self-check:

```
1 80000000 00040117 x2=80040000
2 80000004 00010113 x2=80040000
3 80000008 00001297 x5=80001008
4 8000000c e3828293 x5=80000e40
5 80000010 00001317 x6=80001010
```

`auipc sp, 0x40` then `addi sp, sp, 0` is `la sp, _stack_top`; the second line rewrites `x2` with the same value, which the trace reports faithfully.

## Walkthrough: reading the self-check trace

Run `make trace-rv32-emu` for `build/rv32/selfcheck.trace` (32,610 lines) and `build/rv32/selfcheck.lst` for the matching disassembly. Step numbers below refer to that trace.

### A call and stack growth: `fib(15)`

`main` calls `fib` for check 5 (listing `0x8000_023c`):

```
553 8000023c 00000097 x1=8000023c        auipc ra, 0          ra = this PC
554 80000240 790080e7 x1=80000244        jalr 0x790(ra)       ra = return address, pc = 0x800009cc
555 800009cc ff010113 x2=8003ffc0        addi sp, sp, -16     open a 16-byte frame
556 800009d0 00112623 mem[8003ffcc]<-80000244/4   sw ra, 12(sp)
557 800009d4 00812423 mem[8003ffc8]<-01000193/4   sw s0, 8(sp)
558 800009d8 00912223 mem[8003ffc4]<-00000000/4   sw s1, 4(sp)
559 800009dc 01212023 mem[8003ffc0]<-80000e30/4   sw s2, 0(sp)
```

The call is two instructions because a 32-bit address does not fit one: `auipc` captures the PC, `jalr` adds the offset the linker computed and writes the return address in the same instruction. The trace shows the value after the `addi`, so on entry `sp` was `0x8003_ffd0`: `main` had already taken 48 bytes below `_stack_top` (`0x8004_0000`). `fib` saves `ra` and the three callee-saved registers it will use, then recurses; step 568 is the next entry, with `sp` = `0x8003_ffb0`. Every level costs 16 bytes; the lowest `sp` in the whole trace is `0x8003_fee0`, fifteen frames below `main`'s `sp`, one for each activation from `fib(15)` down to `fib(1)` (the prologue runs before the `n < 2` test, so even the leaf allocates). The stack grows downward and only the C compiler's frame arithmetic decides by how much; the machine has no notion of a frame.

The first return (step 746 onward) is the mirror image:

```
747 80000a10 00c12083 x1=80000a00 mem[8003feec]->80000a00/4   lw ra, 12(sp)
751 80000a20 01010113 x2=8003fef0                              addi sp, sp, 16
752 80000a24 00008067                                          ret  (jalr x0, 0(ra))
753 80000a00 ffe40413 x8=00000000                              back in the caller
```

`ret` writes nothing (`rd` is `x0`) and the next line's PC is the value loaded into `ra` five lines earlier.

### A signed branch: the sign fix-up in `rv32_div`

Signed division works on magnitudes and negates the quotient when exactly one operand was negative. The listing at `0x8000_0d8c`:

```
80000d8c: xor  a1, a1, a2      a1 = n ^ d
80000d90: bgez a1, 0x80000d98  skip the negation if the signs agreed
80000d94: neg  a0, a0
80000d98: ret
```

`bgez a1` is `bge a1, x0`: taken when `a1 >= 0` as a signed number, so bit 31 alone decides. The trace shows the same instruction going both ways:

```
27689 80000d8c 00c5c5b3 x11=fffffffb     check 14: -7 ^ 2, negative
27690 80000d90 0005d463                  not taken
27691 80000d94 40a00533 x10=fffffffd     quotient negated: -3
...
29751 80000d8c 00c5c5b3 x11=7fffffff     check 17: INT32_MIN ^ -1, positive
29752 80000d90 0005d463                  taken
29753 80000d98 00008067                  ret with the magnitude 0x80000000 unchanged
```

Had the compiler emitted `bgeu`, `0xfffffffb` would compare as 4,294,967,291 ≥ 0 and the branch would always be taken, so check 14 would return 3 instead of −3. Test `test_branch_conditions_at_signed_boundary` pins all six branches at exactly this boundary with `INT32_MIN` against 1.

### What the trace cannot tell you

No line says how long anything took. The `.bss` clearing loop at steps 7–10 is four instructions per word here; on the RTL it will be four instructions and some number of cycles that depends on the memory's `ready` signal. That is the boundary between M2 and M3.

## Verification

- `make test-rv32-emu`: 33 tests in [tests/test_rv32_emu.py](../tests/test_rv32_emu.py). The instruction encoder is the shared [tools/rv32_asm.py](../tools/rv32_asm.py), written from the specification's format diagrams, and every expected value is a hand-computed constant or Python integer arithmetic. Families: arithmetic and compare edges (`0x7fffffff + 1`, `sltiu` against −1, signed versus unsigned `slt`), shifts by 31 and by 33 (five-bit masking), `x0` writes, loads and stores of every width with byte order and sign extension, all six branches at the signed boundary in both directions, `jal`/`jalr` including bit-0 clearing and `rd == rs1`, misaligned jump targets, `ecall` → handler → `mret`, `ebreak`, nine illegal encodings (`mul`, `fence.i`, `mstatus`, `sret`, malformed shifts, unused `funct3` values), CSR masking, 45 memory-fault cases at every window's offsets, widths, and directions and every RAM edge, double faults and nested traps, console output and every done-word outcome, the instruction limit, and load base and start PC options. M5 added the timer (instruction counts, a trapped instruction counts, the write and the wrap), the display and framebuffer at every width with checkpoints against [tools/rv32_devices.py](../tools/rv32_devices.py)'s hash, the PPM frames and an unwritable directory, input events at frames with COUNT and KEYS, the drop of a seventeenth event, the script grammar's errors, and the [diagnostic image](rv32-soc.md#the-diagnostic) when built. M6 added the recording (every offered event, a drop reproduced on replay, an unwritable record refused), the widened aliasing guard, and the [Pong session](rv32-window.md) when built: 200 checkpoints against `pong.expected`, the pinned PASS word, no timer read, and a Q one frame later giving one checkpoint more.
- `make run-rv32-emu`: `selfcheck.bin` prints `PASS 807d9fad`, writes `0x5555`, and exits 0, checked with the same `classify` as the QEMU run. A copy with one byte of `g_init` flipped printed `FAIL 1` and exited 1.
- `make diff-rv32-qemu`: QEMU with `-accel tcg,one-insn-per-tb=on -d exec,nochain` logs one line per instruction. The old log is deleted first and QEMU must exit 0 and produce a log with instructions in RAM, so a failed QEMU run can never pass against stale output; after dropping the mask ROM at `0x1000`, its 32,610 PCs equal ours in order. QEMU logs the guard spin after the done store about 30 more times before its exit request lands; only repeats of that one address are tolerated. Register values are not in QEMU's log; the self-check's 28 checks and checksum compare those.
- `make test-rv32` runs all of the above after the M1 checks; `make test` is still the counter.

## Exercises

Optional experiments on the committed baseline; each is a small edit with a real trade-off, and the tests say whether the machine still agrees with the contract.

1. **Double-fault policy.** `trap()` in `rv32emu_core.c` halts when a handler cannot start. Change it to QEMU's behavior (keep trapping forever until `--max-instructions`) and to a third option, re-entering the handler with `mcause` overwritten. Which one makes a firmware bug easiest to find, and which is closest to what hardware would do? `test_double_fault_halts_with_report` shows what each choice changes.
2. **Branch evaluation.** Rewrite the six-way `switch` under opcode `0x63` as two comparisons (`eq`, `lt`) plus a sign-selection and an inversion bit, the way an ALU does it. Confirm with `test_branch_conditions_at_signed_boundary` that `INT32_MIN < 1` is true signed and false unsigned.
3. **Trace memory line.** The trace shows the narrowed value for `sb`/`sh`. Change it to show the full word and a byte strobe (`mem[80001004]<-00ab0000/0100`) as the RTL bus will present it, update `test_loads_stores_and_byte_order`, and decide which form the RTL testbench should print.
4. **Speed.** Replace the `switch` with a table of function pointers, or cache the decoded fields, and measure with the loop image. Does anything change at 400 M instructions/s, and what does the trace mode cost?

## Milestone result

Completed on 2026-09-19 on branch `m2-rv32-emulator`. `make test-rv32` passes from a clean `build/`:

- `make test-rv32-emu`: 24 tests, about 2 s including compiling the emulator into a temporary directory.
- `make run-rv32-emu`: `PASS 807d9fad`, done word `0x5555`, exit status 0, 32,610 instructions retired, 0 traps.
- `make diff-rv32-qemu`: identical PC sequence for all 32,610 instructions.
- Counter, ALU, SAP8, SIMD4, and all M1 targets unchanged and passing.

Limitations that remained after M2: no timer, input, or framebuffer; no interrupts, `mstatus`, or privilege modes; no cycle timing of any kind; trace mode is about fifty times slower than plain execution. M3 and M4 built the RTL core whose trace matches this emulator's line for line ([record](rv32-rtl.md)); M5 (completed 2026-09-21, [record](rv32-soc.md)) added the timer, input queue, display, and framebuffer here and as RTL peripherals, with the diagnostic image passing on both. M6 (completed 2026-09-21, [record](rv32-window.md)) split this file into a core library and the headless main so a native SDL3 window could run the same machine, and decided against a host-time timer mode: the window paces presents and the timer counts instructions everywhere. Interrupts and privilege modes remain.
