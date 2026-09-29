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
`qemu-system-riscv32 -M virt,dumpdtb=virt.dtb -bios none -m 4M` (QEMU 8.2.2)
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
    };
};
```

The blob is 1,340 bytes. A compatible list names our device first and adds a
generic name only where the device implements everything a driver for that
name may touch. The console implements only a 16550's transmit and
line-status registers, and a 16550 driver's first act is to program the
divisor and FIFO registers, which fault here, so it does not claim
`ns16550a`. There is no `timebase-frequency`, because the contract gives a
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
the platform from the tree through [fdt.c](../programs/rv32/fdt.c), a
200-line reader that checks the header, walks the structure block, decodes
`reg` with the parent's cell counts (QEMU uses two address cells, we use
one) and reads the blob a byte at a time. On QEMU:

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
PASS b8a59113
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
reported and never touched.

`rv32_run_qemu.py --last-line` judges the last console line, as the
diagnostic's runner does. QEMU 8.2 has no bare `rv32i` CPU model, so the run
uses the generic `rv32` (`RV32_PLATFORM_QEMU_CPU`); the image is RV32I with
two `csrr`s.

## Evidence

Run on 2026-09-29 in a Linux container (x86-64): Ubuntu clang and lld 18.1.3
(`RV32_LLVM=/usr/bin RV32_LD=/usr/bin/ld.lld`), QEMU 8.2.2, Icarus 13.0 and
Verilator 5.040 built from their release tags, Yosys 0.33, dtc 1.7.

- **Map:** `check-rv32-virt-map`: 12 windows against `virt`'s 22 regions,
  shared devices equal, the rest disjoint.
- **platcheck:** `PASS b8a59113` on QEMU `virt` (exit status 0, transcript
  equal to the pinned one), the emulator (102,221 instructions), Icarus
  (431,651 cycles) and Verilator with one stall per request (556,639 cycles),
  the RTL runs results-identical to the emulator (18 console lines).
- **Unit tests:** `test-rv32-platform` 9 tests: the generator round-trips and
  its copies are current, `dtc` agrees, malformed blobs are refused; the map
  checker passes ours and rejects the old input, a moved CLINT and a window on
  a virtio slot; `fdt.c` built natively under AddressSanitizer and UBSan gives
  the Python parser's answers on our tree and QEMU's, and refuses six
  malformed blobs without reading past them. `test-rv32-emu` 35 (new: CLINT
  registers and `time`, boot registers and ROM, the reserved PLIC and virtio
  addresses fault); `test-rv32-rtl` on Verilator 41 (new: the same boot and
  CLINT program with identical traces with and without stalls, and six fault
  cases).
- **Earlier results:** the self-check (`PASS 807d9fad`, traces identical on
  Icarus and Verilator), Pong (`PASS 8fef54bc`, 200 checkpoints), the
  diagnostic, the SIMD4, G1, G2, digit, S1 and capstone runs listed below keep
  their PASS words and checkpoints. The diagnostic's word changed from
  `8bd87e9a` to `efd4ec82` because it folds the faulting address of its byte
  read of the timer, now `0x0200_bff8`. That value is recomputed in
  [rv32_devices.py](../tools/rv32_devices.py), not copied from a run.
- **Cost (Yosys 0.33, `synth` per module):** the CLINT is 668 cells with 129
  flip-flops (the two 64-bit registers and `msip`) against the timer's 156;
  the boot ROM is 1,278 cells of combinational logic.

Failures that reproduce identically on the base commit in this container, as
[Track 0](rv32-groundwork.md#acceptance-record-2026-09-29) recorded: the
self-check's pinned clang 22 trace length, the runner test that uses BSD
`sed -i ""`, the Icarus diagnostic and Pong tests' 120-second timeout, the
diagnostic's Verilator target at the default 10-million-cycle limit (it passes
at 30 million: 19,027,745 cycles), and the host tests that link `-shared`
libraries.

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
