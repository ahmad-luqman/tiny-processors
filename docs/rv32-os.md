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
tree, installs [trap.S](../programs/rv32/trap.S) and checks the
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
| `0x8010_0000 + 0x2_0000 × n`, n = 0..23 | Program slot n of 128 KiB (256 KiB, 12 slots, until O4); a program spans one or more: code, data and `.bss` from the bottom, the heap above them, a 32 KiB stack at the top ([user.ld](../programs/rv32/os/user.ld)) |

Each program is linked for its own slot (`--defsym SLOT_BASE=...`), so any
set of programs can be resident at once with no relocation and no MMU; the
price is that one program cannot run twice at the same time, which `spawn`
refuses. [tools/rv32_ramdisk.py](../tools/rv32_ramdisk.py) checks the rules
(one load segment at a slot base, entry at the base, room for the stack, one
program per slot) and packs the programs into a RAM disk that
[kentry.S](../programs/rv32/os/kentry.S) includes with `.incbin`; the
kernel checks the table again at boot, since `spawn` trusts it. An entry's
flag says the program drives the accelerators itself (only the menu). While
G1 or G2 is busy it owns the framebuffer, so only such a program is scheduled
then and a `present` from anyone waits for the engines; O5 grants the flagged
program the engines' windows.

### Contexts and traps

Every context the machine runs, each process and the kernel's idle loop, has
a `struct frame`: the 31 registers, the pc and `mstatus`. While a context
runs, `mscratch` holds its frame's address. The trap vector swaps `sp` with
`mscratch`, stores every register and `mepc`/`mstatus` into the frame,
switches to the kernel's one stack and calls `kernel_trap(frame)`; whatever
frame that returns, `kernel_resume` loads into `mscratch`, `mepc`, `mstatus`
and the registers, and `mret` runs it. A process's `mstatus` holds MPIE set,
so it always runs with interrupts on; the kernel always runs with them off
(trap entry clears MIE) and must never trap itself. Trap entry sets
`mscratch` to 0 once the frame is saved, and `kernel_resume` sets it to the
next frame, so a trap that finds 0 there is a fault in the kernel: the vector
takes the kernel's stack back and `kernel_fault()` prints the cause, pc and
`mtval` and halts with code 254, instead of saving registers through a user
stack pointer into a frame that is not there. (The machine's double-fault
rule catches only a handler that faults before its first instruction
retires, so it would not.)

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
`kernel: pid 9 fault killed: cause 5 at 80200074 tval 00200000`, the exit
code is 128 plus the cause, and the shell carries on. Nothing stops a program
from writing kernel memory yet; that is O5.

### Programs

| Slot (since O4) | Program | What |
| --- | --- | --- |
| 0 | [sh](../programs/rv32/os/sh.c) | The shell, pid 1: `ls`, `NAME [ARGS]`, `NAME &`, `wait`, `halt [CODE]`; it echoes each line, since the host does not |
| 1 | [hello](../programs/rv32/os/hello.c) | Its pid and arguments |
| 2 | [primes](../programs/rv32/os/primes.c) | A sieve on `sbrk` memory |
| 3 | [pong](../programs/rv32/os/pong.c) | [pong.c](../programs/rv32/pong.c) on system calls |
| 4 | [tetris](../programs/rv32/os/tetris.c) | The M7 Tetris, Q quits |
| 5–6 | [menu](../programs/rv32/os/menu.c) | The capstone runtime (menu, games, 2D, 3D, digit screen) on system calls, accelerators direct |
| 7 | [syscheck](../programs/rv32/os/syscheck.c) | System-call edge cases |
| 8 | [fault](../programs/rv32/os/fault.c) | A load from an unmapped address, or an illegal instruction; since O5 also kernel and other-slot accesses and a machine CSR |
| 9, 10, 11 | [cat](../programs/rv32/os/cat.c), [write](../programs/rv32/os/write.c), [files](../programs/rv32/os/files.c) | Print a file, write one, list them (O3) |
| 12, 13 | [bars](../programs/rv32/os/bars.c), [life](../programs/rv32/os/life.c) | Two programs that share the screen (O4) |
| 14 | [fill](../programs/rv32/os/fill.c) | Numbered lines into a file, to a given size (after O5's review) |

The user library ([ulib.c](../programs/rv32/os/ulib.c)) also defines
`console.h`'s functions and `rv32_exit` on top of system calls, so the game
and demo sources link unchanged.

### Three platforms

The kernel finds its devices only through the tree: the console
(`tiny-processors,console`, else `ns16550a`), the done register
(`sifive,test0`), the CLINT and PLIC, and our input, display and engines where
listed. QEMU puts its tree at `0x8020_0000`, inside slot 8 (slot 4 of O2's
256 KiB slots, where `tetris` was linked then), so the kernel reads
everything it needs (including the model string) before loading the first
program. The same `kernel.elf` then ran O2's console session on QEMU `virt`
(the addresses and the count have moved with later steps; the pinned
transcripts are current):

```
kernel: riscv-virtio,qemu, hart 0
kernel: devices console clint plic
kernel: 8 programs
sh: ls, NAME [ARGS] [&], wait, halt [CODE]
$ ls
...
$ fault load
kernel: pid 9 fault killed: cause 5 at 80200074 tval 00200000
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

## O3: storage

### The device

[rv32_virtio_blk.v](../rtl/rv32/rv32_virtio_blk.v) is a virtio-mmio version 2
block device at `virt`'s first virtio slot, so one driver serves it and
QEMU's; [docs/rv32.md](rv32.md#virtio-blk-at-0x1000_1000) has the register
table. The interesting part is how it reaches RAM. A virtio device is a bus
master: it reads the driver's rings and descriptors and moves the data
itself. Ours does all of that while the notify store that started it waits.
The bus contract lets any device hold `ready` low, so the device keeps the
CPU's store pending, and since the CPU is then presenting nothing to RAM, the
SoC hands the CPU's side of the RAM port to the device (`side_valid` and its
fellows in [rv32_soc.v](../rtl/rv32/rv32_soc.v)); the arbitration with the
graphics engines, which already alternated between the CPU side and the
engines, needs no change. When the last available request is served the
device lets the store be accepted. The testbench's stall generator never sees
a DMA access, but it sees the notify wait: the cycle formula still holds, with
the DMA cycles counted as that store's stalls.

The device is a state machine of one access at a time: read the available
index, read a ring entry, read three descriptors of four words each, read the
header's type and sector, copy the data a word per access (the disk is a
memory inside the device, one word per cycle), write the status byte with a
byte strobe, write the used element, write the used index with a halfword
strobe, and go round again. Any address outside RAM, and any chain that is
not header, data, status with the right NEXT and WRITE flags, stops it with
DEVICE_NEEDS_RESET. The emulator's `virtio_request()` performs the same reads
in the same order, so the two agree even on which failure they report.

### The kernel's driver and file system

[virtio.c](../programs/rv32/os/virtio.c) sets the device up as the
specification's driver initialization says (reset, ACKNOWLEDGE, DRIVER,
`VIRTIO_F_VERSION_1`, FEATURES_OK, one queue of 8, DRIVER_OK) and sends one
request at a time: three descriptors, one ring entry, a notify, then a wait
for the used index. On our machine the wait is already over when the notify
returns; on QEMU the request completes a little later; the loop is the same.
The kernel polls: the device's interrupt (PLIC source 1) is there for a
driver that sleeps, and `virtiocheck` tests it.

The file system, `tfs` ([fs.c](../programs/rv32/os/fs.c),
[rv32_mkfs.py](../tools/rv32_mkfs.py)), is the smallest that still is one: a
superblock, one directory sector of sixteen 32-byte entries (name, first
sector, capacity, size), and one contiguous extent per file, allocated at
creation after the last extent in use with a capacity of 8 sectors. Writing a
file replaces its contents from the start; the new size reaches the disk when
the file is closed (or its process ends). Every transfer goes through one
sector buffer, so a partial sector is read, changed and written back.
Descriptors 3 to 6 of each process are its open files; `open`, `close`,
`files`, and `read`/`write` on those descriptors are the calls.

Pong and Tetris record their best scores in a file, `scores`, through
[score.c](../programs/rv32/os/score.c): Pong keeps the winning side's points,
Tetris its score.

### The kernel finds the disk

On QEMU the tree lists all eight of `virt`'s virtio slots and most are empty
(DeviceID 0); ours lists the one we have. The kernel walks the `virtio,mmio`
nodes with `fdt_find_nth` (the reader's new query for the nth matching node)
and takes the first whose DeviceID is 2.

### Evidence (O3)

- **virtiocheck** ([virtiocheck.c](../programs/rv32/virtiocheck.c)): set up;
  sector 3 out and back; `InterruptStatus` and the PLIC's pending bit until
  acknowledged; two requests served by one notify; a read past the disk is
  IOERR and type 6 is UNSUPP (and, since O5's review, a two-sector write from
  the last sector is IOERR and leaves it blank, and a descriptor index past
  the queue's size needs a reset). `PASS 16fc0878` (`edec4a52` before the
  review added the crossing write) on QEMU `virt`, the emulator,
  Icarus and Verilator (with seeded stalls on the CPU's bus and the graphics
  port), the traces identical and every backend's final disk the same
  131,072 bytes as QEMU's. On our machine it also checks what QEMU does
  differently: a misaligned buffer is IOERR, a broken chain and a queue
  outside RAM set DEVICE_NEEDS_RESET and a reset recovers, and four register
  misuses fault. (Type 5 was the first choice for "unknown"; QEMU masks the
  OUT bit off the type and served it as a flush.)
- **The console session** now also lists the disk's files, prints
  `welcome`, writes `note` and reads it back, and fails to `cat` a missing
  file: `PASS 8409efe3` on QEMU `virt` with a copy of the same disk image, the
  emulator and Verilator, and the three disks are identical afterwards.
- **Scores survive a boot:** the Pong session ends with `pong: best 1` and
  `cat scores`; a second run of the kernel on the disk the first left (the
  emulator's and the RTL's, found identical) lists `scores` and prints
  `pong 1`, and `rv32_mkfs.py --cat scores` reads the same line on the host.
  In step-tick mode the Pong session, disk writes included, is
  trace-identical between the emulator and Verilator (1,507,642 lines).
- **Cost:** the device is 5,153 generic cells with its disk shrunk to 64
  words (Yosys 0.33), latch-free.

## O4: preemptive multitasking

### The scheduler

O2's kernel already had everything a round-robin scheduler needs: every
context's registers live in its frame, `kernel_trap` returns whichever frame
should run next, and `next_ready()` walks the process table from the one after
the current process. O4 adds one rule to the timer interrupt: when another
process is ready, the running one becomes ready too, goes to the back of the
round, and counts a switch. A process that blocks (reading the console,
waiting for a child, sleeping) gives the machine up at once, as before; one
that computes now loses it at the next tick. `yield`, `sleep` and `wait` are
unchanged. `switches()` tells a program how often the timer took the machine
from it, and `ps(i, buf, len)` describes process table entry `i`.

The quantum is 10,000 device ticks where ticks have no rate (our machine's
tree has no `timebase-frequency`, deliberately) and 100 µs where the tree
gives one: QEMU's 10 MHz timebase makes that 1,000 ticks. The kernel reads it
with the FDT reader's new `"@name"` query (the `/cpus` node's own property).
Without that, QEMU runs the demo programs so fast that a 1 ms quantum might
never interrupt them.

### Slots

Twelve 256 KiB slots were all taken by the end of O3. Slots are now 128 KiB
(24 of them), and a program may span several: its link script's
`SLOT_SPAN`, recorded in the RAM disk entry, sets where its stack starts. The
menu, the only program larger than 96 KiB, spans two. `spawn` refuses a
program whose span overlaps a live process's.

### Two programs, one machine

[bars](../programs/rv32/os/bars.c) animates bands of colour in the left half
of the screen for 24 frames; [life](../programs/rv32/os/life.c) runs Conway's
Life on a 20 × 30 torus in the right half for 16 generations. Both present
every frame, so in the window you see the halves move together as the timer
passes the machine between them. The session is

```
$ bars &
[2]
$ life &
[3]
$ wait
$ cat bars.out
bars: 24 frames, sum 6bf3c4c5, preempted yes
$ cat life.out
life: 16 generations, population 31, sum 63c97f8a, preempted yes
```

Each program writes its line to a file ([report.c](../programs/rv32/os/report.c))
rather than the console, because two programs' console lines would interleave
differently on every backend, and the shell prints the files after `wait`.
The files are already on the disk image, so creating them cannot depend on
the order either; the directory is written last with both sizes, so the disks
end identical too. The sums fold only what each program drew or computed, and
"preempted yes" says the timer took the machine from it at least once.

With cycle ticks the frames each backend presented are mixes of the two
programs' work at different moments, so the runner compares only how many
there were (`--compare-checkpoints count`, 40). In step-tick mode the timer
fires on the same instruction on both backends, so the whole trace, every
switch and every frame's hash, is compared.

Preemption also changes what results comparison can ask. With cycle ticks the
order of two processes' exceptions depends on when the timer fired, and so
does the number of a single process's system calls: a blocked `read` or
`wait` runs its `ecall` again, and whether `wait` blocks at all depends on
whether the child has already finished. What cannot change is each process's
own sequence of faults. The runner therefore groups exception records by the
128 KiB region of their PC (a slot, so a process) and, with
`--compare-traps faults`, leaves environment calls out; the kernel sessions
use both. The default comparison is unchanged for everything else.

### Evidence (O4)

- **Two programs:** the `bars &`, `life &`, `wait` session gives the same
  transcript and `PASS 408a6738` on QEMU `virt` (three runs, "preempted yes"
  each time), the emulator (6,560,282 steps, 626 interrupts) and Verilator
  with a stall per request (39,935,033 cycles), with 40 presents each and
  identical final disks. In step-tick mode with seeded stalls the emulator
  and Verilator traces are identical, 6,560,282 lines, every switch and all
  40 frame hashes included.
- **Earlier sessions under preemption:** the console session, Pong (with its
  200 checkpoints, trace-identical in step-tick mode), the second boot and the
  S1 menu session keep their results; only the fault addresses in the console
  transcript moved with the slots.
- **Tools:** `test-rv32-os` checks the per-region and faults-only trap
  comparison and the checkpoint count on made-up runs, and the RAM disk's
  spans.

## O5: protection

### Two modes

Until O5 every program ran in machine mode, so the kernel's system call
checks were a courtesy: any program could write the kernel's memory, the
process table or another program's slots, or turn interrupts off. O5 adds the
two things the privileged specification needs to stop that, on the emulator
and the RTL: user mode, and physical memory protection
([the contract](rv32.md#o5-protection)).

User mode costs the core two bits, the current mode and `mstatus.MPP`. A trap
saves the mode in MPP and enters machine mode; `mret` enters the mode MPP
names and leaves MPP at user. In user mode `ecall` is cause 8 instead of 11,
and a machine CSR, `mret` or `wfi` is an illegal instruction (the counters
too, unless `mcounteren` allows them). Interrupts are always enabled there:
a less privileged mode must not be able to mask the machine's interrupts, and
that is what makes the kernel's timer inescapable.

PMP is one combinational checker in [rv32.v](../rtl/rv32/rv32.v). In FETCH it
looks at the PC and, if the fetch is refused, the core takes the fault in
place of presenting the request, the way it takes an interrupt: the bus never
sees the address. In EXECUTE it looks at the data address, after the
misalignment check and before MEM, so a refused load or store is an access
fault with the address in `mtval` and nothing reaches a device. Eight entries
cover what the kernel needs with room to spare; the checker walks them in
order and the first that matches decides. A locked entry binds machine mode
too and ignores writes until reset, which the directed tests exercise though
the kernel does not use it.

### The kernel's side

Programs now start with MPP 0, so the `mret` in `kernel_resume` enters user
mode; a trap saves the frame's `mstatus` with the mode it came from, and the
idle loop keeps MPP 3 and stays in machine mode, where `wfi` is legal.
Before resuming a process the kernel points PMP at it, in three TOR pairs:

| Entries | Region | Access |
| --- | --- | --- |
| 0, 1 | the process's slots, `base` to `base + span` | read, write, execute |
| 2, 3 | the framebuffer, from the tree | read, write |
| 4, 5 | the accelerators' windows, SIMD4 to G2, from the tree | read, write, only for a program flagged `accelerators` (the menu) |

Nothing else matches, so every other address, the kernel's MiB, other slots,
the console, the CLINT, the PLIC and the disk, is refused in user mode. The
kernel rewrites the entries only when a different process is about to run.
It runs in machine mode with no locked entry, so PMP never stops it: system
calls still copy to and from the caller's memory after `user_range()` has
checked the pointer. `mcounteren` is 7, so programs may read the counters; on
a hart with S-mode (QEMU's) a user counter read also needs `scounteren`, so
the kernel writes that too, with `mtvec` pointed past the write for the
moment, since on our machine the CSR does not exist and the write traps.

Entries 4 and 5 are a hole PMP cannot close. G1 and G2 read and write RAM by
DMA wherever their registers point (G2's depth buffer, G1's blit source), and
PMP holds only the CPU, so the menu could reach the kernel or another slot
through the engines. A program flagged `accelerators` is therefore trusted,
as a driver would be; closing the hole would take the kernel starting every
engine job itself, or bounds in the engines.

A fault in user mode was already fatal to the process (O2); now there are
more ways to earn one. [fault.c](../programs/rv32/os/fault.c) gains five:

```
$ fault kernel
kernel: pid 11 fault killed: cause 7 at 80200074 tval 80000000
sh: fault exited 135
$ fault shell
kernel: pid 12 fault killed: cause 7 at 80200094 tval 80100000
sh: fault exited 135
$ fault csr
kernel: pid 13 fault killed: cause 2 at 80200108 tval 30002573
sh: fault exited 130
$ fault read
kernel: pid 14 fault killed: cause 5 at 80200114 tval 80000000
sh: fault exited 133
$ fault exec
kernel: pid 15 fault killed: cause 1 at 80000000 tval 80000000
sh: fault exited 129
```

A store to the kernel's first word and one to the shell's slot are store
access faults at the address, and `csrr a0, mstatus` is an illegal
instruction with the instruction in `mtval`; `fault read` is a load from the
kernel and `fault exec` a jump to the kernel's `_start`, a fetch fault with
the pc and `mtval` both `0x8000_0000`. QEMU `virt`, whose
hart has PMP too, prints the same lines. `fault load` still faults at
`0x0020_0000`, but PMP now refuses it before the bus decoder could.

Programs are built for user mode too: their images are checked with
`--allow-user`, which admits `ecall`, `unimp` and counter reads and none of
the machine's CSRs or `mret`/`wfi`; only `fault` is checked with
`--allow-system`, since it reads `mstatus` on purpose.

### Hardening after review

A review of the whole track found places where the kernel trusted what it
should check, or said nothing when something failed. Each is fixed and
covered:

- **Busy engines.** A present, or a CPU store to the framebuffer, faults while
  G1 or G2 owns it. The kernel now schedules only a program flagged
  `accelerators` while an engine is busy, and every `present` waits (runs
  its `ecall` again) until the engines are idle, so no process can be killed
  for another's engine work and the kernel's own store cannot fault.
- **Faults in the kernel** are caught at trap entry (above, "Contexts and
  traps") and reported by `kernel_fault()` instead of corrupting memory.
- **Processes.** A finishing process frees its zombie children, which nobody
  could wait for any more. `halt` writes the directory back when a file is
  still open for writing, so its size is not lost, and a failed write-back
  when a process finishes is reported. It also reports input events the full
  buffer dropped.
- **Files.** One writer per file and no readers while it is written (opening
  for writing truncates); `open` finds a free descriptor before it creates a
  file, so a refused open leaves nothing behind. A device error is an error:
  `read` and `write` return `0xffff_ffff` rather than a partial count that
  looks like the end of the file, `cat` says so, a game's score is not
  written from a table it could not read, and `bars` and `life` exit nonzero
  when their report cannot be written. `fs_write` counts only bytes whose
  sector reached the disk, `fs_mount` refuses a file system larger than the
  device, and a corrupt directory entry is set aside (and reported at boot)
  but written back unchanged rather than erased.
- **The driver** reads the used ring with a fence before the status byte and
  the data (QEMU's device completes asynchronously), gives up on a request
  after a bound, and reports once that the disk is disabled when the device
  needs a reset.
- **The devices.** Both virtio-blk models refuse a descriptor index at or past
  QueueNum; the emulator's file follows its in-memory disk when a DMA fault
  stops an OUT, and a failed host write reaches the guest as IOERR.
- **Programs.** `write` refuses a name longer than 19 bytes instead of moving
  the rest into the contents, the shell refuses an over-long line and a halt
  code that is not a number, and the kernel checks the RAM disk's table at
  boot.

The new [fill](../programs/rv32/os/fill.c) program (slot 14) writes numbered
lines, so the console session now covers a file of two sectors, rewriting a
file, and a 5,000-byte write stopped at the 4 KiB capacity; `syscheck`
refuses kernel, straddling and wrapping buffers for every call that writes
into the caller's memory.

### Evidence (O5)

- **Directed tests:** `make test-rv32-pmp` runs seven programs (user-mode
  entry and `ecall`, the instructions and CSRs user mode may not use and the
  `mcounteren` gate, every PMP address mode with the permission bits and entry
  priority, locked entries holding machine mode, MPP's WARL values, a timer
  interrupt taken in user mode with MIE clear, and seeded memory stalls) on
  the emulator and the RTL in step-tick mode: identical traces on Verilator
  and on Icarus, and every cause, `mtval`, `mepc` and `mstatus` equal to the
  values written into the test by hand. `test-rv32-irq` now expects MPP to
  read user after `mret`.
- **Console session with the new faults:** `PASS 91219bd0` on QEMU `virt`
  (the same three fault lines, pinned in
  [session.qemu.expected](../programs/rv32/os/session.qemu.expected)), the
  emulator (1,168,545 steps) and Verilator with a stall per request
  (7,011,061 cycles), results-identical over 78 console lines and 552
  exception records, with identical disks; Icarus agrees too (5,276,984
  cycles).
- **Earlier sessions in user mode:** Pong keeps its 200 checkpoints and
  `PASS 814f72be` on the emulator and is trace-identical to Verilator in
  step-tick mode (1,165,956 lines); the second boot (`PASS 455b9c97`) is
  results-identical between the emulator and Verilator; the jobs session
  gives `PASS 408a6738` on QEMU, the emulator and Verilator (41,568,246
  cycles, 40 presents, identical disks) and is trace-identical between the
  emulator and Verilator in step-tick mode (6,675,686 lines); the S1 menu
  session, which drives G1, G2 and SIMD4 from user mode through entries 4 and
  5, keeps its 185 checkpoints on the emulator and on Verilator with seeded
  waits (135,312,338 cycles). `test-rv32-os` passes.
- **Cost:** the core grows from 51,542 to 56,096 generic cells (Yosys 0.33,
  `synth -top rv32`), 4,554 cells for the eight entries' 320 flip-flops, the
  checker's comparators and the mode logic; still latch-free.

## Exercises (O5)

1. **One entry for a slot.** Slots are 128 KiB and aligned, so one NAPOT entry
   could replace entries 0 and 1. Which program would that break, and why?
2. **A locked kernel.** Add a locked entry that makes the kernel's text
   read-and-execute only, even for machine mode. What must the link script
   guarantee first, and what happens if the kernel then writes a variable?
3. **The idle loop in user mode.** What stops the idle loop running in user
   mode as it is, and what would the kernel have to grant it?
4. **Without the fetch check.** Make `fetch_deny` 0 in
   [rv32.v](../rtl/rv32/rv32.v) and run `make test-rv32-pmp`. Which tests
   fail, and what does the RTL execute instead of faulting?

## Exercises (O4)

1. **Switches.** Print `switches()` at the end of `bars` and run the jobs
   session on the emulator, on Verilator with cycle ticks and in step-tick
   mode. Which two agree, and why?
2. **The quantum.** Set `KERNEL_TICK` to 1,000 and to 100,000. What happens to
   the number of interrupts and to the frames the window shows?
3. **Starvation.** Start `bars &` and then type into the shell while it runs.
   How long does the shell take to answer on the emulator, and what decides
   it?
4. **Fairness.** `next_ready()` starts after the current process. What would
   go wrong if it always started at entry 0?

## Exercises (O2)

1. **Blocking by retry.** Run the console session with `--trace` and find a
   `read` ecall that executes more than once. What changed between the two
   executions, and which function made the process ready again?
2. **Where the tree is.** Swap the slots of `sh` and `fault` (so the shell,
   the first program spawned, is linked at slot 8) and move the kernel's
   `discover()` after that first `spawn`. Run the session on QEMU and on the
   emulator. How does the QEMU run end, what does the done register say, and
   why does the emulator not notice?
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
