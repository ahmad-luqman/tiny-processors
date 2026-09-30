# Next tracks: platform, OS, more software, and Nand2Tetris beyond

Written 2026-09-29, after S1 and the digit size fix. This is a menu of
candidate tracks with a recommended order, not a commitment. Each track gets
its own contract and acceptance checks in the usual milestone style when it is
chosen; the entries below say what it would take and what "done" could mean.

## Where the machine stands

| Layer | Today | Gap |
| --- | --- | --- |
| CPU | Multicycle RV32I + F: 4 to 5 cycles per integer instruction, plus FPU issue/wait cycles for floating-point arithmetic (see [F2](../rv32-f.md)); the four trap CSRs plus `fflags`/`frm`/`fcsr`; machine mode only | No M (multiply/divide is software in `programs/rv32/rt/muldiv.c`), no `mstatus`, no interrupts, no A, no C, no counters |
| Memory and devices | 4 MiB RAM (images fit a 256 KiB slice); timer, 16-event input queue, 320×240 RGB332 framebuffer, SIMD4, G1, G2 | Everything polls; no storage device; the palette window at `0x2000_3000` is reserved but unbuilt |
| OS | Polling runtime: one static image with every application compiled into the menu | No syscalls, separate programs, files, scheduling, or protection |
| QEMU | Reference runner only: `selfcheck` and `floatsoft` run on the `virt` board because the console and done register match its 16550 UART and `sifive_test` | None of our other devices exist there, so the capstone cannot run on QEMU |
| Software | Freestanding C, no libc, no `malloc` | Real programs expect a C library |

## Track 0: groundwork

**Done (2026-09-29):** all five steps; see the [Track 0 record](../rv32-groundwork.md).
The CPU row of the table above is now RV32IMF with Zicntr, and O1 is next.

Small, low-risk steps that every later track leans on.

- **Architectural compliance.** Run `riscv-arch-test` (the official RISC-V
  architectural tests) for I, F and later M/A/Zicsr on the emulator and the
  RTL. Done: all selected suites pass on the emulator, Icarus and Verilator.
- **M extension in hardware.** Multiply and divide in the core and the
  emulator; retire `rt/muldiv.c` for M builds while keeping an RV32I build.
  Linux, Lua and Doom all assume it.
- **Zicntr.** `cycle`, `time` and `instret` (with their high halves), so
  programs can measure themselves.
- **CoreMark and Dhrystone.** A standard performance baseline in cycles per
  iteration on the RTL, recorded before any pipelining work.
- **GDB stub in `rv32emu`.** Registers, memory, breakpoints and single-step
  over the GDB remote protocol, so kernel work can be debugged.

## Track 1: proper QEMU support

**Done (2026-09-29), option 1:** see the [plan](track1-qemu.md) and the
[Track 1 record](../rv32-platform.md). Our devices moved into a hole `virt`
leaves unused, the timer became a CLINT at `virt`'s address, and every backend
passes a device tree in `a1`; the PLIC and virtio-blk addresses are reserved for
O1 and O3. Option 2 (a custom QEMU board) remains optional.

"Proper QEMU support" can mean two different things.

1. **Make the platform `virt`-compatible where QEMU already has devices
   (recommended).** Put the timer at the CLINT addresses (`mtime` at
   `0x0200_bff8`, `mtimecmp` at `0x0200_4000`), then add an interrupt controller
   compatible with the PLIC and a virtio-blk storage device (see O3). The
   console and done register already match. Result: the kernel runs unmodified
   on QEMU, the emulator and the RTL, and QEMU becomes an independent check of
   the kernel rather than of `selfcheck` alone. Our own devices (input,
   display, accelerators) are not simply absent on QEMU: as
   [docs/rv32.md](../rv32.md#memory-map) records, their
   windows overlap `virt`'s flash banks at `0x2000_0000`, its PCIe
   configuration space at `0x3000_0000` and its PCI memory up to
   `0x8000_0000`, so probing them would touch unrelated QEMU devices. This
   track must first choose one of: remap our devices into a range `virt`
   leaves unused (checked against `-M virt,dumpdtb=`), or have the kernel
   discover the platform without touching those addresses, for example from
   the device tree QEMU passes in `a1` (with our backends passing a
   recognisable value or their own device tree) and never access a device
   the platform does not describe.
2. **A custom QEMU board for our machine (optional).** A QEMU fork with our
   timer, input queue, display and framebuffer, with device models wrapping the
   existing C models in `tools/rv32_simd4.c`, `tools/rv32_gpu.c` and
   `tools/rv32_g3d.c`. The capstone would run on a third independent backend
   with QEMU's gdbstub and record/replay. Our emulator is already fast (about
   400 M instructions/s), so the payoff is independence and tooling, not speed.

## Track 2: a real OS, one step at a time

**In progress (2026-09-30):** see the [plan](track2-os.md) and the
[Track 2 record](../rv32-os.md). O1 is done: `mstatus`, `mie`, `mip`,
`mscratch`, `wfi`, CLINT and PLIC interrupts on the emulator and the RTL, and
the open question below answered both ways (results comparison, and a
deterministic step-tick mode on the RTL that keeps trace comparison). O2 is
done: a kernel with system calls runs separately linked programs from a RAM
disk and a shell, on QEMU, the emulator and the RTL. O3 is done: virtio-blk at
`virt`'s first virtio slot, a tiny file system, and high scores that survive
a boot. O4 is done: timer-driven round-robin scheduling, two programs sharing
the screen, trace-identical in step-tick mode.

- **O1 interrupts.** `mstatus` (MIE/MPIE), `mie`, `mip`, a CLINT timer
  interrupt and `wfi`, on the RTL and the emulator.
  Open question: a timer tick is one instruction on the emulator and one clock
  cycle on the RTL, so an interrupt lands on a different instruction on each
  backend. Extend the [device-time contract](../rv32.md#device-time) so
  interrupt-driven programs are compared at the results level, as timer reads
  already are, and keep trace comparison for programs that never enable
  interrupts. A deterministic mode that fires the timer on an instruction count
  on both backends would give a trace-comparable variant.
- **O2 kernel and syscalls.** `ecall` becomes a syscall interface (write,
  exit, read input, present, sbrk). Pong, Tetris and the digit screen become
  separately linked programs loaded from a RAM disk bundled into the image; a
  shell on the console lists and runs them. The menu becomes one program.
- **O3 storage.** A block device backed by a host file on the emulator and by
  memory on the RTL (virtio-blk-compatible if Track 1.1 is chosen), plus a tiny
  file system. High scores persist across boots.
- **O4 preemptive multitasking.** Timer-driven context switches, a
  round-robin scheduler, and two programs visibly sharing the machine.
- **O5 protection.** User mode and PMP first: a user program that touches
  kernel memory traps and is killed while the system keeps running. A Sv32 MMU
  with a TLB is a later, larger hardware milestone.

## Track 3: run more apps

- **C library.** Port picolibc or newlib with `sbrk` and file access backed
  by O2 and O3. Most of the list below needs it.
- **Lua or MicroPython REPL** on the console: the first real third-party
  program.
- **Ray tracer or Mandelbrot on the FPU**: an easy showcase for F2.
- **Doom (doomgeneric).** 320×200 fits the 320×240 framebuffer, and its
  256-colour palette justifies building the reserved palette window. It needs
  more than 4 MiB of RAM and somewhere to keep the WAD, so it depends on O3 and
  a larger RAM contract.
- **Linux without an MMU (stretch).** cnlohr's `mini-rv32ima` shows that Linux
  boots on RV32IMA + Zicsr with a CLINT timer and a UART, without an MMU.
  Track 0, O1 and the A extension put this in reach on the emulator first, then
  slowly on Verilator. A Sv32 MMU later would open xv6 or Linux with an MMU.

## Track 4: Nand2Tetris and beyond

- **The bottom of the stack.** Map `rv32` to NAND gates only with Yosys
  (`abc -g NAND`) and report the count: how many NANDs is our CPU? Optionally a
  tiny NAND-level simulator that runs the self-check, slowly.
- **The top of the stack.** A Jack-like compiler, the Nand2Tetris VM translated
  to RV32, and the Nand2Tetris OS libraries (Math, Memory, Screen, Output,
  Keyboard, String, Array, Sys) written for our devices. Acceptance: unmodified
  course Jack programs (Pong, Square) run on our machine.

This track does not depend on the others and can run alongside any of them.

## Track 5: hardware performance

- A 5-stage pipeline with hazards and branch prediction, measured against the
  multicycle core with CoreMark.
- The C extension (compressed instructions) for code size.
- Caches, once memory has latency worth hiding.
- **FPGA port.** `rv32_ram` reads asynchronously, and FPGA block RAM needs
  synchronous reads; the ready/valid bus already allows memory to take extra
  cycles, so the change is contained.
- **Browser frontend.** Compile `rv32emu` to WebAssembly so anyone can play
  the capstone in a browser.

## Recommended order

1. Groundwork: compliance suite, M, Zicntr, CoreMark, GDB stub (done, [record](../rv32-groundwork.md)).
2. Track 1 option 1 (done, [record](../rv32-platform.md)), then O1 interrupts
   through its CLINT and a PLIC at `virt`'s address.
3. O2 kernel, syscalls, separate programs and a shell, run on QEMU `virt` as
   well as our backends.
4. C library, then Lua: the first "runs more apps" result.
5. A extension, then Linux without an MMU on the emulator and then the RTL;
   or the Jack compiler and OS if Nand2Tetris is the priority.
6. Storage, palette and more RAM, then Doom. After that: the MMU, the
   pipeline, the custom QEMU board or the FPGA, by interest.
