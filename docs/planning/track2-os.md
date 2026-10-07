# Track 2 plan: a real OS, one step at a time

Written 2026-09-30, after Track 1. This fixes the contract and acceptance for
[Track 2](next-tracks.md#track-2-a-real-os-one-step-at-a-time): interrupts
(O1), a kernel with system calls and separately linked programs (O2), storage
(O3), preemptive multitasking (O4) and protection (O5). Each step lands as its
own commit with its own evidence; the record is
[docs/rv32-os.md](../rv32-os.md).

**Status (2026-10-07): O1–O5 complete.** Later milestones also delivered
S-mode/Sv32, a four-entry RTL TLB, per-process page tables, DMA bounds, stack
guards, lazy FPU switching and disk programs. See the [current backlog](next-tracks.md)
for optional OS expansion and MMU Linux/xv6.

**As built.** The plan below is kept as written; where the build departed
from it, the [record](../rv32-os.md) and [docs/rv32.md](../rv32.md) are the
contract:

- Slots are 128 KiB, 24 of them from `0x8010_0000` (56 with issue #33's 8 MiB; now 120 with issue #35's 16 MiB), and a program may span
  several (O4); they were 256 KiB in O2, as planned.
- The shell runs a program by its name (`NAME [ARGS]`, `NAME &`), not
  `run NAME`, and gained `wait` (O4) and file programs (`cat`, `write`,
  `files`, `fill`) rather than built-in `ls`/`cat` for files.
- The process table has eight entries, not four.
- The second boot is a second run on the disk the first left, on the
  emulator and the RTL (`run-rv32-os-boot2`) and on QEMU
  (`run-rv32-os-qemu-reboot`), not a mid-run reset on the RTL.
- The file calls are `open`, `read`, `write`, `close` and `files`; there is
  no seek at O3 (Track 3 L1 subsequently added it), and opening for writing truncates.
- PLIC sources latch their requests in a gateway until claimed (the PLIC
  specification, QEMU 11), rather than being pending only while the line is
  high as item 6 of O1 says.
- O5 grants the framebuffer to every process and the accelerators only to a
  program flagged for them, which is trusted: the engines' DMA is not held by
  PMP. (Issue #20 later closed this with a DMA window the kernel
  sets to the program's own slots.)

The track keeps the project's two rules. Every hardware change is made twice,
in the emulator and in the RTL, and compared. Every program that can run on
QEMU's `virt` board does, as an independent reference: Track 1 made that
possible, and this track keeps each new device at `virt`'s address and in
`virt`'s register layout (the PLIC, a 16550's receive side, virtio-blk), so a
kernel that finds its devices through the device tree runs unmodified on all
three backends.

## The open question: interrupts and device time

A device tick is one clock cycle on the RTL and one executed instruction on
the emulator ([Device time](../rv32.md#device-time)), so a timer interrupt
lands on a different instruction on each backend. Two answers, both adopted:

- **Results comparison** (the default, cycle ticks on the RTL). A program that
  takes interrupts is compared like one that reads the timer: console,
  outcome, checkpoints and the ordered exception records. Interrupt entries
  are timing and are excluded from the trap records, as step numbers are.
- **A deterministic tick mode** on the RTL (`+ticks=steps`, `--ticks steps`
  in the runner). The CLINT's `mtime` and the core's `cycle` advance once per
  *step*, an instruction retired or trapped or an interrupt taken, exactly as
  on the emulator, and `wfi` does not wait. Timer reads and timer interrupts
  then happen at the same instruction on both backends and the retirement
  traces, interrupt lines included, are compared line for line. The
  emulator needs no mode: its tick already is a step. Accelerators still
  advance per clock, so a program that polls one stays at results level.

## O1: interrupts

Machine-mode interrupts on the emulator and the RTL, through the CLINT and a
PLIC, as the privileged specification defines them for M-mode only.

1. **CSRs.** `mstatus` (0x300): MIE (bit 3), MPIE (bit 7) writable; MPP
   (12:11) reads 3 (M) until O5; FS (14:13) reads 3 and SD (31) reads 1,
   because floating state is always on ([F2](../rv32-f.md); writable since
   issue #33); every other bit
   reads 0. `mie` (0x304): MSIE, MTIE, MEIE (bits 3, 7, 11) writable, the rest
   0. `mip` (0x344): MSIP, MTIP, MEIP read-only, the live levels from the
   CLINT and the PLIC; a write is legal and changes nothing. `mscratch`
   (0x340): 32 bits, for the kernel's trap entry.
2. **Taking an interrupt.** Before an instruction is fetched, if `mstatus.MIE`
   is set and `mip & mie` is nonzero, the highest-priority pending interrupt
   is taken instead: MEI (11), then MSI (3), then MTI (7). `mcause` is
   `0x8000_0000 | code`, `mepc` the PC of the instruction not yet executed,
   `mtval` 0; MPIE takes MIE, MIE clears, MPP takes the current privilege.
   Exceptions do the same to `mstatus`. `mret` sets MIE from MPIE and MPIE to
   1 (and, from O5, the privilege from MPP). An interrupt entry is a step and
   a device tick; the double-fault rule treats it as a trap.
3. **Trace.** An interrupt entry is a trace line
   `<step> <pc> 00000000 interrupt <code>` on both backends.
4. **`wfi`** (0x1050_0073) retires. On the RTL with cycle ticks it first
   waits until `mip & mie` is nonzero, whatever MIE says; on the emulator and
   in step-tick mode it retires at once (the specification allows both).
   Programs wait with `wfi` in a loop.
5. **CLINT.** `mip.MTIP` is `mtime >= mtimecmp`, `mip.MSIP` is `msip`.
6. **PLIC** at `virt`'s `0x0c00_0000` (6 MiB window) in the SiFive/`virt`
   layout for one context (hart 0, M-mode): priorities (3 bits) at
   `4 × source`, the pending word at `+0x1000`, the enable word at `+0x2000`,
   threshold at `+0x20_0000`, claim/complete at `+0x20_0004`. Sources 1 to 31
   (`riscv,ndev = 31`); only wired sources hold a priority or an enable, the
   others read 0 and ignore writes. Sources are level-triggered, as QEMU's:
   a source is pending while its line is high and it has not been claimed; a
   claim returns the pending, enabled source with the highest priority above
   the threshold (ties to the lowest number) or 0, and marks it claimed until
   the same number is written back. `mip.MEIP` is set while a claim would
   return nonzero. Wired now: the input queue (line: `COUNT` nonzero) as
   source 12, the first number `virt` leaves free after its RTC. O3 wires
   virtio-blk as source 1, `virt`'s own number for the first virtio slot.
7. **Device tree.** The CPU gets its `riscv,cpu-intc` interrupt controller;
   the CLINT lists `interrupts-extended` (MSI, MTI); the PLIC is a node
   (`sifive,plic-1.0.0`, `riscv,plic0`) wired to MEI; the input node names
   its PLIC source. The map checker compares the PLIC with `virt`'s, as it
   does the CLINT.
8. **Deterministic tick mode** above, on the CLINT and the core.

Acceptance: `irqcheck` checks the gating (pending but disabled, enabled but
masked), a software interrupt, a timer interrupt with `wfi`, the priority of
simultaneous interrupts, the PLIC registers and claim/complete, and, where the
tree lists our input device, a keyboard interrupt from a scripted event. It
prints the same `PASS` word on QEMU `virt`, the emulator, Icarus and
Verilator (results comparison), and on Verilator in step-tick mode it is
trace-identical to the emulator, interrupt lines included. Directed tests
cover every CSR and PLIC register on both simulators and the emulator. Every
earlier image keeps its results; the architectural tests still pass (the one
`mstatus` write they make now succeeds instead of being skipped).

## O2: kernel and system calls

A kernel image boots, finds its devices in the device tree, and runs
separately linked programs from a RAM disk bundled into the image.

- **Console input.** The console gains a 16550's receive side: a byte read of
  +0 (RBR) returns and removes the next received byte (0 when there is none)
  and bit 0 of the status byte (LSR.DR) says one is waiting. The host feeds
  bytes from a file (`--console-input` on the emulator, `+console-input=` on
  the testbench, stdin on QEMU), all available from reset, so a scripted
  session is deterministic.
- **Programs.** Each program is linked at a fixed 256 KiB slot above the
  kernel (`0x8010_0000 + 0x4_0000 × n`), so any set of programs can be
  resident at once without relocation. A RAM disk (`tools/rv32_ramdisk.py`)
  packs their images with a name, load address, entry and size.
- **System calls.** `ecall` with the number in `a7` and arguments in `a0` to
  `a5`, the result in `a0`: exit, write, read (console), event and keys
  (input), present, sbrk, spawn, wait, list (the RAM disk), and later yield
  and sleep (O4) and the file calls (O3).
- **Kernel services.** The keyboard is interrupt-driven: the PLIC handler
  drains the input queue into a kernel buffer. The kernel parses the device
  tree before anything else, because on QEMU the tree lies in the RAM the
  program slots use.
- **Shell.** `sh`, itself a program, reads lines from the console: `ls`, `run
  NAME`, `halt`. Pong, Tetris and the menu become programs; the menu is the
  M7 runtime linked as one program.

Acceptance: a scripted console session runs the same on QEMU, the emulator
and the RTL; Pong run from the shell gives the standalone Pong's 200
checkpoints and `PASS 8fef54bc`, trace-identical between the emulator and
Verilator in step-tick mode.

## O3: storage

- **virtio-blk** (virtio-mmio version 2, `virt`'s first slot at
  `0x1000_1000`, PLIC source 1), one queue. On the emulator the disk is a host
  file; on the RTL a memory inside the device, loaded and saved by the
  testbench; on QEMU a `-drive`. Our device processes a request while the
  notify store waits (the contract lets any device hold `ready` low), using
  the CPU's side of the RAM port; QEMU completes asynchronously, so the
  driver waits for the used ring on every backend.
- **A tiny file system** (`tools/rv32_mkfs.py` builds and reads it): a
  superblock, a flat directory, contiguous extents. Calls: open, read, write,
  close, and a `ls`/`cat` in the shell.
- **High scores persist**: Pong and Tetris record their score to `scores`;
  the next boot shows it.

Acceptance: the same disk image read and written on all three backends, a
second boot (a second run on the emulator and QEMU, a mid-run reset on the
RTL) sees the first boot's score, and the host tool reads it back.

## O4: preemptive multitasking

Timer interrupts drive a round-robin scheduler over up to four processes;
`yield`, `sleep` and `wait` block. Two programs share the machine: each draws
its half of the screen and prints its result at exit.

Acceptance: in step-tick mode the emulator and Verilator agree trace for
trace, checkpoints included; with cycle ticks and on QEMU the results agree
(each program's result is independent of the interleaving) and the kernel's
report shows both were preempted.

## O5: protection

- **User mode.** `mstatus.MPP` holds 0 (U) or 3 (M); `mret` enters U; `ecall`
  from U is cause 8; a machine CSR, `mret` or `wfi` in U is illegal; the
  counters need `mcounteren` (CY, TM, IR). In U-mode interrupts are always
  enabled.
- **PMP.** Eight entries (`pmpcfg0`–`1`, `pmpaddr0`–`7`), OFF/TOR/NA4/NAPOT,
  R/W/X and L, 4-byte granularity. U-mode accesses must match an entry that
  allows them; M-mode is checked only against locked entries. A refused
  access never reaches the bus: fetch, load or store access fault with the
  address in `mtval`.
- **Kernel.** Programs run in U-mode with PMP granting their slot, the
  framebuffer and the accelerators. A program that touches kernel memory or
  executes a privileged instruction is killed; the shell and every other
  process keep running.

Acceptance: `fault` (a program that writes kernel memory, then one that reads
`mstatus`) is killed with the right cause on all three backends and the
shell continues; directed PMP tests on both simulators; the O2 to O4 sessions
keep their results in U-mode. An Sv32 MMU stays a later milestone.
