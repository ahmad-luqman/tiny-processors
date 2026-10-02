# Track 3, stream A: a C library, then Lua

Track 2 ended with an operating system whose programs were all ours: written
against a small user library ([ulib.h](../programs/rv32/os/ulib.h)) with no
`printf`, no `malloc` and no floating point beyond what the games needed.
Stream A of Track 3 ([plan](planning/track3-apps.md)) runs other people's
code: picolibc as the C library (L1), then the Lua 5.4.7 interpreter on top of
it (L2), unmodified, as programs the shell starts. Both run on QEMU `virt`, the
emulator and the RTL.

## L1: the C library

### What a C library needs from the machine

picolibc is a C library for small embedded systems. Most of it is plain C that
needs nothing from the machine: `printf`'s formatting, `strtod`, `qsort`, the
math functions. What it needs is short:

| picolibc calls | Served by | System call (sys.h) |
| --- | --- | --- |
| `read(0, ...)` | the line discipline ([tty.c](../programs/rv32/os/libc/tty.c)) | `read` |
| `read`, `write`, `close` on files, `write(1/2)` | [syscalls.c](../programs/rv32/os/libc/syscalls.c) | `read`, `write`, `close` |
| `open(name, O_RDONLY)` / `O_WRONLY \| O_CREAT \| O_TRUNC` | `open`; append and read-write refused | `open` |
| `lseek`, `fstat` | positions within a tfs file | `seek` (new) |
| `sbrk` | nano-malloc's heap | `sbrk` |
| `gettimeofday`, `times` | the epoch; device ticks | `time` |
| `_exit` | the end of the process | `exit` |
| `unlink`, `rename`, `stat`, `getentropy` | not provided: `ENOSYS` | |

and three compiler-runtime facts: RV32I has no multiply or divide, no floating
point and no 64-bit arithmetic, so clang emits calls to `__muldf3`,
`__divdi3` and so on. Those come from LLVM's compiler-rt builtins, vendored
next to picolibc.

The kernel gains one system call for all of this. `seek(fd, offset, whence)`
moves an open file's position (from the start, the current position or the end,
the offset signed) and returns the new one; a position outside 0 to the file's
size is refused. `fseek`, `ftell`, `rewind` and `fstat`'s size use it. Nothing
else changes in the interface, and a C library program and a ulib program can
run side by side.

### Vendoring

[third_party/picolibc](../third_party/picolibc/README.md) is picolibc 1.8.10 cut
down to the 142 source files the two programs link, with every header they
include and the `picolibc.h` that meson generated for our configuration
(tiny stdio, nano-malloc, a global `errno`, single-threaded, `long long` in
printf). [third_party/compiler-rt](../third_party/compiler-rt/README.md) is
the 26 builtins they call, from LLVM 18.1.8. Neither directory has a build
system of its own: each has a `SOURCES` list, and the Makefile compiles the
lists into `build/rv32/libc/libc.a` and `builtins.a` with our clang.

[tools/rv32_vendor_libc.py](../tools/rv32_vendor_libc.py) chose the files. It
reads the link maps of the programs built against a full meson build of
picolibc, maps each archive member back to its source through meson's
`compile_commands.json`, and copies the sources, the headers the preprocessor
reads for each, and the licences. When a new program needs a function that is
not there, the link names it and the script, run with the new program's map,
adds it. (One trap found on the way: compiler-rt's `riscv/muldi3.S` is for RV64
only and assembles to nothing on RV32, so the script prefers the generic C
file.)

### The programs' side

[crt0.S](../programs/rv32/os/libc/crt0.S) and [crt.c](../programs/rv32/os/libc/crt.c)
replace ustart.S. The kernel passes one argument string; crt.c splits it at
spaces (a part in double quotes keeps its spaces: `libccheck one "two words"`)
into `argv`, with the program's name as `argv[0]`. The kernel does not pass the
name, so crt.c is compiled once per program with it. Then the constructors run
(`__libc_init_array`) and `exit(main(argc, argv))`; picolibc's `exit` runs the
destructors, one of which flushes stdout. The link script ([user.ld](../programs/rv32/os/user.ld))
keeps the constructor and destructor tables inside `.rodata`, so an image
still has one segment and the image checker's usual sections.

**The console.** The host does not echo (issue #30), and picolibc's stdin
reads fd 0 a buffer at a time, so a program would read keys it never showed.
[tty.c](../programs/rv32/os/libc/tty.c) is a line discipline in the library,
the shell's editor moved under `read(0)`: it echoes each key, handles
Backspace, ^U, Tab and escape sequences as [sh.c](../programs/rv32/os/sh.c)
does, and hands the program a whole line at Enter. ^D at the start of a line is
the end of input (Lua's REPL then exits). A line past 254 characters rings the
bell and is dropped at Enter with a message, as the shell refuses one.

**Errors.** A failed call sets `errno` as POSIX names it. The kernel's
refusals carry no reason, so `open` works one out afterwards: every descriptor
taken (`EMFILE`), the file exists but someone is writing it (`EBUSY`), it does
not exist (`ENOENT`), or it would have been new (`ENOSPC`).

**Time.** The machine has no real-time clock, so `time()` is 0 and
`os.date()` says 1970-01-01 on every backend. That also keeps Lua
deterministic: its string hash seed and `math.random`'s default seed mix in
`time(NULL)`. `clock()` counts device ticks, which differ between backends by
design ([device time](rv32.md#device-time)); no session prints them.

### Two things picolibc does differently

- **Floating-point digits.** picolibc prints the shortest digits that read
  back to the same double, then zeros: `%.17g` of 0.1 is `0.1`, where glibc
  prints `0.10000000000000001`, and `%.20f` of 0.1 ends in zeros. Lua's own
  `%.14g` is unaffected (`0.1 + 0.2` prints `0.3`), and `%.17g` of
  `0.1 + 0.2` is still `0.30000000000000004`. libccheck pins both.
- **errno after malloc.** clang treats `malloc` as touching no memory a program
  can see, so it may keep `errno`'s old value across a failed `malloc`.
  picolibc builds itself with `-fno-builtin-malloc` and friends for this
  reason; the programs are built that way too (`RV32_LIBC_ALLOC_FLAGS`).
  libccheck found it: `malloc(16 MiB)` returned `NULL` and `errno` read 0.

### libccheck

[libccheck.c](../programs/rv32/os/libc/libccheck.c) (slot 6) checks the library
on the kernel, each group against values written into it by hand, and prints
one line per group:

```
$ libccheck one "two words"
libccheck: 2 arguments, "two words"
libccheck: printf 7 2.50 ok
libccheck: conversions
libccheck: strings
libccheck: heap
libccheck: setjmp
libccheck: maths 1.4142135623731 0.841470984807897
libccheck: nosuch: No such file or directory
libccheck: files
libccheck: type a line: 3 plus 4
libccheck: 3 plus 4 is 7
libccheck: time
libccheck: ok
$ cat libc.out
rewritten
```

Formatting covers integers, `long long`, doubles, infinities and NaN,
truncation; conversions `strtol`, `strtoull`, `strtod` (hex too) and a
`%.17g` round trip; the heap 32 blocks, `realloc`, `calloc`, a refused 16 MiB
`malloc` with `ENOMEM` and a `malloc` after it; maths compares `sqrt`, `sin`,
`cos`, `exp`, `log`, `pow`, `fmod`, `atan2` and `sqrtf` with their correctly
rounded values; files write a file, read it back with `fgets`, `ftell`,
`fseek` from the end, `fread` after `rewind`, refuse a seek past the end and an
append, and rewrite it. `cat libc.out` shows the rewrite reached the disk.

## The stack: a size per program and a guard page

Lua needs far more stack than our programs. A `pcall` costs 656 bytes of C
stack per level (picolibc's RISC-V `jmp_buf` alone is 208 bytes), and Lua
allows 200 C levels, as on a desktop. Measured by painting the stack and
looking at what was overwritten:

| Probe (180 to 300 deep) | Stack used |
| --- | --- |
| nested parentheses, tables or `do` blocks in `load` | 29 KB to 32 KB |
| recursion through `pcall` (stops at "C stack overflow") | 132 KB |
| the same through `xpcall` with `debug.traceback` | 133 KB |
| recursive `__index`, `__tostring` | 37 KB, 57 KB |

Every program had the top 32 KiB of its span for a stack, with its heap right
below and nothing in between: the deepest of these ran silently into Lua's heap.
Lowering Lua's limit (`LUAI_MAXCCALLS`) would have fitted 32 KiB only at about
40 levels. Instead the OS changed, for every program:

- **A stack size per program.** [user.ld](../programs/rv32/os/user.ld) takes
  `STACK_SIZE` with `--defsym`, as it takes the slot base and span (the
  Makefile passes 32 KiB unless `RV32_OS_STACK_name` says otherwise). The RAM
  disk entry grows a ninth word, the stack size
  ([rv32_ramdisk.py](../tools/rv32_ramdisk.py)), checked to be whole pages, at
  least two, above the program; the kernel checks it again at boot and stops
  `sbrk` below the stack rather than below a fixed 32 KiB. Lua asks for
  160 KiB, 27 KB over the worst case measured.
- **A guard page.** The kernel maps every page of a process's slots but the
  stack's lowest, so a stack that outgrows its size takes a page fault instead
  of writing the heap, and the kernel names it:

  ```
  $ fault stack
  kernel: pid 4 fault killed: cause 15 at fault+0x80 tval 80218fa0 (stack overflow)
  sh: fault exited 143
  ```

  `fault stack` grows a stack 1 KiB at a time until it gets there. QEMU prints
  the same line: its page tables are the kernel's. The guard costs every
  default program 4 KiB of its 32, which none of the pinned sessions noticed.

A first version kept the stack size in `struct proc`, which made it 260 bytes;
RV32I has no multiply, so every `procs[i]` in the scheduler's loops became a call
and the console session took 2.86 M steps instead of 1.80 M. The guard address
now lives in the per-entry `struct address_space` with the page-table layout,
and a static assertion keeps `struct proc` at 256 bytes.

## L2: Lua

[third_party/lua](../third_party/lua/README.md) is Lua 5.4.7 from the Lua
team's repository, unmodified: the interpreter's sources less the test suite.
The build is Lua's own "generic" platform, `LUA_COMPAT_5_3` and nothing else, so
integers are 64-bit and numbers doubles, all in software. The program is
`lua.c`, the stand-alone interpreter: with no arguments a REPL on the console,
with a file name a script with `arg`.

| | Bytes |
| --- | --- |
| Lua's code and data (32 files, -O2, RV32I) | 255 KB |
| picolibc and compiler-rt as linked | 99 KB |
| the image, `.bss` included | 370 KB |
| stack | 160 KiB |
| heap, up to the stack | 252 KB |

It spans slots 18 to 23, the six at the top of RAM. Lua's image is the RAM disk's
largest program, and spawning it copied 368 KB a byte at a time; [mem.c](../programs/rv32/os/mem.c)'s
`memcpy` and `memset` now move words when both addresses allow, which took
the Lua session from 24.7 M steps to 14.7 M.

What does not work: `io.popen` and `os.execute` (no processes from C;
`'popen' not supported`), `os.remove` and `os.rename` (tfs has neither),
`io.open(name, "a")` and `"r+"` (tfs replaces a file opened for writing), and
`require` of a C module (no dynamic loading; Lua modules from the disk would
work through `package.path`, given a `?.lua` name, but tfs has no directories).

### The session

[lua.session](../programs/rv32/os/lua.session) runs on a disk of its own,
`apps.disk`, which holds `welcome` and the three scripts in
[programs/rv32/os/lua](../programs/rv32/os/lua), so the Track 2 sessions keep
their file listings. In the REPL:

```
$ lua
Lua 5.4.7  Copyright (C) 1994-2024 Lua.org, PUC-Rio
> print("hello from Lua", 1 + 2, 10 / 4, 2^53, 7 // 2, 0.1 + 0.2, 1/0)
hello from Lua	3	2.5	9.007199254741e+15	3	0.3	inf
> print(math.maxinteger, math.mininteger, 3 % -2, -7 // 2, 2^0.5, math.pi)
9223372036854775807	-9223372036854775808	-1	-4	1.4142135623731	3.1415926535898
...
> fib = setmetatable({[0] = 0, 1}, {__index = function(f, n) f[n] = f[n - 1] + f[n - 2] return f[n] end})
> print(fib[50], fib[90])
12586269025	2880067194370816120
...
> error({code = 7})
(error object is a table value)
stack traceback:
	[C]: in function 'error'
	stdin:1: in main chunk
	[C]: in ?
...
> print(io.open("nosuch"))
nil	nosuch: No such file or directory	2
> print(os.time(), os.date("!%Y-%m-%d %H:%M"), os.getenv("HOME"), pcall(io.popen, "ls"))
0	1970-01-01 00:00	nil	false	'popen' not supported
> os.exit(3)
sh: lua exited 3
```

then a second REPL ended with ^D, and the scripts:

```
$ lua hello.lua one two three
hello from Lua 5.4 on the tiny computer
3 arguments: one, two, three
squares: 1 4 9 16 25 36 49 64
$ lua queens.lua 6
6 queens: 4 solutions
$ lua words.lua welcome
welcome: 15 words, 12 different
  3 the
  2 files
...
$ lua nosuch.lua
lua: cannot open nosuch.lua: No such file or directory
sh: lua exited 1
```

`words.lua` reads `welcome` with `io.lines`, sorts with a comparison
function, and writes `words.out`, which `cat` then prints. One REPL line is
typed with a typo and three rubouts, so the transcript holds the echo's
`\b \b`s.

## Evidence

Measured on Ubuntu 24.04 with clang 18.1.3, QEMU 8.2.2 and Verilator 5.020, from
a clean `build/`. Verilator 5.020 needed two local workarounds that are not part
of this change: the testbench was built with `-Wno-WIDTHCONCAT` (it stops at a
width warning in `rv32_virtio_blk.v` that predates Track 3), and the simulator
was run through a wrapper that drops `+verilator+quiet`, which the runner passes
and 5.020's runtime does not know.

- **libc session** ([libc.session](../programs/rv32/os/libc.session):
  libccheck, `cat libc.out`, `fault stack`): `PASS ccbfe7c6` on QEMU `virt`
  (transcript pinned in [libc.session.qemu.expected](../programs/rv32/os/libc.session.qemu.expected)),
  the emulator (2,361,798 steps) and Verilator with a stall per request
  (14,162,058 cycles), results-identical over 27 console lines and 240 exception records, with identical disks; QEMU's disk is byte for byte the
  emulator's.
- **Lua session**: `PASS 76c88bdd` on QEMU `virt`
  ([lua.session.qemu.expected](../programs/rv32/os/lua.session.qemu.expected)),
  the emulator (14,718,454 steps) and Verilator with a stall per request
  (94,903,246 cycles, 6 m 49 s with the libc session alongside), results-identical over its 87 console
  lines and 4,277 exception records, with identical disks; QEMU's disk is byte for byte the emulator's.
- **Earlier sessions:** the console session (only `ls` and the program count
  changed in its transcripts: `PASS 8b4402e5` still), Pong (200 checkpoints,
  `PASS 814f72be`), the jobs session (40 presents, `PASS 408a6738`) and the
  menu session (185 checkpoints, `PASS b1f2b253`) on the emulator; the console
  (with its reboot and its typed-Enter twin) and jobs sessions on QEMU; and on
  Verilator the console session (piped and typed, 8,880,028 cycles), the second
  boot, the jobs session (40 presents) and the menu session (185 checkpoints,
  seeded waits), results-identical to the emulator, with Pong and the jobs
  session trace-identical in step-tick mode (1,168,126 and 6,742,051 lines) and
  `test-rv32-os` passing. The
  console session takes 1,448,225 steps against 1,797,082 before Track 3 on the
  same toolchain: the word-wise `memcpy` saves more than the two new `ls`
  lines and the page tables split around each guard cost (1,821,505 steps
  without it).
- **Tools:** `test-rv32-os` checks the RAM disk's new stack word (round trip,
  the Makefile's sizes, and four refusals: not whole pages, one page, the
  whole span, over the program).

## Running it

```sh
make run-rv32-lua-emu          # the Lua session on the emulator
make run-rv32-lua-qemu         # on QEMU, against the pinned transcript
make run-rv32-libc-rtl-verilator
```

Or by hand, at an interactive QEMU console, as in
[docs/rv32-os.md](rv32-os.md#at-an-interactive-qemu-console) with
`build/rv32/os/apps.disk` copied for the drive, then `lua` at the `$` prompt.
On Linux the Makefile needs `RV32_LLVM=/usr/bin RV32_LD=/usr/bin/ld.lld`.

To add a program on the C library: put its sources where the Makefile can
compile them with `RV32_OS_LIBC_CFLAGS`, list it in `RV32_OS_LIBC_PROGRAMS`
with a slot (and a span or a stack if it needs more), and link. If the link
names a missing function, rebuild picolibc with meson as
[third_party/picolibc/README.md](../third_party/picolibc/README.md) says and
run `tools/rv32_vendor_libc.py` with the new program's map.

## Exercises

1. `tty.c` and `sh.c` hold the same line editor twice. Move it into one place
   both can link. What does the shell lose if it starts depending on the C
   library?
2. Give tfs an append mode: what has to change in `fs_truncate`'s callers, and
   what does `fopen(name, "a")` then need from `syscalls.c`?
3. Lua's REPL reads with `fgets`. Add a history to `tty.c` (the up arrow
   recalls the previous line). Where does the escape sequence parser need to
   change?
4. Measure the C stack a `pcall` level costs with `-Os` instead of `-O2`,
   then with picolibc's `jmp_buf` for RV32I without F registers (`_JBLEN`). How
   small could Lua's stack be at the default 200 levels?
5. `time()` is 0 because there is no real-time clock. QEMU's tree has a
   `goldfish-rtc`. What would the kernel need to offer a `clock` system call,
   and how would the sessions stay comparable across backends?
6. Put `os.clock()` around `queens.lua 8` on the emulator and on Verilator.
   Why are the numbers different, and which of them is "seconds"?
