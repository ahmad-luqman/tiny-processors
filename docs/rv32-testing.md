# Running the RV32 tests

`make test-rv32` is the check before every RV32 pull request. By
[issue #26](https://github.com/ahmad-luqman/tiny-processors/issues/26) it ran one
job at a time and took 56 minutes on a 16-core Mac. Most of that went to Icarus
runs that repeat a Verilator check. The checks now come in two tiers, and both
tiers are safe to run in parallel.

## The tiers

| Target | What it runs | When |
| --- | --- | --- |
| `make -j8 test-rv32` | Every check on the emulator, QEMU and Verilator, lint and synthesis, the sanitizers, and the Icarus runs that take seconds | Before every RV32 PR |
| `make -j8 test-rv32-slow` | The long Icarus twins of Verilator checks in `test-rv32` | Before merging |
| `make -j8 test-rv32-full` | Both | Before merging, or nightly |

`test-rv32-full` ran exactly the 123 checks the single `test-rv32` ran before
issue #26 (issue #33 added the float and mandel sessions since, and issue #35 the diskprog session on QEMU, the emulator and Verilator, and Doom's 350 frames on QEMU and the emulator plus its window check; Doom's 35 frames on Verilator are in `test-rv32-slow`), with one exception. `test-rv32-3d` used to run four test files in
one process. The aggregate now runs them as two targets:

- `test-rv32-3d-model` (in `test-rv32`) runs the oracle, the C reference and the
  emulator's device.
- `test-rv32-3d-icarus` (in `test-rv32-slow`) runs the Icarus corpus and the SoC
  contracts.

`make test-rv32-3d` still runs all four files.

## Readable parallel logs and timing

GNU Make 3.81, the macOS default, has no `--output-sync`, so `make -j` interleaves
the logs of concurrent recipes. `RV32_TIMING=1` makes every recipe line run
through [tools/rv32_recipe_shell.py](../tools/rv32_recipe_shell.py):

```sh
rm -f build/rv32-timing.jsonl
make -j8 RV32_TIMING=1 test-rv32
python3 tools/rv32_recipe_shell.py --report build/rv32-timing.jsonl
```

Each line prints `>> target: command` when it starts. Its output is printed in
one block when it finishes, followed by `<< target: N s ok` or `FAILED`. Each
line also appends a JSON record to `RV32_TIMING_LOG` (default
`build/rv32-timing.jsonl`). `--report` turns the log into a table of targets,
longest first. With GNU Make 4, `--output-sync=target` keeps the logs apart
without the wrapper.

The wrapper strips `RV32_TIMING` from the environment of the commands it runs.
A `make` that a test starts itself, such as `make print-rv32-os-layout` in
`test_rv32_os.py`, then prints plain output. Verilator's generated makefiles are
also left unwrapped.

## Which Icarus runs stay in the fast tier

Icarus is a four-state, event-driven simulator. Verilator is two-state and
compiled. An X that reaches a register, a race between blocking assignments,
or Verilog that only Icarus's `-g2012 -Wall` front end rejects all show up on
Icarus alone. So `test-rv32` keeps Icarus runs that cover the core, every
device class and the privilege modes, each in seconds:

| Fast-tier Icarus run | Guards |
| --- | --- |
| `run-rv32-rtl` | The self-check image, trace for trace, and the testbench as the Makefile builds it |
| `run-rv32-f-rtl` | The F datapath (`fp32.v`) under four-state simulation |
| `run-rv32-simd4-rtl` | The SIMD4 peripheral's handshake |
| `run-rv32-mmu-rtl`, `test-rv32-mmu-icarus` | Sv32 walks and the TLB |
| `run-rv32-irq-rtl`, `test-rv32-irq-icarus`, `test-rv32-pmp-icarus` | Interrupts and PMP |
| `run-rv32-virtio-rtl` | The virtio-blk device and its DMA |
| `test-rv32-dma-window-icarus` | The DMA window in front of G1 and G2 |
| `test-rv32-gfx` | The G1 device alone (`rv32_gpu.v`), and the SoC contracts |
| `test-rv32-a`, `test-rv32-arch-a-icarus`, `test-rv32-ua-icarus`, `run-rv32-atom-rtl` | The A extension (issue #34): `AMO_WRITE`, the reservation, and the testbench's read-then-write check |

`test-rv32-slow` holds the rest. Each target there has a Verilator twin in
`test-rv32` that runs the same image or the same tests:

| Slow-tier Icarus run | Verilator twin in `test-rv32` |
| --- | --- |
| `run-rv32-gfx-rtl` | `run-rv32-gfx-rtl-verilator` |
| `run-rv32-capstone-rtl` | `run-rv32-capstone-rtl-verilator` |
| `test-rv32-rtl` | `test-rv32-rtl-verilator` |
| `run-rv32-pong-rtl` | `run-rv32-pong-rtl-verilator` |
| `test-rv32-3d-icarus` | `test-rv32-3d-verilator` |
| `test-rv32-simd4` | `test-rv32-simd4-verilator` |
| `run-rv32-diag-rtl` | `run-rv32-diag-rtl-verilator` |
| `run-rv32m-rtl` | `run-rv32m-rtl-verilator` |
| `run-rv32-platform-rtl` | `run-rv32-platform-rtl-verilator` |
| `test-rv32-f` | `test-rv32-f-verilator` |

Issue #33 first put `run-rv32-mandel-rtl-verilator`, mandel's default picture,
in the slow tier: about 900 M cycles and 12 minutes while the FPU's adds and
multiplies waited ~540–560 cycles each. Since the FPU's [narrow
datapath](fp32.md#narrow-datapath-2026-10-03) the picture takes 43.7 M cycles
and under a minute, so it runs in `test-rv32` beside the float session
(`run-rv32-float-rtl-verilator`, 31.2 M cycles), and its 100 M cycle budget
fails a slower FPU.

With issue #35's platform PR (16 MiB RAM, the diskprog session, the palette and key tests) `make -j8 test-rv32` took 5 min 5 s and 5 min 19 s on two runs on the same Mac, against #26's 284 s; the added Verilator runs account for most of it, and every Icarus run starts about 3.7 s later, zero-filling 16 MiB of RAM.

The OS sessions never ran on Icarus in the aggregate; `run-rv32-os-rtl` is a
manual target.

`test-rv32-rtl` runs the Icarus sweep of
[tests/test_rv32_rtl.py](../tests/test_rv32_rtl.py) through
[tools/rv32_unittest_shards.py](../tools/rv32_unittest_shards.py). The 42 tests
are dealt out to `RV32_RTL_SHARDS` (8) processes. Each process compiles its own
testbench and emulator in `setUpClass`, as the single process did. The run
fails if the shards together ran fewer tests than discovery found. The sweep
went from 303 s to 111 s. One test, `test_pong_image_when_built`, takes 106 s
by itself, so more shards would not help.

## Keeping the aggregate parallel-safe

Make builds each file target once per invocation, so images and simulators
shared by many checks are safe. What breaks under `-j` is two recipes writing
the same file. The audit for issue #26 checked every recipe's expanded command
(`make -n` per target) and every unit-test file that more than one target runs:

- `tools/rv32_rtl.py` writes `<out>/<image name>.*`. Two targets may share an
  `--out` directory only if they run different images, or if one depends on the
  other. `run-rv32-os-qemu-reboot` reuses `run-rv32-os-qemu`'s disk,
  `run-rv32-os-qemu-enter` reuses `run-rv32-os-emu`'s and `run-rv32-os-boot2`
  reuses `run-rv32-os-pong-rtl-steps`'s, all through such a dependency.
- A test file that one target runs on Icarus and another on Verilator must write
  under a directory of the simulator's own. Before issue #26,
  `tests/test_rv32_gfx.py` wrote `build/gfx/native.dylib` and
  `build/gfx/commands.txt` from both runs, and `test-rv32-gfx-sanitize` read
  that fixture while the other run could be rewriting it. Each simulator now
  writes under `build/gfx/unit-<sim>/`.
- Every other paired suite (`rtl`, `m`, `f`, `irq`, `pmp`, `mmu`, `simd4`,
  `dma_window`, `3d_rtl`, `3d_soc`, `gfx_soc`) already used temporary
  directories or per-simulator directories.

The first cold `make -j8 test-rv32-full` runs found two more problems that
serial order had hidden:

- **An undeclared generated file.** `test-rv32-capstone-sanitize` compiled
  `build/rv32/digit_weights.c` without naming it as a prerequisite. Serially,
  another target had always generated it first. A recipe must name every
  generated file it reads.
- **A wall-clock limit sized for an idle machine.** `run_rtl` gives the
  simulator 120 s by default. The Pong test in `test_rv32_rtl.py` takes 106 s
  on Icarus alone, so with eight jobs sharing the machine it ran out of time.
  The sweep now uses `RV32_ICARUS_TIMEOUT`, like the Icarus recipes. Cycle
  limits bound every run anyway, so the wall-clock limit only catches a hang.

## Measurements

Measured on 2026-10-01 on a 16-core Apple Silicon Mac with GNU Make 3.81,
nothing else running. Each run started from an empty `build/` in a fresh
worktree, so the times include building the images and the simulators (about
50 s of recipe time). The timings come from `RV32_TIMING=1`.

| Run | Wall time | Result |
| --- | ---: | --- |
| Before: `make test-rv32` on `main` (02252b2), serial | 3,355 s (55.9 min) | All checks passed except `test-rv32-os`, which tripped on the timing wrapper (fixed in the wrapper; see below) |
| After: `make -j8 test-rv32-full` | 908 s (15.1 min) | All passed; 555 unit tests, as before |
| After: `make -j8 test-rv32` | 284 s (4.7 min) | All passed |

The baseline's `test-rv32-os` error was in the measurement, not in `main`. The
baseline passed the wrapper on the command line, so it reached the
`make print-rv32-os-layout` that the test runs and prefixed that output with a
banner. The wrapper now strips itself from the environment of the commands it
runs.

The full aggregate's wall time is now its longest single target,
`run-rv32-gfx-rtl` (the G1 check image on Icarus, 831 s). Concurrent jobs make
each target 10–50% slower than it runs alone. Speeding up the Icarus gfx run
itself is the next lever.

Every target, longest first. `test-rv32-3d` is no longer in the aggregate; its
two halves are:

| Target | Tier | Before, serial (s) | After, in `make -j8 test-rv32-full` (s) |
| --- | --- | ---: | ---: |
| `run-rv32-gfx-rtl` | slow | 747.3 | 830.7 |
| `run-rv32-capstone-rtl` | slow | 471.5 | 533.7 |
| `test-rv32-rtl` | slow | 302.8 | 159.3 |
| `synth-rv32-soc` | fast | 167.4 | 218.2 |
| `run-rv32-soc-menu-rtl-verilator` | fast | 97.2 | 142.9 |
| `run-rv32-pong-rtl` | slow | 96.9 | 140.7 |
| `synth-rv32-3d` | fast | 113.7 | 139.4 |
| `run-rv32-os-menu-rtl-verilator` | fast | 117.7 | 125.7 |
| `run-rv32-diag-rtl` | slow | 81.4 | 123.9 |
| `test-rv32-simd4` | slow | 82.0 | 112.3 |
| `run-rv32m-rtl` | slow | 75.2 | 108.1 |
| `test-rv32-3d` | — | 86.0 | — |
| `test-rv32-arch-verilator` | fast | 52.4 | 59.4 |
| `run-rv32-soc-rtl-verilator` | fast | 39.4 | 54.6 |
| `run-rv32-gfx-menu-rtl-verilator` | fast | 42.3 | 51.3 |
| `test-rv32-3d-icarus` | slow | — | 50.2 |
| `test-rv32-digit` | fast | 43.8 | 49.2 |
| `test-rv32-3d-model` | fast | — | 46.6 |
| `accuracy-rv32-digit` | fast | 40.6 | 43.9 |
| `run-rv32-platform-rtl` | slow | 33.9 | 40.3 |
| `test-rv32-f` | slow | 32.3 | 35.7 |
| `run-rv32-3d-menu-rtl-verilator` | fast | 27.4 | 33.3 |
| `run-rv32-3d-rtl-verilator` | fast | 26.6 | 31.9 |
| `test-rv32-arch` | fast | 25.3 | 28.9 |
| `run-rv32-digit-menu-rtl-verilator` | fast | 22.6 | 27.5 |
| `run-rv32m-rtl-verilator` | fast | 16.4 | 25.9 |
| `run-rv32-gfx-rtl-verilator` | fast | 21.5 | 25.3 |
| `run-rv32-capstone-rtl-verilator` | fast | 14.5 | 20.1 |
| `test-rv32-rtl-verilator` | fast | 14.2 | 20.0 |
| `run-rv32-soc-menu-emu` | fast | 15.0 | 18.0 |
| `run-rv32-os-jobs-rtl-verilator` | fast | 13.1 | 17.8 |
| `test-rv32-gfx` | fast | 15.9 | 17.7 |
| `test-rv32-3d-verilator` | fast | 16.2 | 17.4 |
| `synth-rv32` | fast | 15.4 | 17.0 |
| `run-rv32-os-jobs-rtl-steps` | fast | 12.1 | 16.4 |
| `test-rv32-os` | fast | 12.4 | 14.3 |
| `test-rv32-m` | fast | 13.7 | 14.1 |
| `test-rv32-dma-window-icarus` | fast | 11.7 | 13.6 |
| `synth-rv32-gfx` | fast | 12.2 | 13.5 |
| `run-rv32-os-enter-rtl-verilator` | fast | — | 13.4\* |
| `run-rv32-mmu-rtl` | fast | 11.4 | 13.2 |
| `run-rv32-virtio-rtl` | fast | 11.8 | 13.1 |
| `run-rv32-irq-rtl` | fast | 11.3 | 12.5 |
| `run-rv32-os-rtl-verilator` | fast | 9.4 | 12.3 |
| `test-rv32-mmu-icarus` | fast | 9.1 | 10.3 |
| `run-rv32-rtl` | fast | 6.7 | 10.2 |
| `test-rv32-simd4-verilator` | fast | 7.8 | 9.2 |
| `run-rv32-simd4-rtl` | fast | 7.7 | 8.7 |
| `run-rv32-digit-rtl-verilator` | fast | 7.4 | 8.5 |
| `run-rv32-gfx-menu-emu` | fast | 6.2 | 8.3 |
| `test-rv32-gfx-verilator` | fast | 6.9 | 7.9 |
| `run-rv32-os-menu-emu` | fast | 5.4 | 7.7 |
| `run-rv32-soc-emu` | fast | 6.1 | 7.4 |
| `test-rv32-irq-icarus` | fast | 6.4 | 7.3 |
| `run-rv32-3d-menu-emu` | fast | 5.9 | 7.1 |
| `test-rv32-pong` | fast | 5.7 | 7.0 |
| `test-rv32-capstone` | fast | 6.6 | 7.0 |
| `test-rv32-3d-sanitize` | fast | 6.7 | 6.6 |
| `run-rv32-os-pong-rtl-steps` | fast | 5.0 | 6.6 |
| `test-rv32-pmp-icarus` | fast | 5.4 | 5.8 |
| `run-rv32-f-rtl` | fast | 4.7 | 5.8 |
| `run-rv32-3d-emu` | fast | 4.1 | 4.8 |
| `test-rv32-win` | fast | 3.9 | 4.4 |
| `run-rv32-digit-menu-emu` | fast | 3.4 | 4.1 |
| `run-rv32-pong-rtl-verilator` | fast | 2.8 | 3.9 |
| `test-rv32-emu` | fast | 3.1 | 3.9 |
| `test-rv32-platform` | fast | 3.4 | 3.8 |
| `run-rv32-os-qemu-enter` | fast | — | 3.8\* |
| `run-rv32-gfx-emu` | fast | 3.1 | 3.7 |
| `run-rv32m-emu` | fast | 2.9 | 3.5 |
| `run-rv32-capstone-emu` | fast | 2.3 | 3.3 |
| `test-rv32-f-verilator` | fast | 1.9 | 3.3 |
| `run-rv32-diag-rtl-verilator` | fast | 2.2 | 3.0 |
| `test-rv32-gdb` | fast | 2.6 | 2.8 |
| `run-rv32-os-boot2` | fast | 2.0 | 2.4 |
| `test-rv32-m-verilator` | fast | 1.9 | 2.1 |
| `test-rv32-rt` | fast | 1.1 | 1.9 |
| `test-rv32-mmu` | fast | 1.8 | 1.7 |
| `test-rv32-capstone-sanitize` | fast | 1.5 | 1.6 |
| `test-rv32-gfx-sanitize` | fast | 1.5 | 1.0 |
| `run-rv32-digit-emu` | fast | 1.2 | 1.4 |
| `run-rv32-os-jobs-emu` | fast | 1.0 | 1.4 |
| `test-rv32-arch-model` | fast | 0.7 | 1.3 |
| `run-rv32-platform-rtl-verilator` | fast | 1.0 | 1.3 |
| `test-rv32-irq` | fast | 1.0 | 1.1 |
| `run-rv32-os-emu` | fast | 0.8 | 1.1 |
| `test-rv32-pmp` | fast | 0.9 | 1.1 |
| `bench-rv32-emu` | fast | 0.9 | 1.0 |
| `run-rv32-os-pong-emu` | fast | 0.6 | 0.8 |
| `run-rv32-os-qemu-reboot` | fast | 0.5 | 0.8 |
| `test-rv32-f-tools` | fast | 0.7 | 0.7 |
| `run-rv32-f-rtl-verilator` | fast | 0.5 | 0.7 |
| `run-rv32-pong-emu` | fast | 0.5 | 0.7 |
| `run-rv32-os-jobs-qemu` | fast | 0.3 | 0.6 |
| `run-rv32-os-qemu` | fast | 0.4 | 0.5 |
| `run-rv32-virtio-rtl-verilator` | fast | 0.5 | 0.5 |
| `run-rv32-virtio-rtl-steps` | fast | 0.5 | 0.5 |
| `run-rv32-f-emu` | fast | 0.4 | 0.5 |
| `test-rv32-dma-window` | fast | 0.4 | 0.5 |
| `run-rv32-mmu-rtl-steps` | fast | 0.5 | 0.5 |
| `run-rv32-irq-rtl-steps` | fast | 0.4 | 0.5 |
| `run-rv32-mmu-rtl-verilator` | fast | 0.4 | 0.5 |
| `run-rv32-emu` | fast | 0.5 | 0.1 |
| `run-rv32-virtio-qemu` | fast | 0.3 | 0.4 |
| `run-rv32-irq-rtl-verilator` | fast | 0.4 | 0.4 |
| `test-rv32-tools` | fast | 0.3 | 0.4 |
| `run-rv32-rtl-verilator` | fast | 0.2 | 0.4 |
| `run-rv32-diag-emu` | fast | 0.2 | 0.3 |
| `run-rv32-simd4-rtl-verilator` | fast | 0.2 | 0.3 |
| `run-rv32-platform-qemu` | fast | 0.2 | 0.3 |
| `run-rv32-mmu-qemu` | fast | 0.2 | 0.3 |
| `run-rv32-irq-qemu` | fast | 0.2 | 0.2 |
| `lint-rv32-soc` | fast | 0.2 | 0.2 |
| `run-rv32-platform-emu` | fast | 0.1 | 0.2 |
| `diff-rv32-qemu` | fast | 0.1 | 0.2 |
| `lint-rv32` | fast | 0.1 | 0.1 |
| `run-rv32-mmu-emu` | fast | 0.1 | 0.1 |
| `run-rv32-virtio-emu` | fast | 0.1 | 0.1 |
| `test-rv32-bench` | fast | 0.1 | 0.1 |
| `lint-rv32-3d` | fast | 0.1 | 0.1 |
| `run-rv32-irq-emu` | fast | 0.1 | 0.1 |
| `run-rv32-simd4-emu` | fast | 0.1 | 0.1 |
| `run-rv32-qemu` | fast | 0.1 | 0.1 |
| `check-rv32-virt-map` | fast | 0.1 | 0.1 |
| `run-rv32-f-soft-qemu` | fast | 0.1 | 0.1 |
| `lint-rv32-gfx` | fast | 0.1 | 0.1 |
| `check-rv32-dtb` | fast | 0.0 | 0.0 |

\* Added after issue #26 (issue #30) and timed alone, `run-rv32-os-qemu-enter` with its `run-rv32-os-emu` prerequisite.
