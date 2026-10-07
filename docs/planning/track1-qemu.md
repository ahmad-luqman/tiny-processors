# Track 1 plan: a `virt`-compatible platform

Written 2026-09-29, after Track 0. This fixes the contract and acceptance for
[Track 1](next-tracks.md#track-1-proper-qemu-support), option 1: make our
machine compatible with QEMU's `virt` board wherever `virt` already has a
device, so one image runs unmodified on QEMU, our emulator and the RTL. The
optional custom QEMU board (option 2) is not part of this plan.

**Status (2026-10-07): complete.** See [the acceptance record](../rv32-platform.md).
This document preserves the original milestone contract. Track 2 subsequently
implemented the reserved PLIC and virtio-blk; RAM is now 16 MiB. The custom
board is saved as [Track 7](next-tracks.md#track-7-a-custom-qemu-board).

## The choice: remap, and describe the platform

[Next tracks](next-tracks.md#track-1-proper-qemu-support) asked the track to
choose between remapping our devices into a range `virt` leaves unused and
discovering the platform from a device tree. We do both, because they solve
different problems:

- **Remap** removes the overlap. Our devices at `0x2000_0000` and
  `0x3000_0000` sit on `virt`'s flash and PCIe windows, so a stray access on
  QEMU touches an unrelated device. After the remap every one of our windows
  lies in addresses `virt` leaves unmapped, so the same access on QEMU is an
  access fault, which a trap handler can see, rather than a silent read of
  flash.
- **A device tree** makes discovery possible without touching anything. QEMU
  starts the image with the hart id in `a0` and a flattened device tree (FDT)
  in `a1`. Our backends adopt the same boot convention and pass their own
  device tree, so a program can find out which devices exist and never
  access one the platform does not describe. That is how a kernel will find
  its devices from O2 on.

Remapping alone would leave programs guessing; a device tree alone would leave
a program that guesses wrong reading flash on QEMU.

## Contract changes

1. **Timer becomes a CLINT at `virt`'s address** (`0x0200_0000`, 64 KiB):
   `msip` at +0, `mtimecmp` at +`0x4000` (two words), `mtime` at +`0xbff8`
   (two words), word access only. `mtime` counts device ticks, as `TICKS`
   did, and is writable half by half. `msip` and `mtimecmp` are plain
   registers until O1 wires them to `mip`; `mtimecmp` resets to all ones.
   The old timer window at `0x2000_0000` is removed.
2. **`time` shadows `mtime`.** The Zicntr `time`/`timeh` CSRs read the CLINT's
   `mtime`, as on QEMU and on real platforms, so a write to `mtime` moves
   `time`. `cycle` stays the core's own count.
3. **Our devices move** into the hole between `virt`'s `fw_cfg`
   (`0x1010_0000`) and its flash (`0x2000_0000`): register windows from
   `0x2000_xxxx` to `0x1100_xxxx` (same low offsets), the framebuffer from
   `0x3000_0000` to `0x1200_0000`.
4. **Boot convention:** at reset `a0` holds the hart id (0) and `a1` the
   address of a device tree blob; the PC is still `0x8000_0000`. On our
   backends the blob lives in a 4 KiB **boot ROM** at `0x0000_1000` (where
   `virt` has its mask ROM), readable at every width, never writable or
   fetchable. One generator, [rv32_dtb.py](../../tools/rv32_dtb.py), writes
   the blob and the two committed copies built from it (a C array for the
   emulator, a ROM module for the RTL); a test fails when they are stale.
5. **Reserved for later tracks:** `virt`'s PLIC (`0x0c00_0000`) and
   virtio-mmio slots (`0x1000_1000`–`0x1000_8fff`) stay unmapped (access
   faults) on our backends. A PLIC is only useful once the CPU takes
   interrupts, so it belongs to O1; virtio-blk is storage, so it belongs to
   O3. Their addresses are fixed now so nothing else lands there.

The console (`0x1000_0000`), done register (`0x0010_0000`) and RAM
(`0x8000_0000`, 4 MiB) already match `virt`.

## Steps

1. Generator and checker: `tools/rv32_dtb.py` builds our FDT and parses any
   FDT (the standard library only); `tools/rv32_virt_map.py` dumps QEMU's
   own tree with `-M virt,dumpdtb=` and checks that every shared device
   matches its `virt` node and every one of our windows is disjoint from
   every `reg` and `ranges` entry `virt` describes.
2. Emulator and RTL: the remap, the CLINT (`rtl/rv32/rv32_clint.v` replaces
   `rv32_timer.v`), `time` from `mtime`, the boot ROM, and `a0`/`a1` at reset.
3. Firmware: `board.h` addresses, the diagnostic's timer checks moved to
   `mtime`, and a small FDT reader (`programs/rv32/fdt.c`).
4. `platcheck`, a program that reads the tree it is given, checks that the
   console, done register, CLINT and memory are where the contract says,
   exercises the CLINT (`mtime` advances, `time` follows a write to `mtime`,
   `mtimecmp` and `msip` hold their values), and exercises each of our
   devices only when the tree lists it. It prints what it found and ends with
   a `PASS` word computed only from what every backend shares.
5. Records: [docs/rv32.md](../rv32.md) memory map and boot convention, a
   track record in [docs/rv32-platform.md](../rv32-platform.md), and the
   next-tracks and PLAN status.

## Acceptance

- `make check-rv32-virt-map` passes against the installed QEMU.
- `make run-rv32-platform-qemu`, `run-rv32-platform-emu`,
  `run-rv32-platform-rtl` and `run-rv32-platform-rtl-verilator` pass:
  `platcheck` gives the same `PASS` word on QEMU `virt`, the emulator, Icarus
  and Verilator; QEMU's transcript reports our devices absent and ours report
  them present; the RTL runs are compared with the emulator at the results
  level (they read `mtime`).
- `make test-rv32-platform` passes: the generated blob, C array and ROM module
  are up to date, the map checker and the firmware's FDT reader behave as
  specified on good and malformed trees.
- Every earlier program keeps its `PASS` word and its checkpoints, except the
  diagnostic's word, which folds the address of its faulting timer read and
  so changes from `8bd87e9a` to `efd4ec82` (derived, not copied, in
  `tools/rv32_devices.py`); every trace comparison that held before still
  holds, on the emulator and on Verilator (and Icarus where its runtime
  allows).
