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
- The testbench zero-fills RAM at start. Icarus's `platcheck` run took 36.6 s at 16 MiB and 38.2 s on main, so the change made no measurable difference.
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
3. checks the entry exactly as the boot check checks the RAM disk's (one function, `entry_ok`, against the file's size), and requires the entry's name to be the file's;
4. reads the image into the program's slots with the whole-sector path.

A file that is being written cannot be run. The RAM disk's table is unchanged, so the boot line still says `20 programs` and no transcript moved.

`diskprog` is the test. It carries 256 KiB of a generated sequence (the assembler writes it from a `.rept`) and is linked into slots 100–103, above 8 MiB. It sits at the end of a 6 MiB disk, behind a 4 MiB `pad` file. Its session:
- runs it, and it checks every word;
- refuses `pad` (not a program) and `renamed` (the same file under another name);
- runs `palcheck` (below);
- lists the files.

QEMU, the emulator and Verilator agree (`PASS e9591b4a`), and the 6 MiB disks they leave are identical. Verilator takes about 22 s.

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
| RTL | [rv32_palette.v](../rtl/rv32/rv32_palette.v): a 256×24 array, filled at power-on from two level tables (the RGB332 fields scaled with truncation), with a bus port and select. Synthesis shrinks it to 16 entries as it shrinks RAM: 338,753 SoC cells, 1,527 more |
| Device tree | The display node's third `reg` entry. `check-rv32-virt-map` stays clean: the window is in the hole between `fw_cfg` and flash |
| Kernel | `SYS_PALETTE` (22): `palette(colours, set)` reads all 256 entries into a word-aligned user buffer (`set` 0) or writes them from it (1). It is an error on a machine without a palette (QEMU), for a misaligned buffer, for another `set` value, or for memory that is not the caller's. PMP has no free entry, so the window is not mapped into programs |
| `platcheck` | Prints the window and checks four power-on colours. Its word is unchanged; the expected console gains a line |

**Tests:**
- The emulator and RTL tests hash every power-on word and check the top byte, the sub-word and fetch faults, trace for trace.
- The PPM shows a written colour.
- The old unmapped-palette faults moved to `0x1100_3400`, the unmapped rest of that page.
- `palcheck`, a disk program next to `diskprog`, reads the RGB332 palette through the kernel. It then sets a palette whose words carry a top byte, reads it back with that byte dropped, restores it, and checks three refusals. On QEMU it prints `no palette`.

## Keys

Codes 15 to 26 are added: `CTRL`, `SHIFT`, `TAB`, `Y`, `N` and `DIGIT1` to `DIGIT7`. They are in `board.h`, the emulator's name table, the testbench, `rv32_devices.py` and the window, where either Ctrl or either Shift is the one key. The digits are named rather than `1`..`7` because an input script already reads a bare number as a raw code. The fourteen existing codes do not move. A new RTL test reads every named key from a script on both backends, in both cases.

## Evidence (platform PR)

- `make test-rv32`: every target passes on macOS.
  - Under `-j8` load QEMU once truncated `F/fsub_b11-01`'s console. That is issue #41, and the suite passed 198/198 when rerun alone.
- `run-rv32-diskprog-{qemu,emu,rtl-verilator}`: `PASS e9591b4a`, with identical disks.
- `run-rv32-pong-emu`: 200 checkpoints and `PASS 8fef54bc`, unchanged.
- `run-rv32-platform-{qemu,emu,rtl,rtl-verilator}`: `PASS bee59113`.
- `lint-rv32` and `lint-rv32-soc` are clean. `synth-rv32-soc` gives 338,753 cells.
