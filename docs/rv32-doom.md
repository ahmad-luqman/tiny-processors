# Doom on the RV32 OS (Track 3, B3)

[Issue #35](https://github.com/ahmad-luqman/tiny-processors/issues/35) asks for doomgeneric running as a C library program on the OS, with its 320×200 output drawn into the 320×240 framebuffer. The work is two PRs, as the issue allows:

1. **The platform:** what Doom needs from the machine and the kernel.
2. **Doom:** the port itself.

This record covers the platform, which is the first PR. The port's section is added by the second.

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
