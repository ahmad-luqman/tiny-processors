# Doom on the RV32 OS (Track 3, B3)

[Issue #35](https://github.com/ahmad-luqman/tiny-processors/issues/35) asks for doomgeneric running as a C library program on the OS, with its 320×200 output drawn into the 320×240 framebuffer. The work is two PRs, as the issue allows:

1. **The platform:** what Doom needs from the machine and the kernel.
2. **Doom:** the port itself.

The first PR's sections run from "What stood in the way" to "Keys"; [The port](#the-port) onward is the second's.

## What stood in the way

| Need | Before | Why it blocked Doom |
| --- | --- | --- |
| RAM | 8 MiB, 56 slots of 128 KiB | doomgeneric's zone is a fixed 6 MiB (`DEFAULT_RAM` and `MIN_RAM` are both 6 in its `i_system.c`), and the code, data, screen buffer and stack come on top of that |
| A place for the program | Every program is copied into the kernel's 1 MiB image, which had 244 KiB free | Doom's image is several hundred KiB |
| A place for the WAD | The disk is 128 KiB on every backend | The shareware `doom1.wad` is 4,196,020 bytes |
| Colours | `0x1100_3000` is reserved and unmapped; pixels are a fixed RGB332 mapping | Doom draws 256 palette indices and changes the palette (damage, pickups) |
| Keys | 14 key codes | Doom wants fire, run, the automap, yes and no, and weapon keys |

## RAM is 16 MiB

RAM doubles again, to 16 MiB, on every backend:
- the emulator's `RAM_SIZE`;
- every RTL module's `RAM_WORDS` and the testbenches;
- QEMU's `-m 16M`;
- the device tree and boot ROM;
- `OS_SLOTS` (120) and the RAM-disk tool.

16 MiB is the issue's own example, and it is also what Linux (#36) inherits.

The slot base (`0x8010_0000`) and size (128 KiB) do not change, so no program moves and no session transcript changes. The slots now touch four 4 MiB regions, so each process-table entry needs seven page tables: the root, four for the slots, one for the framebuffer and one for the engines. That is 224 KiB for eight entries. The kernel's MiB still has 180 KiB free: `.pagetables` ends at `0x800C_F000` and the stack starts at `0x800F_C000`.

**What moved:**
- `platcheck` folds the memory size into its word, so `PASS 91659113` became `bee59113`.
- QEMU's device-tree fixture was re-dumped from QEMU 11.1.2 at 16 MiB. QEMU now puts its tree at `0x80E0_0000`, inside slot 104. The kernel reads the tree once, in `discover()`, before the first spawn, so this is safe, just as slot 40 was at 8 MiB.
- The arch suite's device-tree offset moves to 14 MiB.
- The end-of-RAM fault cases, the debugger's edge reads and G2's depth-buffer edge all move to the new end.

**Cost:**
- The testbench zero-fills RAM at start, and Icarus feels it. With the testbench already built (the second of two runs on each side), `make run-rv32-platform-rtl` takes 40.5 s against 36.8 s on main: about 3.7 s more per Icarus run. Verilator's start-up is not noticeably slower. A plusarg that sized RAM per run would win that back; it was not worth a second RAM size in the contract. The full aggregate (`test-rv32-full`, `-j8`) took 16 min 56 s, against 14 min 23 s for #34's, which also includes the new tests.
- Synthesis shrinks RAM to 64 words, so its cell count is unaffected.

## A disk of any size

tfs already stored a file the host adds as one contiguous extent of any length (`rv32_mkfs.py` gives it `ceil(size/512)` sectors), and `fs_read` already walked sectors. The 4 KiB is only what the *kernel* gives a file it creates. The real limit was the disk, so that is what changed. The issue's choice between multi-sector extents and a read-only blob therefore resolves to extents, which tfs already had (user decision, 2026-10-05).

A disk is now any whole number of 512-byte sectors up to 8 MiB, and the capacity the device reports is the disk's size:
- **Emulator:** `--disk` takes such a file. Without one, the disk is 128 KiB of zeros, as before.
- **RTL:** `DISK_WORDS` is the 8 MiB maximum. A new `disk_sectors` input, the disk in the drive, gives the capacity and bounds requests. The testbench sets it from `+disk=`'s word count and clears, loads and saves only those words.
- **`rv32_mkfs.py --new --size`** makes a disk of another size. A disk's superblock must give its own sector count.
- **Kernel:** `fs_read` moves whole sectors straight into a word-aligned buffer in RAM, one request per run of sectors. It no longer bounces 512 bytes at a time, which matters for a 4 MiB WAD. A partial sector still goes through the sector buffer, so no byte past the request or the file is written.

Every existing session keeps its 128 KiB disk, so none of their transcripts or PASS words moved. The SoC grew from 336,242 to 337,226 cells for the runtime capacity comparisons.

## Programs on the disk

A program can live on the disk instead of in the kernel's image. Its file on the disk is a RAM disk of one program, which is exactly the format `tools/rv32_ramdisk.py` already writes. When the RAM disk has no program with the name being spawned, the kernel:
1. opens the tfs file of that name;
2. reads the header and the entry;
3. checks the entry exactly as the boot check checks the RAM disk's (one function, `entry_ok`, against the file's size, now also refusing any flag the kernel does not know), and requires the entry's name to be the file's and its flags to be none;
4. reads the image into the program's slots with the whole-sector path.

A program in the RAM disk comes first, so a file named `hello` cannot replace the built-in one. A file that is being written cannot be run. When a file exists but cannot run, the kernel says why before the shell's `cannot run`:
- `being written`;
- `not a program` (a bad header);
- `a program that breaks the slot rules`;
- `a program filed under another name`;
- `a program on the disk may not drive the accelerators`.

The RAM disk's table is unchanged, so the boot line still says `20 programs` and no transcript moved.

**Trust.** A RAM-disk program's entry comes from the kernel's image. A disk program's entry comes from a file that any program may write (`write` on a new file name). So the entry is untrusted. `entry_ok` checks its geometry: the image inside the file, the span inside the slots, and the stack whole pages inside the span. What it may ask for is limited too:
- It runs in user mode in its own slots, mapped and PMP-bounded as any process is.
- `spawn` refuses it while its slots overlap a live process's.
- It may not drive the accelerators. Only the kernel's own image grants them. A disk program that asked for them could keep an engine busy forever, and while an engine is busy the scheduler runs only the programs that drive the engines, so the shell would starve (the six-agent review found this).

A file the kernel creates is at most 4 KiB, so a program written on the machine can only be a small one. The large ones come from the host.

`diskprog` is the test. It carries 256 KiB of a generated sequence (the assembler writes it from a `.rept`) and is linked into slots 100–103, above 8 MiB. It sits just past 4 MiB into a 6 MiB disk, behind a 4 MiB `pad` file whose byte `i` is `i mod 256`. Its session:
- runs it, and it checks every word;
- refuses `pad` (not a program) and `renamed` (the same file under another name), each with the kernel's reason;
- runs the RAM disk's `hello`, although a disk file of that name exists;
- runs `palcheck`, then `palcheck leave`, then `palcheck` again (below);
- runs `readcheck`, a user program's reads of `pad` on every path of `fs_read`. It reads whole sectors into an aligned buffer, the same into a misaligned one, mid-sector to mid-sector, and a sector and a part followed by a canary word. It reads past the end of the file followed by a canary, and at the end. Its own second instance is refused while it runs;
- writes a file with `write`, far past the old 128 KiB, and reads it back with `cat`;
- lists the files.

QEMU, the emulator and Verilator agree (`PASS d4d49003`), and the 6 MiB disks they leave are identical. Verilator takes about 30 s.

A unit test (`DiskProgramTest`) breaks a good program file one clause at a time, 17 ways (a short header, a count of 2, a reserved word, a cut image, an offset past the file, a file larger than its memory, a load below, between or past the slots, a span past them, a stack that is not whole pages or fills the span, memory that runs into the stack, an entry outside the image, a name with no NUL, an unknown flag, the accelerators flag). It checks each is refused with its reason, a good one still runs, and the shell carries on.

## The palette

The window M6 reserved is built on every backend: 256 words at `0x1100_3000`.
- **Format:** word `N` is the colour of pixel value `N`, as `0x00RRGGBB`.
- **Access:** words only. Any other width faults, and so does a fetch. The top byte reads as zero, and a write keeps the low 24 bits.
- **Power-on contents:** the RGB332 mapping the pixels always had. A program that never writes the palette looks exactly as before.
- **Reset:** leaves it as it is, like the framebuffer.

The checkpoint hash covers pixel indices only, so Pong's 200 checkpoints and `PASS 8fef54bc` are unchanged by construction, and `run-rv32-pong-emu` confirms it.

| Where | What |
| --- | --- |
| Emulator | A region in the device table. The PPM writer and the window colour a frame through the palette as it is at the present; the window rebuilds its table at every present |
| RTL | [rv32_palette.v](../rtl/rv32/rv32_palette.v): a 256×24 array, filled at power-on from two level tables (the RGB332 fields scaled with truncation), with a bus port and select. Synthesis shrinks it to 16 entries as it shrinks RAM: 338,990 SoC cells, 1,764 more |
| Device tree | The display node's third `reg` entry. `check-rv32-virt-map` stays clean: the window is in the hole between `fw_cfg` and flash |
| Kernel | `SYS_PALETTE` (22): `palette(colours, direction)` reads all 256 entries into a word-aligned user buffer (`OS_PALETTE_READ`) or writes them from it (`OS_PALETTE_WRITE`). The kernel keeps the palette it found at boot and puts it back when the process that last wrote the palette finishes, so a program that sets colours (Doom) and exits, or is killed, leaves the next one RGB332. A display without a palette is a boot panic, as one without a framebuffer is. It is an error on a machine without a palette (QEMU), for a misaligned buffer, for another `set` value, or for memory that is not the caller's. PMP has no free entry, so the window is not mapped into programs |
| `platcheck` | Prints the window and checks four power-on colours. The palette line does not change its word (the RAM size did); the expected console gains a line |

**Tests:**
- The emulator and RTL tests hash every power-on word and check the top byte, the sub-word and fetch faults, trace for trace.
- The PPM shows a written colour.
- The old unmapped-palette faults moved to `0x1100_3400`, the unmapped rest of that page.
- `palcheck`, a disk program next to `diskprog`, reads the RGB332 palette through the kernel. It then sets a palette whose words carry a top byte, reads it back with that byte dropped, restores it and reads that back.
  - It checks five refusals: a misaligned buffer, an unknown direction, memory that is not its own, a buffer past the top of its memory, and one into its guard page.
  - `palcheck leave` sets a palette and exits, and the next `palcheck` finds RGB332 again: the kernel's restore.
  - On QEMU it prints `no palette`. On a machine with a display, a missing palette is a failure.
- An RTL test writes a palette word, resets with `+reset-at` and reads the word back. An emulator test shows that each of two frames is coloured by the palette at its own present.

## Keys

Codes 15 to 26 are added: `CTRL`, `SHIFT`, `TAB`, `Y`, `N` and `DIGIT1` to `DIGIT7`. They are in `board.h`, the emulator's name table, the testbench, `rv32_devices.py` and the window, where either Ctrl or either Shift is the one key. The digits are named rather than `1`..`7` because an input script already reads a bare number as a raw code. The fourteen existing codes do not move. A new RTL test reads every named key from a script on both backends, written in upper and in lower case. The window counts the two host keys that share a code: the contract's key is pressed with the first and released with the last.

## Evidence (platform PR)

- `make test-rv32`: every target passes on macOS.
  - Under `-j8` load QEMU once truncated `F/fsub_b11-01`'s console. That is issue #41, and the suite passed 198/198 when rerun alone.
- `run-rv32-diskprog-{qemu,emu,rtl-verilator}`: `PASS d4d49003`, with identical disks.
- `run-rv32-pong-emu`: 200 checkpoints and `PASS 8fef54bc`, unchanged.
- `run-rv32-platform-{qemu,emu,rtl,rtl-verilator}`: `PASS bee59113`.
- `lint-rv32` and `lint-rv32-soc` are clean. `synth-rv32-soc` gives 338,990 cells.

## The port

Frame 120 of the demo, written as a PPM by the emulator (`rv32emu --frames`), shows E1M1 in its own colours, letterboxed between black bands 20 rows tall, with "PICKED UP THE ARMOR" at the top. The picture is not committed: it is id's artwork, which stays out of the repository like the WAD.

### Licences

- **doomgeneric** ([ozkl/doomgeneric](https://github.com/ozkl/doomgeneric), `master` at `dcb7a8db`) is GPL-2.0. It is vendored unmodified in [third_party/doomgeneric](../third_party/doomgeneric/README.md): the 80 sources its own Makefile compiles besides the X11 back end, every header, and `LICENSE`. The `doom` program built from it is therefore GPL too; nothing else links it.
- **The shareware `doom1.wad` v1.9** is not free software. id's licence lets anyone copy it to give to others, unmodified and free of charge. The copyright file in Debian's `doom-wad-shareware` package keeps John Carmack's 1999 clarification that "the DOOM shareware wad is freely distributable".
  - It is never committed. [tools/rv32_doom.py](../tools/rv32_doom.py) (`make fetch-rv32-doom-wad`) downloads that Debian package and checks its SHA-256. It takes out the WAD and checks its SHA-256 `1d7d43be…` (4,196,020 bytes, MD5 `f0cefca4…`, the Doom Wiki's v1.9). It writes the copyright file, then the WAD, each whole or not at all, into the git-ignored `third_party/doom-wad/`.
  - The mirrors that served the bare file directly were gone (404). The archive's pool keeps a version only while a release has it, so the tool falls back to snapshot.debian.org, which serves the same package by its SHA-1 for good. Both are checked against the same SHA-256.
  - A target that needs the WAD fetches it, so the first `make test-rv32` needs the network. `--check` names the fetch target when the WAD or its licence is missing or wrong.

### Building it

- **The build.** doomgeneric is built as its own Makefile builds it, with `CMAP256` and 320×200.
  - `CMAP256` makes the screen buffer palette indices, and makes the palette the global `colors[]` with a `palette_changed` flag. The platform file reads both, so the engine needs no change.
  - It is built for **rv32im**: the core has M, Doom's fixed point multiplies constantly, and rv32im objects link with the ilp32 C library.
  - The upstream code is built with warnings off (`-w`), as the vendored C library is; the platform file is built with `-Wall -Wextra -Werror`.
- **What the C library lacked.** The link needed eight picolibc functions no earlier program used: `atof`, `atoi`, `strcasecmp`, `strncasecmp`, `strdup`, `strncpy`, `putchar` and `vsnprintf`. They were copied unmodified from the pinned picolibc commit and added to its `SOURCES`.
  - It also needed `mkdir`, which the OS layer now refuses with `ENOSYS`, as it does `stat`; `libccheck` checks both.
  - Doom asks for its config directory `.` and its save directory `./.savegame/`. tfs has no directories, so **saving a game ends Doom**. Its save file `./.savegame/temp.dsg` is 20 bytes, one more than a tfs name holds. Doom then writes a recovery file, `/tmp/recovery.dsg` (a legal tfs name, at most 4 KiB), and stops with `I_Error`. Loading and saving are out of scope here.
- **Where it runs.** `doom` is a disk program, a 474 KB image, in 64 slots, 27 to 90. That is 8 MiB for its image, the 6 MiB zone and its heap, crossing into the RAM 16 MiB added, with a 64 KiB stack. `rv32_ramdisk.py --check` caught a first placement at slot 56 colliding with the test programs.
  - Its disk is 5 MiB: `doom1.wad` and the program.
  - Doom saves its config (`.default.cfg`) only when it quits or stops on an error (`I_Quit`, `I_Error`). The measured runs end with `exit()` after their last frame, which skips that, so they leave the disk as it was; the disk comparison shows that nothing wrote to it. Quitting from the menu in the window does save, which is why `run-rv32-doom` plays on a copy.

### The platform file

[doom_rv32.c](../programs/rv32/os/libc/doom_rv32.c) implements doomgeneric's hooks:
- **`DG_DrawFrame`**:
  - writes a changed palette with `SYS_PALETTE`, then reads it back. On a machine with a display, a palette the device does not hold stops Doom with an error, so a palette call that fails cannot pass unnoticed.
  - copies the 320×200 picture into framebuffer rows 20 to 219 and presents; a refused present is an error too;
  - every 35 frames prints `doom: frame N screen <hash> palette <hash>`.

  The hashes are the checkpoint hash over Doom's screen buffer and over its 256 palette words, computed by the program, so QEMU, which has no display, prints the same lines.
- **`DG_GetKey`** maps input events to Doom's keys, by `board.h`'s names:
  - Left and Right turn, Up and Down (or W and S) move, A and D strafe;
  - Ctrl fires, Space opens, Shift runs, 1 to 7 pick a weapon;
  - Tab shows the map, Escape the menu, Y and N answer it, Enter confirms, P pauses. Q and R are not Doom's.
- **`DG_GetTicksMs` and `DG_SleepMs`: a virtual clock.** This departs from the issue, which asked for time from `mtime`. The clock is a count of milliseconds that only `DG_SleepMs` advances.
  - Doom waits for its clock in two places: `TryRunTics` before a tic, and the screen wipe when the screen changes (the title, a new level, an intermission). Both sleep a millisecond at a time while they wait, so each present comes one tic after the last, wipe frames included.
  - A timedemo never waits in `TryRunTics`: it runs one tic per frame by itself. In play, the first frame after a wipe may run the tics the wipe let pass.
  - With `mtime`, the wipe would draw as many frames as fit in the device time it took, and device time per frame differs between QEMU, the emulator and the RTL. The same demo would give each backend a different frame count.
  - In the window, presents are paced at 35 a second, Doom's tic rate, and that is the game's real speed, as for Pong.
- **`-frames N`** (N a count above 0; anything else is an error) ends the run after N frames, with `exit()`, not `I_Quit`: no config is saved and a timedemo's end-of-demo report (an `I_Error`) never comes. **`-fps`** adds a line with the device ticks the frames took, the low word of `mtime` (100 ns on QEMU, a step on the emulator, a cycle on the RTL). It depends on the backend, so the cross-backend sessions leave it out.

### The pinned runs

- `doom -iwad doom1.wad -timedemo demo1 -frames 350` plays ten seconds of the shareware demo.
  - The timedemo sets `singletics`, and the virtual clock covers the wipe, so the run is the same everywhere.
  - Both the emulator (`run-rv32-doom-emu`) and QEMU (`run-rv32-doom-qemu`) print the same ten screen hashes, ending `frame 350 screen ed9ddef9 palette 2002492b`, and `PASS 1af5ac24`. Their transcripts differ only in the two lines that name the platform.
  - The emulator also pins every frame's framebuffer checkpoint: 350 lines, [doom.checkpoints](../programs/rv32/os/doom.checkpoints).
- `-frames 35` runs on the emulator and Verilator (`run-rv32-doom35-rtl-verilator`, in the slow tier). The console, all 35 framebuffer checkpoints, `PASS 1af5ac24` and the disks they leave are identical. Verilator covers these 35 frames, not the 350.
  - The RTL retires 243 M instructions to the emulator's 210 M. The difference is the kernel's timer interrupt, which fires by device time: cycles on the RTL, steps on the emulator. So it fires six times as often on the RTL, 122,064 interrupts against 20,550, at a few hundred kernel instructions each. Doom's own instructions are the same on both, since its clock is virtual.
  - It takes 1.407 G cycles and 21 minutes.
- **What the palette pins cover.** The palette changes during the demo: frames 105 and 175 print `a835bcb1`, frame 280 `04534b77` (the pickup and damage flashes), and the rest `2002492b`. So the lines pin Doom's palette at three different moments.
  - On the emulator and Verilator, each palette written is also read back from the device and compared, so a palette that does not arrive stops the run.
  - On QEMU the hash is Doom's own copy, since there is no device.
- **Keys.** `doomkeys.session` (emulator) plays the title for 50 frames with scripted keys ([doomkeys.input](../programs/rv32/os/doomkeys.input)):
  - Escape opens the menu, Q (not Doom's) changes nothing, Down then Enter opens Options, Escape closes it all.
  - Its 50 framebuffer checkpoints are pinned, so a wrong key map changes them.
  - It also pins `-fps`'s line and the reports of a limit that is not a multiple of 35 (frames 35 and 50).
- **Untraced.** The runner compares these sessions with `--compare outputs`, new in this PR: the console, the outcome, every checkpoint and the disks, as `results` does, but with no trace written and so no trap records or stores. It is only for runs too long to trace, since nothing can check that a shorter one would not have given a full trace comparison. A trace of the 35 frames alone is 9.5 GB.
- `check-rv32-doom-window` runs Doom through the window, headless, for 400 M instructions: the title and the demo, 396 frames through the palette.
  - The run must give exactly the headless emulator's console and checkpoints.
  - Doom must still be running at the end: a kill or an exit would leave the shell idling to the limit, which the console would show. On a failure it prints the end of both.
- `make run-rv32-doom` opens the window for play at 35 frames a second and records the session. It runs on a copy of the disk, so it replays headless. It was not played by hand for this record.

### How fast

| Backend | Start-up (to the first frame) | A frame | 350 frames, wall time |
| --- | --- | --- | --- |
| Emulator | 202 M instructions | 1.28 M instructions | 26 s (25 M instructions/s) |
| QEMU (`-icount`) | the same instructions | the same | 1.1 s |
| Verilator (`--stall 1`) | about 1.2 G cycles | about 7.4 M cycles | 35 frames in 21 min (1.1 M cycles/s); 350 would take about an hour |

Start-up is most of a short run: `W_Init` and `R_Init` read the WAD's directory and composite the wall textures, and `Z_Init` sets up the 6 MiB zone. A frame of E1M1 at 320×200 takes about 1.28 M instructions on the RV32IM core. The RTL averaged 5.8 cycles an instruction over the run, with a stall on every memory request as the slow tier runs it. That makes a frame about 7.4 M cycles: 6.8 frames a second at a 50 MHz clock, and 35, Doom's full rate, would need about 260 MHz.
