# Track 3 plan: run more apps, in two streams

Written 2026-10-02, after Track 2 and the paging work of issues #20, #24, #25
and #30. [Next tracks](next-tracks.md) lists Track 3's candidates in one
bullet list: a C library, Lua or MicroPython, a ray tracer, Doom and Linux
without an MMU. They do not depend on one another equally. A C library unlocks
almost everything else, and the larger programs each need something new
underneath (more RAM, a palette, the A extension). So the track is split into
two streams. Stream A is done first and has its own record; stream B is a menu
for later, like the rest of next-tracks.md.

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

## Stream B: bigger programs (later)

Each needs something stream A did not, so each gets its own contract and
acceptance checks when it is chosen. A suggested order, cheapest first:

1. **An FPU showcase.** A Mandelbrot or a small ray tracer, drawn to the
   framebuffer with the F extension's single-precision instructions (F2), as a
   C library program. It needs the kernel to save the F registers on a context
   switch (it saves only the integer ones today), which is the real work.
2. **The A extension, then Linux without an MMU.** `mini-rv32ima` shows Linux
   booting on RV32IMA with a CLINT and a UART. The A extension is a hardware
   milestone on both backends; Linux then runs on the emulator first and on
   Verilator slowly.
3. **Doom.** doomgeneric at 320×200 fits the framebuffer. It needs the
   reserved palette window built, more than 4 MiB of RAM (a larger RAM
   contract on every backend), and a file system that holds a WAD (tfs files
   are 4 KiB).

MicroPython is an alternative to Lua that stream A did not need. Its port
would reuse L1 unchanged.
