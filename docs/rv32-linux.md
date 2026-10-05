# Linux without an MMU (Track 3, B4)

[Issue #36](https://github.com/ahmad-luqman/tiny-processors/issues/36) asks for a Linux kernel without an MMU, booting to a shell on our machine in the style of [mini-rv32ima](https://github.com/cnlohr/mini-rv32ima), first on the emulator and then on Verilator, with QEMU `virt` as the reference. The work is two PRs:

1. **The machine** ([PR #44](https://github.com/ahmad-luqman/tiny-processors/pull/44)): what Linux needs from the hardware, which the contract records in [Linux's needs](rv32.md#linuxs-needs-issue-36).
2. **Linux** (this record from [The image](#the-image) on): the build, the device tree, the sessions and the targets.

## What stood in the way

A first image (Buildroot's own nommu configuration) booted on QEMU, which showed what our machine lacked:

| Need | Before | What Linux does |
| --- | --- | --- |
| `mhartid` and the other ID CSRs, `misa` | Illegal | M-mode `head.S` reads `mhartid` before it sets `mtvec`: a double fault |
| `fence.i` | Illegal | `flush_icache_all` |
| A 16550 the 8250 driver can drive | THR/RBR and LSR only | Writes IER, FCR, LCR, MCR and the divisor; waits for THRE\|TEMT; polls through IIR |
| A readable done register | Every read faulted | `syscon-poweroff` reads before it writes |
| A device tree with a timebase and the A extension | The boot ROM's has neither | The CLINT timer needs `timebase-frequency`; the ISA comes from the tree |
| RAM for user space | 16 MiB, of which a 4.5 MB image left 4 MB in pieces | That image's busybox, a 740 KB bFLT binary, needs one contiguous block |

The first four are PR #44. The tree and the memory are this one's.

## The image

**Versions.**
- Buildroot 2025.02.18 (an LTS), upstream and unpatched. Its release tarball is pinned by the SHA-256 in its own `.sign` file.
- Linux 6.12.27, Buildroot's own pin for `qemu_riscv32_nommu_virt_defconfig`.
- busybox 1.37.0, uClibc-ng and gcc 13. Buildroot builds them all from tarballs whose hashes it checks (`BR2_DOWNLOAD_FORCE_CHECK_HASHES`).

The container is Debian bookworm, pinned by digest, with its packages from snapshot.debian.org at a fixed date.

**Configuration.** Everything is in [programs/rv32/linux/](../programs/rv32/linux/):
- `buildroot.defconfig`: Buildroot's 32-bit nommu configuration, with these changes:
  - the ISA narrowed to RV32IMA (Buildroot's default is G, which includes F and D);
  - the root file system built into the kernel as an initramfs, with no getty;
  - a reproducible build.
- `linux.fragment`: what the kernel needs on top of `tinyconfig`:
  - M-mode, nommu, RV32I, with no C, no FPU (F without D is no use to Linux) and one hart;
  - bFLT binaries and `#!` scripts;
  - the 8250 console;
  - `syscon-poweroff`;
  - devtmpfs and procfs;
  - the built-in tree;
  - "emulated" unaligned access, which skips the boot-time probe that times misaligned loads and so depends on the backend's speed.
  - `build.sh` merges it with tinyconfig's own fragments (`tiny-base.config` and `tiny.config`) on `allnoconfig`, with the cross compiler in the environment, because Kconfig probes the compiler. It then checks that every line stuck, and saves the result with `savedefconfig`. The lines are checked again on the configuration as built, after Buildroot's own adjustments.

    That is how the review of PR #45 found `RISCV_ISA_ZBB` turned on in the first pinned image. `savedefconfig` had run against the container's own gcc, so Zbb looked unavailable and its "not set" line was lost. Buildroot's cross gcc then restored the option's default.
- `busybox.fragment`: about thirty-five applets on top of `allnoconfig`, checked the same way. The shell is hush, because ash needs `fork`, which nommu Linux does not have. `LFS` matches Buildroot's 64-bit `off_t`.
- `rootfs-overlay/etc/inittab`: mounts `/proc`, prints `tiny-processors linux: up`, runs a shell on `ttyS0`, and prints `tiny-processors linux: down` on the way out.

**Size.** The first image was built from Buildroot's own configuration. It had KALLSYMS, VT, input, block devices, io_uring, every initramfs compressor and busybox's default 400 applets. It came to 4.5 MB and left that busybox no contiguous 740 KB to load into.

From `tinyconfig` the image is 2,650,076 bytes:
- kernel code 1.2 MB (1,209 KiB);
- data 0.45 MB (451 KiB) and read-only data 0.14 MB;
- 0.8 MB of init (782 KiB), freed after boot. Of that, 672 KiB is the uncompressed initramfs (688,640 bytes), whose busybox is 379,688 bytes.


**Licences.**
- Linux and busybox are GPL-2.0.
- uClibc-ng is LGPL-2.1.
- Buildroot is GPL-2.0-or-later.

The image is never committed. `tools/rv32_linux.py --build` makes it from the pinned sources into the git-ignored `third_party/linux/`, and `--check` holds it to the SHA-256 pinned in the script (`01dcf663a6794749…`). The build also leaves Linux's and busybox's full configurations beside the image.

**Build time.** On this machine's Docker (aarch64, 16 CPUs), a clean build takes 7 minutes once Buildroot's 290 MB of downloads are cached, and most of that is the cross gcc. The first download depends on the mirrors; one ran at a few KB/s.

**Volumes.**
- Each checkout builds in its own Docker volume, named from the checkout's path, so two worktrees with different inputs do not undo each other.
- The downloads sit in one shared volume, `tiny-processors-linux-dl` (`BR2_DL_DIR`). Buildroot checks each by hash.
- The results are copied out by a second, short container running as the host's user, who owns `third_party/linux/`.
- `--clean` drops the checkout's volume and the stamp.

**The stamp.** `third_party/linux/inputs` records the inputs of the image beside it. It is written only once that image matches the pin, and a build checks the digest before it trusts the stamp. So a corrupt or stale image is rebuilt, not reported as current.

**Reproducible.** A second build from an empty volume, with only the downloads seeded and their hashes rechecked, gives the same SHA-256 on the same aarch64 Docker host. No other host has been tried. Its kernel configuration, busybox configuration, initramfs and tree are byte-identical too. That build also ran a `build.sh` that differed in one line from the first's.

`BR2_REPRODUCIBLE` and a fixed `SOURCE_DATE_EPOCH` are what make it so. The kernel then says `#1 Wed Oct  1 00:00:00 UTC 2025` and `buildroot@buildroot`, and busybox's banner carries no date.

## The device tree

Linux compiles in its own tree (`CONFIG_BUILTIN_DTB`) and ignores `a1`. [tools/rv32_dtb.py](../tools/rv32_dtb.py) builds it as `LINUX` from the same addresses and helpers as the boot ROM's `MACHINE`; `tools/rv32_linux.py` writes its blob, and the container turns it into the kernel's DTS with `dtc`.

It names only what Linux drives and every backend has at the same address:
- RAM;
- the CLINT (`riscv,clint0`);
- the console as an `ns16550a`, with no `interrupts`, so the driver polls it;
- the done register as a `syscon`, which `syscon-poweroff` writes `0x5555`.

Linux reads the ISA from `riscv,isa-base` and `riscv,isa-extensions` (`i m a zicsr zicntr zifencei`), since its fallback to `riscv,isa` is configured out.

`timebase-frequency` is 10 MHz, QEMU's CLINT rate. On our backends a tick is one instruction (the emulator, and the RTL with step ticks), so the value sets only how fast guest time runs: about 10 M instructions per guest second.

Two consequences:
- **The boot ROM's tree stays byte-identical**, so platcheck, irqcheck and every other PASS word that hashes it are unchanged.
- **QEMU runs the same tree as ours.** QEMU's `virt` has a real 16550, a CLINT and a `sifive_test` at the same addresses. The tree leaves out the PLIC and virtio, which `virt` has but Linux does not need here.

## Typing into Linux

The repo's convention is that console input is a file whose bytes all wait from reset. That works for our own shell, which reads a line when it wants one. It does not work for Linux:
- **Bytes are lost.** When the port starts up, the 8250 driver reads RBR twice to clear it, and once more when it shuts down. With every byte waiting from reset, those reads ate the session's first two bytes.
- **The echo is out of place.** The tty echoes whatever has arrived as soon as the port is open, so every command appeared before the boot messages.
- **QEMU differs.** Its 16550 has a 16-byte FIFO that the driver's FCR write clears, so it would lose a different number of bytes.

So the session is gated. Line *k* of the input becomes visible only once the guest has printed the prompt (`/ # `) *k* times.
- On the emulator this is `--console-prompt`, and on the testbench `+console-prompt-hex=`. Both watch the bytes the console sends and decide when bytes become visible from the guest's output alone, so the two stay trace-identical with step ticks.
- On QEMU, `tools/rv32_run_qemu.py --prompt` writes each line into QEMU's stdin when it sees the prompt.

The shell then sees each command when it waits for one, on every backend, and the transcript reads as typed.

## The session

[linux.session](../programs/rv32/linux/linux.session) has the shell run:
- `uname -a` and `cat /proc/version`;
- MemTotal and `/proc/cpuinfo`;
- `ls /`;
- a `for` loop and arithmetic;
- the busybox banner;
- `poweroff`.

`poweroff` goes through busybox init's shutdown, then `syscon-poweroff`, then the done register's pass word, which ends the run as a pass.

```
Run /init as init process
init started: BusyBox v1.37.0 ()
starting pid 16, tty '': '/bin/mount -t proc proc /proc'
starting pid 17, tty '': '/bin/echo "tiny-processors linux: up"'
tiny-processors linux: up
starting pid 18, tty '/dev/ttyS0': '-/bin/sh'


BusyBox v1.37.0 () hush - the humble shell
Enter 'help' for a list of built-in commands.

/ # uname -a
Linux (none) 6.12.27 #1 Wed Oct  1 00:00:00 UTC 2025 riscv32
/ # head -n 1 /proc/meminfo
MemTotal:          14228 kB
/ # cat /proc/cpuinfo
processor	: 0
hart		: 0
isa		: rv32ima_zicntr_zicsr_zifencei
mmu		: none
...
/ # for i in 1 2 3; do echo line $i; done
line 1
line 2
line 3
/ # echo $((6 * 7))
42
/ # poweroff
...
reboot: Power down
```

The [whole transcript](../programs/rv32/linux/linux.session.expected) keeps the console's `\r\n`, which is what Linux sends. So `rv32_rtl.py` now reads an expected console file as bytes rather than with Python's newline translation.

**QEMU's transcript** is pinned on its own (`linux.session.qemu.expected`) and differs from ours in one line: `/proc/cpuinfo`'s `marchid` is QEMU's `0x2a` where ours reads 0.

Everything else is the same, every kernel line included. The tree, the console, the timebase and the image are the same, and with `-icount 3` the transcript is reproducible: two runs gave the same one. The gated feeder writes each line when the host sees the prompt, so when a line arrives in guest time is up to the host. The shell is waiting for it either way.

## Numbers

The pinned session, start to power-down:

| Backend | Instructions | Cycles | Wall time |
| --- | ---: | ---: | ---: |
| Emulator | 60,889,775 | — | 2.3 s |
| Verilator, step ticks, one stall per transfer | 60,889,775 | 345,437,877 | 5 min 7 s |
| QEMU `-icount 3` | — | — | 0.6 s |

Verilator's figures:
- CPI is 5.67 with the stalls, of which there are 79.5 M (one per transfer).
- 591 timer interrupts and 1,131 traps. User programs run in U-mode under the PMP entry Linux opens, so the traps include every system call.
- `md_waits` is 3.7 M, the cycles the multiplier and divider took.

**Memory.** The kernel reports 13,260 KiB of the 16 MiB available at boot. Its image holds:
- 1,209 KiB of code;
- 451 KiB of data;
- 140 KiB read-only;
- 782 KiB of init, of which 780 KiB is freed once `/init` runs;
- 163 KiB of bss.

The 2,936 KiB it reports as reserved include the 2,588 KiB image. MemTotal, read in the session, is 14,228 KiB.

## Targets

| Target | What it does |
| --- | --- |
| `make rv32-linux-image` | Builds the image in Docker if it is missing or stale (7 minutes, plus the first download) |
| `make check-rv32-linux-image` | Fails unless the pinned image is in place |
| `make run-rv32-linux-qemu` | The session on QEMU `virt` (`-cpu rv32,mmu=false`, `-icount 3`), against its transcript |
| `make run-rv32-linux-emu` | The session on the emulator, against the transcript |
| `make run-rv32-linux-rtl-verilator` | The session on Verilator with step ticks, the same console as the emulator |
| `make test-rv32-linux` | QEMU and the emulator |

`test-rv32-slow` runs `test-rv32-linux` and the Verilator boot. `test-rv32` does not, so the pre-PR gate never needs Docker.
