# Next tracks: platform, OS, more software, and Nand2Tetris beyond

Updated 2026-10-07 after Track 3 and the console fixes. Tracks 0–3 are complete.
The remaining entries are saved ideas, not commitments or implementation claims.
Choose one bounded milestone and fix its contract and acceptance checks before
implementation. [PLAN.md](../../PLAN.md) preserves the milestone history.

## Where the machine stands

| Layer | Current capability | Remaining boundary |
| --- | --- | --- |
| CPU | Multicycle RV32IMAF, Zicsr/Zicntr, interrupts, M/S/U modes | No pipeline, C extension or caches |
| Memory/protection | 16 MiB RAM, PMP, Sv32, four-entry RTL TLB, per-process page tables and stack guards | Existing paging does not establish compatibility with another paged OS |
| Devices | CLINT, M-context PLIC, console, virtio-blk, input, palette/framebuffer, SIMD4, G1 and G2 | QEMU virt lacks our custom input/display/accelerators; no S-context PLIC |
| Our OS | Shell, syscalls, separate programs from RAM/disk, files, scheduling, isolation, lazy FPU switching | Further Unix-like services are optional |
| Software | Pong, Tetris, digit inference, 3D, picolibc, Lua, Mandelbrot, Doom | More ports need their own dependency and resource checks |
| Linux | Separate no-MMU image boots to BusyBox on QEMU, emulator and Verilator | MMU Linux has not been demonstrated |
| Verification | Differential tests, architectural suites, deterministic replay, GDB stub, benchmarks, lint and synthesis | Emulator speed and RTL cycles remain different measurements |

## Track 0: groundwork

**Complete.** M extension, counters, architectural suites, CoreMark/Dhrystone
baseline and emulator GDB stub. See [the record](../rv32-groundwork.md) and
[GDB](../rv32-gdb.md). The A extension was added in Track 3.

## Track 1: proper QEMU support

**Option 1 complete:** shared devices follow virt, custom devices occupy
non-overlapping ranges, and a device tree describes the platform. See
[the plan](track1-qemu.md) and [record](../rv32-platform.md).
Option 2, a custom board, remains proposed as Track 7 below.

## Track 2: a real OS, one step at a time

**O1–O5 complete:** interrupts, kernel/syscalls/shell, persistent storage,
preemptive scheduling and user-mode PMP protection. Subsequent work added
bounded accelerator DMA (#20), S-mode/Sv32 (#20), the RTL TLB (#24), and
per-process paging (#25). Track 3 added stack guards, seek, lazy FPU switching,
more memory and programs loaded from disk. See [the plan](track2-os.md) and
[the current OS record](../rv32-os.md).

## Track 3: run more apps

**Both streams complete.** [The plan](track3-apps.md) records:

| Milestone | Result | Evidence |
| --- | --- | --- |
| L1/L2 | picolibc and unmodified Lua, REPL and disk scripts | [C library/Lua](../rv32-libc.md) |
| B1 | Lazy floating-state switching and hard-float Mandelbrot | [Floating state](../rv32-os.md#floating-state-issue-33) |
| B2 | LR/SC and AMOs on emulator and RTL, compared with QEMU | [A extension](../rv32-a.md) |
| B3 | 16 MiB RAM, disks up to 8 MiB, disk programs, palette/keys, Doom | [Doom](../rv32-doom.md) |
| B4 | Pinned no-MMU Linux/BusyBox image, shell session and poweroff on all three backends | [Linux](../rv32-linux.md) |

Doom compares 350 demo frames on QEMU/emulator and 35 on Verilator; QEMU
checks guest-computed hashes without our display device. Linux's recorded
session takes 60,889,775 instructions on our backends and 345,437,877 RTL
cycles. These are acceptance records, not newly rerun measurements.

## Track 4: Nand2Tetris and beyond

**Proposed; independent of the other future tracks.**

- **NAND mapping:** define how sequential cells and memories are counted,
  synthesize the combinational logic to a NAND-based library, and report
  reproducible gate/storage counts. A tiny gate-level self-check is optional.
- **Compiler and VM:** a Jack-like compiler, course VM translated to RV32,
  and Math/Memory/Screen/Output/Keyboard/String/Array/Sys libraries for our
  devices. Finish line: course Pong and Square programs run through the new
  toolchain with checked output and a bounded RTL replay.

## Track 5: hardware performance

**Proposed.** Preserve the multicycle core as a reference and measure each
change with the existing workloads before combining optimizations.

- **Pipeline first:** start with a five-stage integer pipeline, forwarding,
  load-use stalls, branch flushing and precise traps. Specify interactions
  with memory stalls, MMIO, interrupts, paging, atomics and multicycle M/F
  units before replacing the full core. Finish line: architectural and guest
  regressions agree at retirement/results as appropriate, with published
  CoreMark/Dhrystone cycles and synthesis cost. Branch prediction follows a
  correct measured baseline.
- **Compressed instructions:** add C decode and instruction fetch across
  boundaries to both backends. Finish line: architectural tests and the same
  applications pass, with measured code-size and fetch-traffic changes.
- **Caches:** first establish a memory-latency workload. Define MMIO bypass,
  DMA ownership/coherence, fences and reset. Finish line: identical results
  under stalls plus measured miss rates, traffic and cycles.
- **FPGA:** choose a board and resource budget, adapt RAM to synchronous
  reads, then boot a serial self-check. Finish line: timing closure and a
  reproducible hardware demo; display and accelerators can follow separately.

## Track 6: Linux or xv6 using the MMU

**Proposed; feasible to investigate, not a boot claim.** Sv32, S-mode,
delegation, PMP and the TLB are already implemented, and our own OS uses
per-process page tables. This track exercises them with another kernel.

### Linux with Sv32

Start with a pinned RV32 kernel/configuration on QEMU, then the emulator,
then a bounded Verilator run. Audit the ISA/configuration, page-table A/D
handling, fences, boot memory layout and RAM budget. Define the M-mode
firmware/SBI services and how S-mode receives timer and external interrupts:
our PLIC currently has only an M-mode context, and supervisor interrupt bits
are driven by software. Choose firmware forwarding or hardware extensions
explicitly; existing Sv32 support alone does not settle this.

Linux's [boot requirements](https://docs.kernel.org/arch/riscv/boot.html)
require a hart ID and device-tree pointer at entry and an initially disabled
MMU. Follow the selected kernel's boot contract rather than assuming the
no-MMU image's configuration can simply be reused.

Proposed finish line: reproducible image, boot to a user shell with paging
active, user-program execution, isolation/page-fault checks, timer-driven
scheduling and clean shutdown, checked against QEMU and replayed on Verilator.
Keep the existing no-MMU target as its own regression. Disk support can be a
follow-up after an initramfs-based boot.

### xv6 with Sv32

Upstream [MIT xv6](https://github.com/mit-pdos/xv6-riscv) targets RV64; its
[book](https://pdos.csail.mit.edu/6.1810/2024/xv6/book-riscv-rev4.pdf)
describes Sv39. It cannot run unchanged on RV32/Sv32. First choose an audited,
pinned RV32 port or scope our own port: word sizes/ABI, assembly, page tables,
linker layout, traps, timers and devices. Extending the machine to RV64/Sv39
would be a separate, much larger hardware track.

Proposed finish line: shell, process creation/exec/wait, file I/O and relevant
user tests with paging/isolation on QEMU and the emulator, followed by a
bounded matching RTL session. Choose Linux or xv6 first; neither is a
prerequisite for the other.

## Track 7: a custom QEMU board

**Proposed; this is Track 1's deferred option 2.** Model our whole machine,
including input, display/palette, SIMD4, G1 and G2, so QEMU can run the capstone
with our actual device interfaces rather than only virt's shared subset.

Pin a QEMU version and define reset state, memory map, device tree, MMIO widths
and faults, interrupt wiring, DMA ownership, and device time. Build in slices:
board/shared devices, input/display, then each accelerator. Existing C device
models can help, but shared implementation is not an independent oracle;
retain the Python/guest references and RTL comparisons.

Proposed finish line: the same guest images boot, recorded capstone and SoC
sessions produce matching console/frame checkpoints, and directed tests cover
faults/reset/ownership. Compare results under an explicit timing contract,
not QEMU host speed against RTL cycles. Preserve all virt-based tests.
The benefit is broader QEMU tooling and platform coverage; cost includes
maintaining device models and a QEMU fork. MMU Linux is not a prerequisite.

## Other saved ideas

| Idea | First bounded milestone and finish line |
| --- | --- |
| Browser frontend | Build the emulator as WebAssembly; adapt display/input and run a recorded capstone session with native-matching checkpoints. Add persistent storage after defining browser save/export behavior. |
| Richer OS | Choose one of pipes/redirection, directories, or more flexible loading; define syscall/failure semantics and verify shell/file behavior and process isolation on applicable backends. |
| Graphics | Textures or a programmable fragment stage, then optionally G1/G2 overlap; define formats and ownership first, compare pixels/depth against an independent reference and measure traffic/cycles. |
| Inference | A larger model or convolution workload; fix preprocessing, numeric bounds and held-out accuracy, then compare CPU/accelerator results and measured end-to-end costs. |
| More applications | MicroPython or a ray tracer as separate ports; audit memory/library dependencies and preserve deterministic output tests. |

## Recommended order

No future track is selected. For deeper CPU design, start with Track 5's
pipeline. For an easily shared result, start with the browser frontend. For
OS learning, choose Track 6's feasibility milestone; for compiler learning,
choose Track 4. Track 7 is independently useful when full-device QEMU support
is the priority. Each starts with a bounded contract, then emulator/reference
checks, RTL evidence where applicable, and a documented learning walkthrough.
