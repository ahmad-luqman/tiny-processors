# Track 3 plan: run more apps, in two streams

Written 2026-10-02, after Track 2 and the paging work of issues #20, #24, #25
and #30. [Next tracks](next-tracks.md) lists Track 3's candidates in one
bullet list: a C library, Lua or MicroPython, a ray tracer, Doom and Linux
without an MMU. They do not depend on one another equally. A C library unlocks
almost everything else, and the larger programs each need something new
underneath (more RAM, a palette, the A extension). So the track is split into
two streams. Stream A is done first and has its own record; stream B subsequently completed all four milestones.

**Status (2026-10-07): both streams complete.** L1/L2 and B1–B4 are delivered,
including Doom and no-MMU Linux on QEMU, the emulator and Verilator. PRs #46
and #47 subsequently fixed interactive output flushing and QEMU console capture.
Future work lives in [next tracks](next-tracks.md); MMU Linux/xv6 and a custom
QEMU board are separate proposed tracks.

## Stream A: a C library, then Lua (done)

The first "runs more apps" result: unmodified third-party C running as an OS
program, on QEMU `virt`, the emulator and the RTL. Two milestones, each with its
own session; the [record](../rv32-libc.md) has the evidence.

### L1: the C library

- **picolibc**, built for RV32I/ILP32 from vendored sources: only the files the
  programs link, picked by [rv32_vendor_libc.py](../../tools/rv32_vendor_libc.py)
  from a meson build, with the configuration meson generated
  (`picolibc.h`). Tiny stdio, nano-malloc, the libm double and float functions,
  setjmp, strings, time.
- **compiler-rt builtins** for what RV32I lacks: multiply and divide, 64-bit
  integers, and soft single- and double-precision floating point. Clang calls
  them; nothing in the programs names them.
- **The OS layer** (`programs/rv32/os/libc/`): startup (`crt0.S`, and
  `crt.c`, which turns the kernel's one argument string into `argc`/`argv`); `read`, `write`, `open`, `close`,
  `lseek`, `fstat`, `sbrk` and `times` on the kernel's system calls
  (`gettimeofday` needs none); a console line discipline, so a program reading
  stdin echoes and edits as the shell does (since the review, with the shell's
  own editor, `line.c`).
- **Kernel:** one new call, `seek`, so `fseek` and `ftell` work on tfs files.
  Nothing else in the system-call interface changes.
- **Acceptance:** `libccheck`, a program that checks printf/scanf formatting,
  string conversions, the heap on `sbrk`, setjmp, libm, files (write, read
  back, seek, rewrite, a missing file's errno) and a line typed on the console,
  prints `libccheck: ok` on QEMU, the emulator and Verilator, with identical
  disks.

### L2: Lua

- **Lua 5.4.7**, vendored unmodified, configured as the release's
  `src/Makefile` configures its `generic` platform (`LUA_COMPAT_5_3`, 64-bit integers, double floats): the stand-alone
  `lua.c` interpreter is the program.
- **Room:** six slots at the top (768 KiB) for a 370 KB image, the heap and
  the stack.
- **Acceptance:** a REPL session (arithmetic, strings, tables, metatables,
  coroutines, errors with tracebacks, files through `io`, `os.exit`) and three
  scripts read from the disk (`hello.lua` with arguments, `queens.lua`,
  `words.lua`, which reads one file and writes another) give the same
  transcript on QEMU, the emulator and Verilator, with identical disks.

### What stream A found and fixed underneath

Lua's C stack needs are what a desktop gives it: a `pcall` chain to its limit of
200 C levels takes 133 KB, and every program had a fixed 32 KiB stack with the
heap directly below. An overflow would have run silently into the heap. Two
changes, for every program:

- **A stack size per program.** The link script takes `STACK_SIZE`, the RAM
  disk entry records it, the kernel stops `sbrk` below it. 32 KiB stays the
  default; Lua asks for 160 KiB.
- **A guard page.** The kernel leaves the stack's lowest page unmapped, so an
  overflow is a page fault and the process is killed with
  `(stack overflow)` instead of corrupting its heap. `fault stack` tests it.

## Stream B: bigger programs (done)

Each needed additions below the application, with its own contract and
acceptance checks. Four issues were completed in this order (B3 and B4 each
took two PRs): [#33](https://github.com/ahmad-luqman/tiny-processors/issues/33) (B1, the
FPU), [#34](https://github.com/ahmad-luqman/tiny-processors/issues/34) (B2, the
A extension), [#35](https://github.com/ahmad-luqman/tiny-processors/issues/35)
(B3, Doom) and [#36](https://github.com/ahmad-luqman/tiny-processors/issues/36)
(B4, Linux without an MMU, on B2 and B3's RAM).

1. **An FPU showcase (B1, PR #37).** A Mandelbrot, drawn to the framebuffer with
   the F extension's single-precision instructions (F2), as a C library
   program built for the single-float ABI. The real work was the kernel
   saving the F registers on a context switch: `mstatus.FS` became real on
   both backends and the kernel switches the FPU lazily. All 24 slots were
   taken, so RAM went to 8 MiB (56 slots) here, ahead of Doom
   ([record](../rv32-os.md#floating-state-issue-33)).
2. **The A extension (B2, issue #34).** A hardware milestone on both backends,
   like F1 and F2: `lr.w`, `sc.w` and the nine AMOs that Linux needs, with a
   single-hart reservation. riscv-arch-test's A suite and riscv-tests' rv32ua
   pass on the emulator, Icarus and Verilator and match QEMU
   ([record](../rv32-a.md)).
3. **Doom (B3, issue #35).** doomgeneric at 320×200 fits the framebuffer. Its
   6 MiB zone ruled out 8 MiB, so the first of its two PRs makes RAM 16 MiB.
   The same PR builds the palette window, lets a disk be up to 8 MiB for the
   WAD (tfs extents were already any length; the disk was the limit), lets
   programs live on the disk (the kernel's image cannot hold Doom), and adds
   keys. The second PR ports Doom: doomgeneric unmodified, GPL-2.0, as a disk
   program; the shareware WAD fetched from Debian's pool, never committed; a
   virtual clock, so the timedemo draws the same frames on QEMU, the emulator
   and Verilator ([record](../rv32-doom.md)).
4. **Linux without an MMU (B4).** Linux 6.12, nommu, in machine mode, built by
   upstream Buildroot in Docker from pinned sources and never committed, boots
   to a busybox shell on QEMU `virt`, the emulator and Verilator, from one image
   with a device tree compiled in. The first of its two PRs gave the machine what
   Linux needs: `misa` and the ID CSRs, `fence.i`, a 16550 the 8250 driver can
   drive, and a readable done register. The second builds the image and types a
   session into the shell, a line per prompt on every backend
   ([record](../rv32-linux.md)).

MicroPython is an alternative to Lua that stream A did not need. Its port
would reuse L1 unchanged.
