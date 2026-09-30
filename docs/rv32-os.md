# Track 2: a real OS, one step at a time

Track 2 turns the machine's polling runtime into an operating system in five
steps, each with its own evidence: interrupts (O1), a kernel with system calls
and separately linked programs (O2), storage (O3), preemptive multitasking
(O4) and protection (O5). The [plan](planning/track2-os.md) fixed the
contract; [docs/rv32.md](rv32.md#behavior-fixed-in-track-2) holds it now. This
record explains each step and what was run to accept it.

Every hardware change is made twice, in the emulator
([rv32emu_core.c](../tools/rv32emu_core.c)) and the RTL, and compared. Every
new device sits at QEMU `virt`'s address in `virt`'s register layout, so the
programs that exercise it also run on QEMU as an independent reference.

## O1: interrupts

### What changed

| Piece | Emulator | RTL |
| --- | --- | --- |
| `mstatus`, `mie`, `mip`, `mscratch` | `csr_read`/`csr_write` | four registers and the `csr_old` mux in [rv32.v](../rtl/rv32/rv32.v); decode admits the numbers |
| Interrupt entry | checked at the top of `step()` | taken in `FETCH` in place of a fetch not yet presented |
| `wfi` | retires at once | `WFI_WAIT` state until `mip & mie` (cycle ticks) |
| CLINT levels | `mip_now()` | `mtip`, `msip_level` outputs of [rv32_clint.v](../rtl/rv32/rv32_clint.v) |
| PLIC | `plic_load`/`plic_store` | [rv32_plic.v](../rtl/rv32/rv32_plic.v), a new bus slave |
| Input interrupt line | `COUNT` nonzero | `nonempty` output of [rv32_input.v](../rtl/rv32/rv32_input.v) |
| Step ticks | (always) | `step_ticks` input: CLINT and `cycle` count the core's retire/trap pulses |

### Taking an interrupt

The privileged specification lets an implementation take an interrupt at any
instruction boundary. Both backends choose the same one: the boundary before
the next fetch. The emulator checks at the top of `step()`: if `mstatus.MIE`
is set and `mip & mie` is nonzero, it takes the highest-priority one (MEI,
then MSI, then MTI) instead of executing the instruction at `pc`, and the
entry counts as a step.

The RTL does the same in `FETCH`, with one care: a memory request may not
change once it is presented (the [transaction
contract](rv32.md#memory-transaction-contract)). The core therefore decides
only in the first cycle of `FETCH` (`fetch_waiting` is clear): `irq_take`
gates `mem_valid` off in that cycle and runs the trap entry instead, which
costs one cycle. A fetch already waiting on a slow memory completes first.
The trap-entry task that exceptions used gained an `interrupt` input; it sets
`mcause`'s top bit, moves MIE to MPIE and clears MIE, and reports the entry on
the retirement port with a new `trap_interrupt` flag, from which the
testbench prints the trace line `N pc 00000000 interrupt 7`.

`wfi` is the one instruction whose behavior differs by backend. On the RTL
with cycle ticks, `EXECUTE` sends it to `WFI_WAIT`, where it stays until an
enabled level (`mip & mie`, whatever MIE says) appears, then retires; the
interrupt, if MIE is set, is taken at the next boundary with `mepc` after the
`wfi`. On the emulator time is instructions, so a waiting `wfi` would wait
forever: it retires at once. Both are allowed, and both run the same loop,
`while (!done) wfi;`.

### The PLIC

[rv32_plic.v](../rtl/rv32/rv32_plic.v) is a priority encoder and three small
register sets. Pending is combinational, `lines & WIRED & ~claimed`, because
QEMU's PLIC treats our kind of source as level-triggered and drops a request
whose line falls. The claim is a loop over 31 sources that keeps the first
strictly greater priority, so the lowest number wins a tie, starting from the
threshold, so only priorities above it count. A read of the claim register
marks the answer claimed at acceptance; writing the number back while it is
enabled clears the mark. `meip` is `best != 0`.

Only the input queue is wired (source 12, the first number `virt` leaves free
after its RTC at 11); O3 will wire virtio-blk as source 1, `virt`'s number for
its first virtio slot. The device tree now describes the wiring the way
QEMU's does: the hart has a `riscv,cpu-intc` (phandle 1), the CLINT lists
`interrupts-extended = <1 3 1 7>`, the PLIC `<1 11>` and `riscv,ndev = 31`,
and the input node `interrupt-parent = <2>; interrupts = <12>`. `fdt_cell`
([fdt.c](../programs/rv32/fdt.c)) reads such a cell, and the map checker now
requires our PLIC to equal `virt`'s.

### Device time and step ticks

A timer interrupt fires when `mtime` reaches `mtimecmp`, and `mtime` counts
cycles on the RTL and instructions on the emulator, so the interrupt lands on
a different instruction on each. Two comparisons result:

- **Results** (cycle ticks, the default): console, outcome, checkpoints and
  exception records must agree. Interrupt lines are left out of the records.
- **Traces** (step ticks, `--ticks steps`): the testbench's `+ticks=steps`
  drives the SoC's `step_ticks` input. The CLINT then advances on the core's
  registered `retire || trap` pulse instead of every cycle, and the core's
  `cycle` counter does too. The pulse is high in the first `FETCH` cycle
  after a step, and the count of that cycle already includes it
  (`mtime = elapsed + pulse`), so the interrupt check in that cycle sees the
  same `mtime` as the emulator's check before the next step, and every load
  or CSR read sees the steps before its instruction. `wfi` does not wait.
  Timer reads and interrupts then happen at the same instruction on both
  backends, and the whole trace, interrupt lines included, is compared.

The input interrupt needs no special mode: events arrive with the present
that reaches their frame on both backends, and the testbench's first push
lands before the next instruction's first `FETCH` cycle, where the
combinational pending line reaches `meip` in time.

### irqcheck

[irqcheck.c](../programs/rv32/irqcheck.c) finds the CLINT and PLIC in the
tree, installs [trap_entry.S](../programs/rv32/trap_entry.S) and checks the
registers, the gating (a pending timer interrupt is not taken while `mie` or
MIE is clear, the handler sees MIE clear and MPIE set, `mret` restores MIE), a
software interrupt, MSI before MTI, a `wfi` loop woken by a timer interrupt
2,000 ticks ahead, and the PLIC's registers. Where the tree lists our input
device it also masks the input with the threshold, then takes MEI before MSI,
claims source 12, drains the scripted events and completes. On QEMU:

```
irqcheck: registers ok
irqcheck: gating ok
irqcheck: software ok
irqcheck: priority ok
irqcheck: wfi ok
irqcheck: plic ok
irqcheck: keyboard absent
PASS 133cab46
```

On our backends the keyboard line is `irqcheck: keyboard source 12 events 2`.
The PASS word folds only what every platform shares.

### Evidence

Run on 2026-09-30 in a Linux container (x86-64): Ubuntu clang and lld 18.1.3,
QEMU 8.2.2, Icarus 13.0 and Verilator 5.040 built from their release tags,
Yosys 0.33.

- **irqcheck:** `PASS 133cab46` on QEMU `virt` (`-cpu rv32`, transcript equal
  to the pinned one), the emulator (56,553 steps, 7 interrupts), Icarus
  (231,595 cycles) and Verilator with a stall per request (297,542 cycles),
  results-identical with cycle ticks; the RTL's `wfi` waits, so it retires
  54,579 instructions against the emulator's 56,553. In step-tick mode on
  Verilator with seeded random stalls the traces are identical, 56,553 lines
  including the 7 interrupt lines.
- **Directed tests:** `test-rv32-irq`, 11 programs on the emulator and
  Verilator in step-tick mode, each compared trace for trace and against
  values written by hand: the CSRs' reset values and writable bits; a timer
  interrupt taken at the next boundary with `mepc`, `mstatus` in the handler
  and after `mret`; MSI before MTI; an input interrupt claimed, drained and
  completed; the PLIC's registers, faults and claim/complete protocol; `mtime`,
  `time` and `cycle` reading the steps before their instruction; `wfi`
  retiring once on the RTL with cycle ticks and spinning on the emulator; an
  interrupt into an unmapped handler as a double fault; and interrupts under
  seeded stalls.
- **Earlier results:** every earlier image keeps its PASS word and its
  trace; the architectural tests' `csrs mstatus` now retires on every backend
  as on QEMU, so the model's handler skips nothing and fails on any trap.

## O2: a kernel and system calls

### The layout

| Range | What |
| --- | --- |
| `0x8000_0000`–`0x800F_FFFF` | The kernel: code, data, the RAM disk, a 16 KiB stack at the top ([kernel.ld](../programs/rv32/os/kernel.ld)) |
| `0x8010_0000 + 0x4_0000 × n`, n = 0..11 | Program slot n: code, data and `.bss` from the bottom, the heap above them, a 32 KiB stack at the top ([user.ld](../programs/rv32/os/user.ld)) |

Each program is linked for its own slot (`--defsym SLOT_BASE=...`), so any
set of programs can be resident at once with no relocation and no MMU; the
price is that one program cannot run twice at the same time, which `spawn`
refuses. [tools/rv32_ramdisk.py](../tools/rv32_ramdisk.py) checks the rules
(one load segment at a slot base, entry at the base, room for the stack, one
program per slot) and packs the programs into a RAM disk that
[kentry.S](../programs/rv32/os/kentry.S) includes with `.incbin`. An entry's
flag says the program drives the accelerators itself (only the menu); the
kernel waits for their engines before that program's present, and O5 will
grant it their windows.

### Contexts and traps

Every context the machine runs, each process and the kernel's idle loop, has
a `struct frame`: the 31 registers, the pc and `mstatus`. While a context
runs, `mscratch` holds its frame's address. The trap vector swaps `sp` with
`mscratch`, stores every register and `mepc`/`mstatus` into the frame,
switches to the kernel's one stack and calls `kernel_trap(frame)`; whatever
frame that returns, `kernel_resume` loads into `mscratch`, `mepc`, `mstatus`
and the registers, and `mret` runs it. A process's `mstatus` holds MPIE set,
so it always runs with interrupts on; the kernel always runs with them off
(trap entry clears MIE) and never traps itself, so a kernel bug is a double
fault and stops the machine with both traps reported, as the contract has
done since M2.

The idle context is a frame whose pc is `kernel_idle: wfi; j kernel_idle` on a
small stack. When nothing is ready, `schedule()` returns it; an interrupt
brings the kernel back.

### System calls

`ecall` with the number in `a7` and arguments in `a0`–`a5`; the result comes
back in `a0` ([sys.h](../programs/rv32/os/sys.h)):

| Call | Does |
| --- | --- |
| `exit(code)` | Ends the process; a waiting parent gets the code |
| `write(fd, buf, len)`, `read(fd, buf, len)` | The console: fds 1 and 2 out, fd 0 in; `read` waits for at least one byte |
| `event()`, `keys()` | The next input event from the kernel's buffer, and the held-key mask |
| `present()`, `display()` | Show the framebuffer; its address, or 0 on a platform without a display |
| `sbrk(n)` | Grow the heap; it stops below the stack |
| `spawn(name, args)`, `wait(pid)`, `list(i, buf, len)` | Run a program from the RAM disk, wait for a child, list the RAM disk |
| `yield()`, `sleep(ticks)`, `time()`, `getpid()`, `halt(code)` | Scheduling and time; `halt` stops the machine |

Every pointer must lie inside the caller's slot, and every call that cannot
be served returns `0xffff_ffff`. [syscheck.c](../programs/rv32/os/syscheck.c)
checks those refusals and the heap, process and list calls on every backend.

**Blocking is by retry.** A call that cannot finish yet (`read` with no byte
waiting, `wait` for a child still running) leaves the pc on the `ecall` and
marks the process blocked. `wake_blocked()` runs before every scheduling
decision and makes a process ready when its condition may hold; the `ecall`
then runs again and finds its byte or its zombie child. No call has to be
resumed halfway, so the kernel keeps no per-call state beyond the reason a
process sleeps. A timer interrupt every 10,000 device ticks makes sure a
blocked reader is looked at even while the machine idles (the console raises
no interrupt).

### Input, display and faults

The kernel owns the input queue: the PLIC interrupt handler (source from the
tree's `interrupts`) drains `EVENT` into a 64-event buffer, and `event()`
pops it. Events still arrive with the present that reaches their frame, and
the interrupt is taken right after that present's `ecall` returns, so a
program sees exactly the sequence the bare image saw. The framebuffer is
left to the programs, which get its address from `display()`; on QEMU it is 0
and Pong says `pong: no display`.

A program that faults is killed: the kernel prints
`kernel: pid 9 fault killed: cause 5 at 802c0074 tval 00200000`, the exit
code is 128 plus the cause, and the shell carries on. Nothing stops a program
from writing kernel memory yet; that is O5.

### Programs

| Slot | Program | What |
| --- | --- | --- |
| 0 | [sh](../programs/rv32/os/sh.c) | The shell, pid 1: `ls`, `NAME [ARGS]`, `NAME &`, `wait`, `halt [CODE]`; it echoes each line, since the host does not |
| 1 | [hello](../programs/rv32/os/hello.c) | Its pid and arguments |
| 2 | [primes](../programs/rv32/os/primes.c) | A sieve on `sbrk` memory |
| 3 | [pong](../programs/rv32/os/pong.c) | [pong.c](../programs/rv32/pong.c) on system calls |
| 4 | [tetris](../programs/rv32/os/tetris.c) | The M7 Tetris, Q quits |
| 5 | [menu](../programs/rv32/os/menu.c) | The capstone runtime (menu, games, 2D, 3D, digit screen) on system calls, accelerators direct |
| 6 | [syscheck](../programs/rv32/os/syscheck.c) | System-call edge cases |
| 7 | [fault](../programs/rv32/os/fault.c) | A load from an unmapped address, or an illegal instruction |

The user library ([ulib.c](../programs/rv32/os/ulib.c)) also defines
`console.h`'s functions and `rv32_exit` on top of system calls, so the game
and demo sources link unchanged.

### Three platforms

The kernel finds its devices only through the tree: the console
(`tiny-processors,console`, else `ns16550a`), the done register
(`sifive,test0`), the CLINT and PLIC, and our input, display and engines where
listed. QEMU puts its tree at `0x8020_0000`, inside slot 4, so the kernel reads
everything it needs (including the model string) before loading the first
program. The same `kernel.elf` then runs the console session on QEMU `virt`:

```
kernel: riscv-virtio,qemu, hart 0
kernel: devices console clint plic
kernel: 8 programs
sh: ls, NAME [ARGS] [&], wait, halt [CODE]
$ ls
...
$ fault load
kernel: pid 9 fault killed: cause 5 at 802c0074 tval 00200000
sh: fault exited 133
...
$ halt
kernel: halt, 10 exits
PASS 34b7bb53
```

and our backends print the same lines but the first two. The PASS word is a
sum over every process that exited of a hash of its name xor its exit code:
a sum, so the order processes finish in does not change it (O4).

### Evidence (O2)

- **Console session** ([session.txt](../programs/rv32/os/session.txt): `ls`,
  `hello` with and without arguments, `primes`, a failing `primes 2`, an
  unknown name, `syscheck`, both `fault`s, `hello` again, `halt`):
  `PASS 34b7bb53` on QEMU `virt` (transcript pinned in
  [session.qemu.expected](../programs/rv32/os/session.qemu.expected)), the
  emulator (1,034,560 steps) and Verilator with a stall per request
  (7,326,810 cycles), results-identical over 45 console lines and 303
  exception records; in step-tick mode with seeded stalls the emulator and
  Verilator traces are identical (`test-rv32-os`).
- **Pong from the shell:** the standalone image's 200 checkpoints and
  `PASS 8fef54bc`, with the kernel's timer and keyboard interrupts in between;
  trace-identical between the emulator and Verilator in step-tick mode,
  1,421,567 lines.
- **The menu from the shell:** S1's 185-frame session gives S1's 185
  checkpoints and `PASS c76cd363` on the emulator and on Verilator with seeded
  waits on CPU memory, the graphics engines and SIMD4 (187,160,127 cycles);
  M7's capstone session gives its checkpoints and `PASS ea60197e` on the
  emulator.
- **Tools:** `test-rv32-os` checks the RAM disk round trip and its refusals
  (two programs in one slot, one name twice, a name too long, an unknown
  accelerator program, an image outside the slots, four malformed disks) and
  the console's receive side on both backends, trace for trace.

## Exercises (O2)

1. **Blocking by retry.** Run the console session with `--trace` and find a
   `read` ecall that executes more than once. What changed between the two
   executions, and which function made the process ready again?
2. **Where the tree is.** Link `tetris` at slot 4 and run the session on QEMU
   with the kernel's `discover()` moved after the first `spawn`. Which line
   of the banner goes wrong, and why only on QEMU?
3. **A second shell.** Try `sh` from the shell. Which check in `spawn()`
   refuses it, and what would it take to run two programs linked for the same
   slot?
4. **The input buffer.** Make `KEY_BUFFER` 4 and replay Pong's session. Which
   events are lost, and why does the standalone Pong not have this problem?

## Exercises (O1)

1. **Where does it land?** Run `make run-rv32-irq-rtl-verilator` and
   `run-rv32-irq-rtl-steps`, then find the first `interrupt 7` line in each
   RTL trace and in the emulator's. Why do the step-tick and emulator lines
   agree, and why does the cycle-tick one not?
2. **A fetch in flight.** In [rv32.v](../rtl/rv32/rv32.v), what would go wrong
   if `irq_take` ignored `fetch_waiting`? Run the directed tests with
   `--seed` stalls after removing it and read the testbench's complaint.
3. **Level or edge.** Make the PLIC latch pending instead of following the
   line. Which `irqcheck` step behaves differently if the handler drains the
   queue without claiming?
4. **Priority.** Give the input priority 1 and the threshold 1. Where does the
   specification say this masks the source, and which line of `rv32_plic.v`
   implements it?
