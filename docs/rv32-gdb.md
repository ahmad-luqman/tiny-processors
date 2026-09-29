# RV32 emulator: the GDB remote-protocol stub

Track 0 groundwork for kernel work: `rv32emu --gdb PORT` lets a stock GDB debug a guest on the [headless emulator](rv32-emulator.md) with source lines, symbols, registers, memory, breakpoints, single step, continue, and Ctrl-C. The stub speaks GDB's remote serial protocol (RSP) over a loopback TCP socket. Source: [tools/rv32_gdb.c](../tools/rv32_gdb.c) and [tools/rv32_gdb.h](../tools/rv32_gdb.h), linked into `rv32emu` only; tests: [tests/test_rv32_gdb.py](../tests/test_rv32_gdb.py).

The machine does not know it is being debugged. The stub runs the guest only through the core's `emu_run_until`, reads and writes memory through a debugger path that never touches a device, and keeps breakpoints in its own table. A run under GDB therefore executes the same steps, writes the same trace, and ends with the same halt line and exit status as a run without it; the tests compare the two byte for byte.

## Running it

```
make debug-rv32-gdb                                   # selfcheck.bin on port 3333
make debug-rv32-gdb IMAGE=build/rv32/capstone.bin PORT=4444
build/rv32/rv32emu --image FILE [other options] --gdb PORT   # PORT 0 picks a free port
```

The emulator loads the image, applies every other option as usual (`--trace`, `--checkpoints`, `--input`, `--record`, `--max-instructions`, `--dump-state` all work under the debugger), prints one line to stderr, and waits for one client:

```
rv32emu: gdb listening on 127.0.0.1:3333
```

In a second terminal, with the ELF that the flat image came from:

```
gdb-multiarch build/rv32/selfcheck.elf -ex 'set architecture riscv:rv32' -ex 'target remote :3333'   # Linux
riscv64-elf-gdb build/rv32/selfcheck.elf -ex 'set architecture riscv:rv32' -ex 'target remote :3333' # macOS (brew install riscv64-elf-gdb; Homebrew's multi-target gdb works too)
```

The machine is stopped at the reset pc, `0x8000_0000` (or `--pc`). Then the usual commands work: `break main`, `continue`, `stepi`, `info registers`, `info registers mtvec fcsr`, `p $ft0`, `x/8wx 0x80000000`, `set $a0 = 5`, `set {int}0x80001000 = 1`, Ctrl-C, `detach`, `kill`. A session with the self-check:

```
(gdb) break main
Breakpoint 1 at 0x80000074: file programs/rv32/selfcheck.c, line 87.
(gdb) continue
Breakpoint 1, main () at programs/rv32/selfcheck.c:87
87	    CHECK(1, *(volatile uint32_t *)&g_init, 0x12345678u);
(gdb) stepi
(gdb) x/4wx 0x80000000
0x80000000 <_start>:	0x00040117	0x00010113	0x00001297	0xef028293
(gdb) continue
[Inferior 1 (Remote target) exited normally]
```

The emulator prints `PASS 807d9fad` on stdout and `rv32emu: halt=done ... pass` on stderr and exits 0, as it would have without the debugger.

The socket is bound to `127.0.0.1` only. The stub reads and writes the whole machine without authentication, so it is not offered to the network; to debug from another host, forward the port over SSH.

## Packet set

| Packet | Reply | Notes |
| --- | --- | --- |
| `qSupported` | `PacketSize=4000;qXfer:features:read+;swbreak+;hwbreak+;QStartNoAckMode+;vContSupported+` | Stop reasons `swbreak`/`hwbreak` are sent only when the client's `qSupported` offered `swbreak+`, as the protocol requires |
| `qXfer:features:read:target.xml:OFF,LEN` | `m…`/`l…` chunks | The [target description](#registers-and-the-target-description); any other annex is `E00` |
| `?` | `T05` | Always stopped when the stub is listening |
| `g` / `G` | 65 registers | x0–x31, pc, f0–f31 in that order, each eight hex digits little-endian. The CSRs are not in the block; gdb reads them with `p` |
| `p N` / `P N=V` | value / `OK` | Any register number of the description, plus any other CSR number the core knows; `E01` otherwise |
| `m A,L` / `M A,L:HEX` / `X A,L:BIN` | data / `OK` | RAM and framebuffer only ([below](#why-debugger-memory-access-skips-the-devices)); `E01` elsewhere. `X` with length 0 is gdb's probe and answers `OK` |
| `c [A]`, `s [A]`, `C sig[;A]`, `S sig[;A]`, `vCont?`, `vCont;c`, `vCont;s` (also `C`/`S` and `:thread` suffixes) | stop reply | The signal of `C`/`S` is ignored: nothing in the machine could receive it. Only the first `vCont` action is used; there is one hart |
| `Z0`/`z0`, `Z1`/`z1` `,A,KIND` | `OK` / `E01` | Software and hardware breakpoints share one table of 64 entries; inserting twice is idempotent, removing an absent one is `E01`. `Z2`–`Z4` (watchpoints) get the empty reply: unsupported |
| `D` | `OK` | Detach: the connection closes and the run continues as a normal run |
| `k`, `vKill` | none / `OK` | Kill: the run stops (see [exits](#stops-and-exits)) |
| `qAttached` `1`, `qC` `QC1`, `qfThreadInfo` `m1`, `qsThreadInfo` `l`, `qOffsets` `Text=0;Data=0;Bss=0`, `qSymbol` `OK`, `H…` `OK`, `T…` `OK` | | One thread, id 1, always alive; the ELF is linked where it runs |
| `QStartNoAckMode` | `OK` | Acks stop after this reply |
| anything else | empty | The protocol's "unsupported" |

Framing: in acknowledgement mode every packet's checksum is verified. A bad checksum, a non-hex checksum digit, or a payload longer than `PacketSize` (16,384 bytes) is answered with `-` and dropped, and gdb retransmits; the stub resends a reply on `-` (up to 16 times, then gives up on the client). After `QStartNoAckMode` nothing is retransmitted, so a dropped packet would leave gdb waiting forever: the stub no longer verifies checksums (the protocol lets the receiver ignore them in no-ack mode, and the transport is TCP), and answers an oversized packet with `E01`. An oversized packet is a client bug in either mode and also prints `rv32emu: gdb: dropped a packet longer than PacketSize` on stderr. A read (`m`, `qXfer`) longer than a reply can carry is answered with its first 8,184 bytes (0x1ff8, room for hex or worst-case escaping), a short reply the protocol allows; gdb asks again for the rest. Stray `+`/`-` and late Ctrl-C bytes between packets are ignored. A request that is well framed but malformed (a missing length, a nine-digit number, non-hex data, a data length that disagrees with the header) gets `E01`. Binary replies escape `$`, `#`, `}`, and `*` (gdb reads a bare `*` as run-length encoding).

## Registers and the target description

The stub sends a target description so gdb needs no guessing about the register file. It uses gdb's own RISC-V numbering and feature names, the ones in gdb's `features/riscv/32bit-*.xml`:

| Feature | Registers | gdb register numbers |
| --- | --- | --- |
| `org.gnu.gdb.riscv.cpu` | `zero ra sp gp tp t0–t2 fp s1 a0–a7 s2–s11 t3–t6` (x0–x31), `pc`, 32 bits | 0–31, 32 |
| `org.gnu.gdb.riscv.fpu` | `ft0–ft7 fs0 fs1 fa0–fa7 fs2–fs11 ft8–ft11` (f0–f31) as `ieee_single`; `fflags`, `frm`, `fcsr` | 33–64; 66, 67, 68 |
| `org.gnu.gdb.riscv.csr` | `mtvec`, `mepc`, `mcause`, `mtval`; the Zicntr counters `cycle`, `time`, `instret`, `cycleh`, `timeh`, `instreth` | 65 + CSR number: 838, 898, 899, 900; 3137, 3138, 3139, 3265, 3266, 3267 |

`<architecture>riscv:rv32</architecture>` tells gdb the XLEN. Every `<reg>` carries an explicit `regnum`, so the CSRs sit at 65 + number without filler registers for the gaps. `p` also answers any other CSR number the core's `csr_read` accepts (a counter CSR added later is readable at 65 + its number without touching the stub); unknown numbers, such as `mstatus` (65 + 0x300), are `E01`.

Writes follow the machine's rules, not the debugger's convenience: a write to x0 is accepted and discarded (x0 stays zero, as with an instruction); pc takes any value (a misaligned pc traps on the next step, as it would after a jump); f registers take any bit pattern; CSRs go through `emu_csr_write`, which applies the same WARL masks as `csrrw` (`mtvec`/`mepc` bits 1:0 read back zero, `fcsr` keeps eight bits, `fflags` five, `frm` three). The read-only Zicntr counters refuse a write with `E01`, as `csrw` to one traps.

## Stops and exits

- **Step** (`s`, `vCont;s`): exactly one instruction step of the machine, `emu_run_until(m, 1)`. A trapping instruction is a step too: afterwards pc is at `mtvec` and `mcause`/`mepc`/`mtval` are set, just as the emulator counts a trap in `steps`. Reply `T05`.
- **Continue** (`c`, `vCont;c`): runs until a breakpoint address is about to execute, Ctrl-C, or a halt. The pc is compared with the table *before* each step, except the first step of the continue, so continuing from a breakpoint makes progress. A breakpoint stop is `T05swbreak:;` (or `T05hwbreak:;` for a `Z1`), with pc at the breakpoint address and the instruction there not yet executed.
- **Ctrl-C** (a raw `0x03` byte while the guest runs) stops it with `T02` (SIGINT) between two instructions.
- **Halt.** When the guest halts (done register, double fault, instruction limit) the stub leaves the session, main flushes the outputs, writes the state file and the halt line exactly as a plain run does, computes the exit status with `emu_exit_status`, and only then sends `W` with that status: `W00` for the pass word, `W01`…`Wff` for a fail word's code, `W02` for an emulator error (double fault, limit, a pass rejected for lost events or incomplete outputs). gdb shows "exited normally" or "exited with code NN", and the process exits with the same status. Because `emu_run_until` checks the instruction limit before its budget, the step that executes the last allowed instruction already ends the session with `W02`.
- **Detach** (`D`): `OK`, the socket closes, and the run continues to its halt as if no debugger had been attached (same outputs, same status). gdb detaches this way when a batch script ends or on `detach`/`quit`.
- **Kill** (`k`, `vKill`) or **hang-up** (the client disconnects): the machine halts as `stopped`, the halt line reads `halt=stopped ... error=host-stopped pc=...`, and the process exits 2. This reuses the halt reason the window uses when it is closed; no new halt kind was needed. A kill is quiet; a lost connection says so first with one stderr line, `rv32emu: gdb: connection closed by client` for an orderly close or `rv32emu: gdb: connection lost: <error>` (for example `Connection reset by peer`) for a socket error, so a crashed client is not mistaken for a deliberate kill.

## Why breakpoints are a table and not patched `ebreak`s

The textbook stub writes `ebreak` over the instruction and restores it on removal. Here that would be visible to the guest: a load of its own code would read `00100073`, a checksum over the text would change, the trace would show an `ebreak` trap and its `mcause`/`mepc`/`mtval` writes, and with the M1 firmware's `mtvec = 0` the patched instruction would double-fault instead of stopping. It would also break the trace-identical comparison with the RTL. The table costs a comparison per instruction during a continue with breakpoints set, and nothing at all without them, because the guest then runs in batches at full speed.

## Why debugger memory access skips the devices

A debugger reads memory to show it, not to act on it. Through the guest's load path, `x/wx 0x20001000` would pop an event off the input queue (EVENT is a destructive read), a write to `0x1000_0000` would print a console byte, a write to PRESENT would present a frame (a checkpoint line and a PPM), and an accelerator's command register would start work. The core's `emu_debug_read`/`emu_debug_write` copy bytes of RAM (`0x8000_0000`, 4 MiB) and the framebuffer (`0x3000_0000`, 76,800 bytes) directly, and refuse every other address with `E01`, including the device windows, the unmapped space, and a range that crosses the end of RAM or wraps past `0xffff_ffff`. The debugger is also exempt from the G1 blit-source and G2 depth locks and from the "engine owns the framebuffer" rule, which constrain the guest's CPU, not an observer. Device state is still inspectable after the run with `--dump-state`, and the guest's own loads are traced.

## The cost of Ctrl-C polling

A continue without breakpoints runs `emu_run_until` in batches of 65,536 instructions and calls `poll()` on the socket between batches (and after a present, but counted on instructions, so a guest that presents often does not poll more). Measured in this container on a three-instruction loop bounded at 200,000,000 instructions: 2.60 s without the stub, 2.48 s under `continue` (the difference is noise); a Ctrl-C lands within one batch, about a millisecond at this container's 77 M instructions/s. With a breakpoint set the guest runs one `emu_run_until(m, 1)` per instruction plus a table lookup: 20,000,000 instructions took 0.32 s, about 20% slower than full speed.

## Core API added for the stub

Appended to [rv32emu_core.c](../tools/rv32emu_core.c) and declared in [rv32emu_core.h](../tools/rv32emu_core.h):

```c
bool emu_debug_read(const machine *m, uint32_t addr, uint8_t *out, size_t n);  /* RAM or framebuffer only */
bool emu_debug_write(machine *m, uint32_t addr, const uint8_t *in, size_t n);
bool emu_csr_read(const machine *m, uint32_t number, uint32_t *value);         /* csr_read, by number */
bool emu_csr_write(machine *m, uint32_t number, uint32_t value);               /* csr_write's WARL masks; false for read-only */
```

The stub's own API is three calls that `rv32emu.c` makes around its run loop: `gdb_accept(port)`, `gdb_serve(fd, m)` (takes ownership of the socket; once per process; returns `GDB_HALTED`, `GDB_DETACHED`, or `GDB_KILLED`), and `gdb_report_exit(status)`, which sends `W` and closes the socket only when the session ended `GDB_HALTED` and does nothing otherwise, so `main` calls it unconditionally. The stub runs the guest only through `emu_run_until` and touches memory only through the debugger path; like `rv32emu.c` and `rv32win.c` it reads and sets `x`, `f`, `pc`, and (on a kill) `halt` in the machine struct directly.

## Limitations

- One client per run; after it detaches or disconnects the port is closed. No `extended-remote` restart (`run` in gdb): start the emulator again.
- No watchpoints (`Z2`–`Z4`); gdb falls back to software watchpoints by single-stepping, which works but is slow.
- No reverse execution, no non-stop mode, no multiprocess extensions, no `qXfer:memory-map` (gdb's default of "all memory is RAM" applies; accesses outside RAM and the framebuffer fail with "Cannot access memory").
- Memory reads are all-or-nothing per request: a read that starts in RAM and runs past its end is `E01`, not a short reply.
- The window (`rv32win`) does not link the stub; debug an interactive program headless with its recorded input script.
- The RTL has no debug module; this is an emulator feature only.

## Verification

`make test-rv32-gdb` (part of `make test-rv32`) builds the emulator from source and runs 31 tests in [tests/test_rv32_gdb.py](../tests/test_rv32_gdb.py). A client written in the test (framing, checksums, acks, binary escapes, strict about every byte the stub sends) drives `rv32emu --gdb 0`, which reports its chosen port on stderr. Protocol and framing tests run on small programs assembled in the test, so only the tests that need the self-check's own trace, symbols, or console skip when it is not built:

- the target description, fetched in 256-byte chunks, parses as XML with 33 cpu registers numbered 0–32, 32 `ieee_single` registers at 33–64, `fflags`/`frm`/`fcsr` at 66–68, and the four trap CSRs and six counters at 65 + number; `qSupported` advertises it;
- the initial stop at `0x8000_0000` with zero registers; the thread queries; empty replies for unknown packets and watchpoints;
- 300 single steps of the self-check visit the pcs of the plain run's `--trace` in order, and each register a trace line wrote holds that value after the step; a trapping `ecall` is one step to `mtvec` with `mcause` 11;
- a run under the stub with `--trace` (ten steps, a breakpoint stop, a continue to the end) writes a trace identical to the plain run's;
- a breakpoint on `fib` (from `llvm-nm`) stops exactly as many times as the trace executes `fib`'s first instruction, with `swbreak`; `z0` removes it and the run ends `W00`; breakpoints by instruction index in an assembled program, `Z1` reported as `hwbreak`;
- Ctrl-C stops a self-loop with `T02` at the loop, twice, with and without a breakpoint set;
- exits: the self-check continues to `W00`, exit 0, `PASS 807d9fad` (the Makefile's `RV32_SELFCHECK_HEX`) and the plain run's halt line; a fail word gives `W01` and exit 1; the instruction limit `W02`; detach runs to a pass; kill, `vKill`, hang-up, and a reset connection give `halt=stopped`, exit 2, with the connection-lost line for the last two and none for a kill;
- memory: the image read back byte for byte, zeros in the framebuffer, `E01` for unmapped, device, crossing, and wrapping ranges; `m80000000,4000` clamped to 0x1ff8 bytes; `M` and `X` (with all four escaped bytes) written and read back, `X` with fewer or more bytes than its length refused; patching the next instruction changes what executes; reads of the input window's EVENT/COUNT/KEYS are refused and the guest still sees both scripted events;
- counters: `cycle`, `time` and `instret` read the steps taken, their high halves zero, and a write is refused (added with Track 0's Zicntr);
- registers: x0 writes discarded, `mtvec`/`mepc`/`fcsr`/`fflags`/`frm` WARL masks, `mstatus` refused, `G` round trip, pc writes and `c ADDR`/`s ADDR`, and a debugger write of x31 turning a fail word into a pass;
- breakpoints: 64 fit, the 65th (`Z0` or `Z1`) is `E01`, re-inserting one is still `OK`, removing one makes room;
- framing: bad and non-hex checksums get `-`, an oversized packet gets `-` and a stderr line and the next packet works, a rejected reply is resent, stray acks are ignored, malformed requests get `E01`, no-ack mode works, and in it an oversized packet gets `E01` and bad or non-hex checksums are not verified; a taken port and an out-of-range port are refused with exit 2;
- one end-to-end run of a real gdb (`gdb-multiarch`, `riscv64-elf-gdb`, or a plain `gdb` that accepts `set architecture riscv:rv32`, such as Homebrew's, from PATH; skipped with the reason when none exists): `target remote`, `break *main`, `continue`, `info registers pc`, `stepi`, `x/4wx 0x80000000`, `continue` to "exited normally", with the emulator exiting 0 and printing the pass line.

## Exercises

1. **Watchpoints.** Implement `Z2` (write watchpoint) by comparing each step's `mem_write`/`mem_addr` effect fields with a watch table after `emu_run_until(m, 1)`, and report `T05watch:ADDR;`. Why is the stop *after* the store here, and what does gdb expect?
2. **Patched breakpoints.** Replace the table with `ebreak` patching and run `test_trace_under_gdb_is_the_plain_trace`. Which lines of the trace change, and what happens with the self-check's `mtvec = 0`?
3. **Poll interval.** Change `POLL_INTERVAL` to 1 and to 2^24 and measure the loop image under `continue` and the Ctrl-C latency. Where is the knee?
4. **Device reads.** Add a `monitor devices` command (`qRcmd`) that prints the input queue, the timer, and the display counters from the `machine` struct without calling any load handler.

## Acceptance record (2026-09-29)

Run in the Linux container (Ubuntu clang/lld 18 for the firmware, gcc for the host, GDB 15.1 `gdb-multiarch`):

- `make RV32_LLVM=/usr/bin RV32_LD=/usr/bin/ld.lld test-rv32-gdb test-rv32-emu run-rv32-emu` passes: `test-rv32-gdb` 25 tests (26 with the counter test added when Zicntr merged), 0 skipped (the real-gdb and `llvm-nm` tests ran), about 3 s; `test-rv32-emu` 33 tests unchanged; `run-rv32-emu` prints `PASS 807d9fad`, exit 0, 33,226 instructions retired, 0 traps.
- The emulator builds with `-std=c11 -O2 -Wall -Wextra -Werror` under gcc, and the stub also compiles cleanly with clang `-Wall -Wextra -Werror -Wimplicit-fallthrough`.
- The self-check under the stub gives the same stdout, halt line, exit status, and trace as without it.
- Speed as measured [above](#the-cost-of-ctrl-c-polling): no measurable cost without breakpoints, about 20% with one set.

Not run here: the Mac toolchain (`riscv64-elf-gdb` from Homebrew, Apple clang). The stub uses only POSIX sockets and `poll`, with `_DARWIN_C_SOURCE` set on macOS as the core does.

Review fixes (2026-09-29, macOS, Apple clang 21, Homebrew GDB 17.2): `make test-rv32-gdb` 31 tests, 0 skipped, the real-gdb test running Homebrew's `gdb`; `make test-rv32-emu` 33 tests; `run-rv32-emu` and `run-rv32m-emu` pass; the self-check, diagnostic, Pong, and capstone (RV32I and RV32IM builds) give traces, state files, checkpoints, stdout, and stderr byte-identical to the emulator before the fixes.
