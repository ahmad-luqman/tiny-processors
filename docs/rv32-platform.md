# Track 1: a `virt`-compatible platform

Track 1 made our machine compatible with QEMU's `virt` board wherever `virt`
already has a device, so one image runs unmodified on QEMU, our emulator and
the RTL, and discovers which of our own devices exist instead of assuming
them. The [plan](planning/track1-qemu.md) fixed the contract; this record
explains the result and the evidence. It is option 1 of
[Track 1](planning/next-tracks.md#track-1-proper-qemu-support); option 2, a
custom QEMU board, stays optional.

## What changed

| Before | After | Why |
| --- | --- | --- |
| Timer `TICKS` at `0x2000_0000`, 32-bit | CLINT at `0x0200_0000`: `msip`, `mtimecmp`, 64-bit `mtime` | `virt`'s timer is a CLINT at that address; O1 needs `mtimecmp` |
| `time` CSR = `cycle` | `time` reads `mtime` | As on QEMU and real platforms: writing `mtime` moves `time` |
| Registers at `0x2000_1000`–`0x2000_9fff` | `0x1100_1000`–`0x1100_9fff` (same low offsets) | `0x2000_0000` is `virt`'s flash |
| Framebuffer at `0x3000_0000` | `0x1200_0000` | `0x3000_0000` is `virt`'s PCIe configuration space |
| Registers other than the PC unspecified | `a0` = hart id 0, `a1` = device tree | QEMU's mask ROM leaves exactly this |
| Nothing at `0x0000_1000` | Boot ROM with the machine's device tree (4 KiB) | Where `virt` keeps its mask ROM |
| — | PLIC `0x0c00_0000` and virtio-mmio `0x1000_1000`–`0x1000_8fff` reserved (unmapped) | Kept free for O1 and O3 |

The console (`0x1000_0000`), the done register (`0x0010_0000`) and RAM
(`0x8000_0000`, 4 MiB) already matched. [docs/rv32.md](rv32.md#behavior-fixed-in-track-1)
has the contract.

## Remap and describe

[Next tracks](planning/next-tracks.md#track-1-proper-qemu-support) asked for
one of two approaches; the track does both because they fix different
failures:

- **Remapping** means a wrong guess is caught. On QEMU an access to an
  address no device claims is an access fault, which a trap handler sees. An
  access to `0x2000_1000` before Track 1 was a read of `virt`'s flash, which
  returns data and fails silently.
- **A device tree** means nothing has to be guessed. QEMU starts every image
  with the hart id in `a0` and a flattened device tree in `a1`. Our backends
  now do the same with a tree of their own, so a program can ask which
  devices exist and touch only those, which is how the kernel of O2 will
  find its devices.

The hole the devices moved into was chosen from `virt`'s own tree:
`qemu-system-riscv32 -M virt,dumpdtb=virt.dtb -bios none -m 4M` (QEMU 8.2.2; RAM is 8 MiB since issue #33 and 16 MiB since issue #35)
describes nothing between its `fw_cfg` at `0x1010_0000` (24 bytes) and its
flash at `0x2000_0000`. [rv32_virt_map.py](../tools/rv32_virt_map.py) repeats
the check on every run of `make check-rv32-virt-map`. It parses both trees and
requires every shared device to sit inside the matching `virt` node (the done
register in `sifive,test0`, the console in `ns16550a`, the CLINT equal to
`riscv,clint0`, memory equal). It also requires every other window of ours to
be disjoint from every `reg` and `ranges` entry `virt` has, including the PCI
windows at `0x0300_0000`, `0x4000_0000` and above 4 GiB. A test moves one
window back to its pre-Track-1 address and checks that the checker reports
the overlap with `/flash@20000000`.

## One source for the tree

[rv32_dtb.py](../tools/rv32_dtb.py) holds the machine's tree as data and
writes it three ways: the blob (`make check-rv32-dtb` writes
`build/rv32/machine.dtb`), the C array the emulator compiles in
([rv32_dtb.h](../tools/rv32_dtb.h)) and a ROM module the RTL synthesizes
([rv32_bootrom.v](../rtl/rv32/rv32_bootrom.v), one `case` arm per word). The
two copies are committed so a build needs no generator, and
`rv32_dtb.py --check` (run by a unit test and `make check-rv32-dtb`) fails
when either is stale. The same module parses any FDT, which is how the map
checker reads QEMU's tree. `dtc`, where installed, decompiles our blob without
a warning:

```
/ {
    #address-cells = <1>; #size-cells = <1>;
    compatible = "tiny-processors,rv32-machine";
    model = "tiny-processors RV32 machine";
    chosen { stdout-path = "/soc/console@10000000"; };
    cpus { cpu@0 { device_type = "cpu"; compatible = "riscv"; riscv,isa = "rv32imf_zicsr_zicntr"; ... }; };
    memory@80000000 { device_type = "memory"; reg = <0x80000000 0x400000>; };
    soc {
        test@100000     { compatible = "tiny-processors,done", "sifive,test0";   reg = <0x100000 0x4>; };
        clint@2000000   { compatible = "tiny-processors,clint", "riscv,clint0";  reg = <0x2000000 0x10000>; };
        console@10000000 { compatible = "tiny-processors,console";               reg = <0x10000000 0x8>; };
        input@11001000  ...  display@11002000 { reg = <0x11002000 0x10 0x12000000 0x12c00>; }
        simd4@11004000 { reg = <0x11004000 0x20 0x11005000 0x400 0x11006000 0x400>; }
        gpu@11007000 ...  g3d@11008000 ...
        dma-window@1100a000 { compatible = "tiny-processors,dma-window";   reg = <0x1100a000 0x8>; };
    };
};
```

The blob was 1,340 bytes at Track 1; with the nodes added since (the PLIC,
virtio-blk, and issue #20's DMA window) it is 2,012. A compatible list names our device first and adds a
generic name only where the device implements everything a driver for that
name may touch. The console implemented only a 16550's transmit and
line-status registers, and a 16550 driver's first act is to program the
divisor and FIFO registers, which faulted, so it does not claim
`ns16550a`. Issue #36 added the rest of the subset Linux's 8250 driver drives,
but this tree is unchanged, so the PASS words that hash it stay; Linux compiles
in its own tree ([contract](rv32.md#linuxs-needs-issue-36)). There is no `timebase-frequency`, because the contract gives a
device tick no rate.

## The CLINT

`mtime` keeps the timer's rule, widened to 64 bits: on the RTL a read returns
the number of the cycle that accepts it; on the emulator, the number of
instructions executed before the reading one; a write to one half replaces
that half of the current count. The low word therefore behaves exactly as
`TICKS` did, and the diagnostic's wrap test (write `0xFFFF_FF00`, poll until
the count wraps) now wraps the low word into the high one. The `time` CSR
reads the same count, so it now differs from `cycle` by one on the RTL: `cycle`
counts the cycles before the reading one, `mtime` includes it.
`msip` and `mtimecmp` are plain registers (`mtimecmp` resets to all ones)
until O1 connects them to `mip`.

On the RTL [rv32_clint.v](../rtl/rv32/rv32_clint.v) replaces `rv32_timer.v`
and drives a new `time_now` port on the core. The core's register file resets
`x11` to its `BOOT_A1` parameter (`0x0000_1000`) and everything else to zero. The
emulator sets `x[11]` in `emu_init`.

## platcheck

[platcheck.c](../programs/rv32/platcheck.c) receives `a0` and `a1` as `main`'s
arguments ([start.S](../programs/rv32/start.S) never touches them) and learns
the platform from the tree through [fdt.c](../programs/rv32/fdt.c), a small
reader that checks the header, walks the structure block, decodes `reg` with
the parent's cell counts (QEMU uses two address cells, we use one; more than
two is refused) and reads the blob a byte at a time. Every query returns a
status ([fdt.h](../programs/rv32/fdt.h)): found, not found, a malformed tree,
a value too wide for 32 bits, or a node that matches but has no usable `reg`.
On QEMU:

```
platcheck: hart 0
platcheck: model riscv-virtio,qemu
platcheck: memory 80000000 00400000
platcheck: done 00100000
platcheck: console 10000000
platcheck: clint 02000000
platcheck: mtime advances
platcheck: time follows mtime
platcheck: mtimecmp and msip hold
platcheck: input absent
platcheck: display absent
platcheck: simd4 absent
platcheck: g1 absent
platcheck: g2 absent
PASS 91659113
```

On our backends the model line is `tiny-processors RV32 machine` and each of
our devices is listed at its address. platcheck then reads the display's
size, the input queue's count and the accelerators' busy bits. The PASS word
folds only what every platform shares (the hart id, memory, the three shared
addresses and the CLINT results), so it is the same on all of them.
[platcheck.qemu.expected](../programs/rv32/platcheck.qemu.expected) and
[platcheck.expected](../programs/rv32/platcheck.expected) pin both
transcripts. On our machine every device must be listed (the root's
compatible says which machine it is); on any other, an unlisted device is
reported and never touched. Only "not found" reads as absent: any other
status from the reader fails the run on every platform, so a tree that is
corrupt after the shared nodes cannot pass by making our devices look absent
(a QEMU run with such a tree, given through `-dtb`, is one of the tests).

`rv32_run_qemu.py --last-line` judges the last console line, as the
diagnostic's runner does. QEMU 8.2 has no bare `rv32i` CPU model, so the run
uses the generic `rv32` (`RV32_PLATFORM_QEMU_CPU`); the image is RV32I with
one `csrr` (of `timeh`).

## Evidence

Run on 2026-09-29 in a Linux container (x86-64): Ubuntu clang and lld 18.1.3
(`RV32_LLVM=/usr/bin RV32_LD=/usr/bin/ld.lld`), QEMU 8.2.2, Icarus 13.0 and
Verilator 5.040 built from their release tags, Yosys 0.33, dtc 1.7.

- **Map:** `check-rv32-virt-map`: 12 windows against `virt`'s 22 regions,
  shared devices equal, the rest disjoint.
- **platcheck:** `PASS 91659113` on QEMU `virt` (exit status 0, transcript
  equal to the pinned one), the emulator (118,151 instructions), Icarus
  (498,994 cycles) and Verilator with one stall per request (643,535 cycles),
  the RTL runs results-identical to the emulator (18 console lines).
- **Unit tests:** `test-rv32-platform` 23 tests. The generator round-trips,
  its copies are current, `dtc` agrees, and the Python parser refuses eleven
  malformed blobs and partial `reg` entries. The map checker passes ours
  against the installed QEMU and against a committed QEMU tree
  (QEMU 8.2.2 with 4 MiB then; since issue #33 the
  [fixture](../tests/fixtures/qemu-11.1.2-virt-16M.dtb) is QEMU 11.1.2 with 16 MiB (8 MiB from issue #33), so it also runs
  without QEMU), and rejects the old input window, a moved CLINT or done
  register, a window on a virtio slot, a resized memory and a missing shared
  node. `fdt.c`, built natively under AddressSanitizer and UBSan (the tests
  that exist to catch an out-of-bounds read are skipped, with the compiler's
  reason, if that build fails), gives the answer a Python model of the
  contract predicts for every compatible string in our tree and QEMU's at
  four indices, and refuses eighteen malformed blobs without reading past
  them. `platcheck` on QEMU passes with virt's own tree and fails, with the
  reader's status, when a node of ours has no usable `reg` or an address
  above 4 GiB. `test-rv32-emu` 36 and `test-rv32-rtl` 42 on Verilator cover
  the CLINT registers (distinct `mtimecmp` halves, `msip` from all ones and
  back to 0), the carry from `mtime`'s low word into the high word and `timeh`,
  `cycle` unmoved by `mtime` writes, the boot registers and ROM (its last word
  reads 0, the word past it faults) and the reserved PLIC and virtio
  addresses. `test-rv32-tools` pins every window's base and size across the
  device tree, the bus decoder's comparators, the emulator's region table and
  headers, `board.h` and the SoC, and the CLINT's register offsets across the
  RTL, the emulator, `board.h` and `rv32_asm.py`.
- **Earlier results:** the self-check (`PASS 807d9fad`, traces identical on
  Icarus and Verilator), Pong (`PASS 8fef54bc`, 200 checkpoints), the
  diagnostic, and the Verilator runs of the F images, SIMD4, G1 (and its
  menu), G2 (and its menu), the digit check and menu, S1 and its menu session,
  the capstone and Pong keep their PASS words and checkpoints; so do the RV32IM
  builds on the emulator and Verilator. `test-rv32-m`, `test-rv32-gdb`,
  `test-rv32-simd4`, `test-rv32-gfx`, `test-rv32-3d`, `test-rv32-digit` and
  `test-rv32-f` pass on Verilator (the first two after updating what the
  contract changed: `time` is one more than `cycle` on the RTL, and `a1` is
  `0x1000` at reset), and all 189 architectural tests pass on the emulator
  against QEMU. The diagnostic's word changed from
  `8bd87e9a` to `efd4ec82` because it folds the faulting address of its byte
  read of the timer, now `0x0200_bff8`. That value is recomputed in
  [rv32_devices.py](../tools/rv32_devices.py), not copied from a run.
- **Cost (Yosys 0.33, `synth` per module):** the CLINT is 668 cells with 129
  flip-flops (the two 64-bit registers and `msip`) against the M5 timer's 156
  cells (Yosys 0.33 generic cells, the same `synth` run);
  the boot ROM is 1,278 cells of combinational logic.

Failures that reproduce identically on the base commit in this container, as
[Track 0](rv32-groundwork.md#acceptance-record-2026-09-29) recorded: the
self-check's pinned clang 22 trace length, the runner test that uses BSD
`sed -i ""`, the Icarus diagnostic and Pong tests' 120-second timeout, the
diagnostic's Verilator target at the default 10-million-cycle limit (it passes
at 30 million: 19,027,745 cycles), the host tests that link `-shared`
libraries, and the QEMU runs of the self-check and `floatsoft`, which ask for
the `rv32i` model QEMU 8.2 lacks (both pass with `--cpu rv32`). Issue #20
later fixed the first two and the diagnostic's cycle limit.

## After review

The review of the pull request found no functional fault in the machine, but
several in how the reader and platcheck reported problems. Each was
reproduced before it was fixed:

- A property name without a NUL before the end of the blob, and a huge
  `#size-cells` with a matching `reg` of 16 bytes, both made `fdt.c` read
  outside the blob (AddressSanitizer: heap-buffer-overflow in the name
  comparison, SEGV in `be32`), and a reg index of 536,870,911 returned a
  property header as an address. Names are now checked to end inside the
  strings block, cell counts above two are `FDT_TOO_WIDE` before any
  arithmetic, and an index is compared with the number of entries rather than
  multiplied. 1.2 million random mutations of our tree and QEMU's then ran
  clean under both sanitizers.
- A matching node without a usable `reg` read as "not found", and platcheck
  printed every reader error as "absent", which QEMU's run accepts: given a
  tree with a broken node of ours, the old image printed `PASS b8a59113` (with 4 MiB of RAM).
  Such a node is now `FDT_NO_REG` and platcheck fails on any status but "not
  found".
- The generated ROM module's header gave the ROM's address as `0x1000_1000`.
- Window sizes were pinned only in the device tree; they are now pinned in
  every copy (above).

## Exercises

1. **Read the tree by hand.** `python3 tools/rv32_dtb.py --dts`, then
   `xxd build/rv32/machine.dtb | head`: find the header's `off_dt_struct`,
   the first `BEGIN_NODE` token and the string offset of `compatible`.
2. **Break the map.** Move the input window back to `0x2000_1000` in
   `rv32_dtb.py` and run `make check-rv32-virt-map`. Which `virt` node does it
   name, and what would a program reading `EVENT` there have received on QEMU?
3. **Touch an absent device.** Make platcheck read the display's `WIDTH`
   even when the tree does not list it and run it on QEMU. Which trap does
   QEMU raise, and what would the same program have done before Track 1?
4. **`time` and `cycle`.** Predict what `rdtime` minus `rdcycle` is in the
   same instruction on the RTL and on the emulator, then after a write of 0
   to `mtime`. Check with `test_rv32_m.py`'s program.
5. **A timer interrupt, on paper.** O1 adds `mie`, `mip` and `mstatus.MIE`.
   Write the three lines of `rv32_clint.v` that would raise `mtip` and the
   condition the emulator must test after every instruction. Which device-time
   rule decides whether both backends take the interrupt at the same
   instruction?

## Limitations

- No interrupts: `msip` and `mtimecmp` do nothing until O1, and the tree has
  no `interrupts-extended` for the CLINT and no PLIC.
- No storage: virtio-blk belongs to O3; its slots are reserved.
- Our devices do not exist on QEMU. The capstone and every device program
  still need our emulator or the RTL. A custom QEMU board (option 2) would
  change that.
- The boot ROM is a `case` table: fine for 1.3 KB, but a much larger tree
  would want a synchronous memory initialized from a file.
