# Tiny Processors: our computer and advanced SoC

Updated 2026-09-22 after the [planning interview](docs/planning/full-stack-plan.md) and the completed M1 to M7, F1, F2, A1, A2, G1 and N1 sessions. The machine contract, the firmware toolchain, the emulator with its native window, a full RV32I multicycle RTL CPU, the machine around it (bus decoder, timer, input queue, display and framebuffer, matched on both backends), Pong, the boot menu/runtime/Tetris capstone, complete RV32F CPU/emulator integration, and a SIMD4 engine that multiplies and accumulates exist; A2 attaches SIMD4 to the RV32 bus with a matched emulator device and guest driver. G1 adds the integer 2D rasterizer, shared RAM arbitration and the third menu entry.
N1 adds a quantized digit classifier that runs on the CPU and the SIMD4 engine, with a fourth menu entry where a digit drawn with the keyboard is read back.

Build our own Nand2Tetris-inspired computer, preserving the completed labs. Design an RV32I CPU and matching emulator, reuse an existing C compiler, and run our own boot/menu/game runtime in a native Mac window. Pong comes first; Tetris defines the first complete computer. After Tetris, build an FP32 unit and integrate the RISC-V F extension into our CPU and emulator. GPU/NPU work also follows the playable machine: 2D acceleration, programmable 3D, and handwritten-digit recognition, integrated into an advanced SoC.

Read the [detailed roadmap](docs/planning/roadmap.md) for architecture proposals, interfaces, milestone acceptance checks, exercises, effort ranges, and deferred decisions. [CONTEXT.md](CONTEXT.md) defines project vocabulary. The [initial research archive](docs/planning/initial-research.md) preserves earlier reference material and completed-design details; its future ordering is superseded here.

## Working agreement

- Complete bounded, verified milestones, then walk through the implementation and offer exercises. Explain RTL as gates, muxes, registers, memories, and control; save useful explanations in `docs/`.
- Commit small verified increments and preserve every existing lab and command. Do not silently change the default counter targets into aggregate tests.
- There is no fixed schedule or deadline. Re-estimate from actual progress; milestone session ranges are rough guides.
- The required destination is an emulated computer on the existing Mac. FPGA deployment is optional. Our own language/compiler/software VM and expanded OS are later tracks.
- Use RTL simulation to verify hardware and the fast emulator for interactive play. Share firmware and machine contracts; compare behavior at instruction/device checkpoints without claiming equal cycle timing.

## Completed and preserved

| Artifact | Verified baseline | Read/run |
| --- | --- | --- |
| Eight-bit counter | Synchronous reset, enable, wraparound; 264 checks per simulator; lint and synthesis pass | [Lab](labs/01-counter/README.md), [gates](docs/verilog-to-gates.md); `make test`, `make test-verilator`, `make lint`, `make synth`, `make waves` |
| Eight-bit combinational ALU | Eight operations and flags; 524,307 checks per simulator; lint and synthesis with no storage pass | [Lab](labs/02-alu/README.md), [gates](docs/alu-to-gates.md); `make test-alu`, `make test-alu-verilator`, `make lint-alu`, `make synth-alu`, `make waves-alu` |
| SAP8 CPU and assembler | 493 core checks across 275 reset scenarios, assembled addition/loop programs, 12 assembler tests; lint and synthesis pass | [Contract](docs/sap8.md), [datapath](docs/sap8-to-gates.md); `make test-sap8`, `make test-sap8-verilator`, `make lint-sap8`, `make synth-sap8`, `make waves-sap8` |
| SIMD4 vector-add engine | 312 cases and 329 completed launches per simulator, six Python tests, 1/2/4-lane lint, four-lane synthesis; stalls and bandwidth measured | [Contract](docs/simd4.md), [gates/performance](docs/simd4-to-gates.md); `make test-simd4`, `make test-simd4-verilator`, `make lint-simd4`, `make synth-simd4`, `make bench-simd4`, `make waves-simd4` |
| SIMD4 multiply/accumulate and matrix kernel (A1) | 16×16→32 products into per-lane 32-bit wrapping accumulators; 395 cases and 423 launches per simulator, 21 Python tests, hand-computed extreme products; 4×4 matrix 754/450/298 cycles on 1/2/4 lanes with 144 transfers each; 13,038 cells, 629 flip-flops, no latches | [A1 record](docs/simd4.md#a1-acceptance-record-2026-09-22), [multiplier, dot product, overflow and matrix walkthrough](docs/simd4-to-gates.md); same commands |
| CPU-commanded SIMD4 (A2) | Private program/data windows and driver; 14 integration tests per simulator, 248 oracle-checked kernels/fault images; vector/matrix 198/298 device ticks, 96/144 transfers; ownership, independent stalls, faults, partial-transfer reset and relaunch verified | [A2 record](docs/rv32-simd4.md); `make test-rv32-simd4`, `make test-rv32-simd4-verilator` |
| Integer 2D rasterizer (G1) | Fill, RAM/framebuffer blit, line and triangle commands; 211 byte-port cases on both simulators; full guest pixel diagnostic; eight-frame menu replay and native-window session; 52,164 generic cells / 1,139 flip-flops | [G1 contract, gates and measurements](docs/rv32-gfx.md); `make test-rv32-gfx`, `make bench-rv32-gfx` |
| Quantized digit inference (N1) | 196-32-10 int8 classifier, 96.16% on the vendored 10,000-image MNIST test set against 96.10% in float, so quantization is not the limit; dense kernels of 304 instructions and 400 transfers for the 32 depth-49 launches and 202/264 for the 3 depth-32 launches, 13,592 transfers per inference; 285,742 RTL cycles accelerated against 546,830 on the CPU; 33 Python tests, guest C checked at -O0/-O2 against a standard-library oracle, `PASS N1` on three backends and a 199-frame drawing session; no RTL change | [N1 record](docs/rv32-digit.md); `make test-rv32-digit`, `make accuracy-rv32-digit`, `make run-rv32-digit-emu`, `make bench-rv32-digit` |
| RV32 machine contract and M1 firmware | 28-check freestanding C self-check runs on QEMU virt with the bare `rv32i` model: `PASS 807d9fad`, exit status 0; 17 tool tests and 6 host runtime tests; ELF image checks pass | [Contract](docs/rv32.md), [C to instructions](docs/c-to-instructions.md); `make test-rv32`, `make check-rv32-image`, `make run-rv32-qemu`, `make disasm-rv32` |
| RV32 headless emulator (M2) | C emulator runs the same image to `PASS 807d9fad` in 32,610 instructions and matches QEMU's PC sequence instruction for instruction; 30 hand-computed edge tests cover arithmetic, branches, jumps, loads/stores, traps, CSRs, and devices; about 400 M instructions/s untraced | [Emulator record and trace walkthrough](docs/rv32-emulator.md); `make test-rv32-emu`, `make run-rv32-emu`, `make trace-rv32-emu`, `make diff-rv32-qemu` |
| RV32 multicycle RTL CPU slice (M3) | Eleven-instruction core with one ready/valid memory port; a 78-instruction loop produces the emulator's trace line for line on Icarus and Verilator with 0 to 3 fixed and seeded random stall cycles; every other encoding halts as a terminal fault or `unsupported`; 20 tests (differential, decode sweep, harness), lint, and synthesis (5,777 cells, 1,331 flip-flops, no latches) pass | [RTL contract](docs/rv32-rtl.md), [gates and cycles](docs/rv32-to-gates.md); `make test-rv32-rtl`, `make test-rv32-rtl-verilator`, `make lint-rv32`, `make synth-rv32`, `make waves-rv32`, `make bench-rv32-rtl` |
| RV32 full RV32I RTL CPU (M4) | Every RV32I instruction plus the four trap CSRs and `mret`; traps vector and a double fault halts as in the emulator; byte strobes on reads and writes; the C self-check runs on the core to `PASS 807d9fad` with the emulator's 32,610-line trace on Icarus and Verilator, unstalled and stalled, in 138,495 cycles; lint and synthesis (8,175 cells, 1,457 flip-flops, no latches) pass | [RTL record and coverage table](docs/rv32-rtl.md), [gates, waves, and cycles](docs/rv32-to-gates.md); `make test-rv32-rtl`, `make run-rv32-rtl`, `make run-rv32-rtl-verilator`, `make waves-rv32` |
| RV32 machine with devices (M5) | Bus decoder and RTL peripherals (timer, 16-event input queue with held keys, display controller, 320×240 framebuffer, console, done) matched by emulator models with the same fault edges; the device diagnostic passes on the emulator, Icarus (1,666,436 cycles), and Verilator with identical console lines and frame checkpoints, its checksum and frame hash derived independently; 36 RTL tests per simulator, 31 emulator tests, 18 tool tests; lint and synthesis of the machine (23,664 cells with 64-word memories, no latches) pass | [SoC record](docs/rv32-soc.md), [contract](docs/rv32.md#behavior-fixed-in-m5); `make run-rv32-diag-emu`, `make run-rv32-diag-rtl`, `make lint-rv32-soc`, `make synth-rv32-soc` |
| RV32 native window and Pong (M6) | The emulator as a core library with a headless main and an SDL3 window that shows presents and queues host keys as frame events, with a recording that replays; drawing routines and Pong in device-free C tested natively at -O0 and -O2; a 200-frame scripted session gives the same 200 checkpoints and `PASS 8fef54bc` on the native build, the emulator, Icarus (1,998,070 cycles), and Verilator, trace-identical for 478,797 instructions; 7 Pong tests, 5 window tests, 33 emulator tests, 38 RTL tests per simulator, 19 tool tests; no RTL change | [Window record](docs/rv32-window.md), [contract](docs/rv32.md#behavior-fixed-in-m6); `make run-rv32-pong`, `make run-rv32-pong-emu`, `make run-rv32-pong-rtl`, `make test-rv32-pong` |
| First complete computer (M7) | One RV32I image boots the menu and runs Pong/Tetris; native rules and rendering tests at -O0/-O2; 68 checkpoints, `PASS ea60197e`, 2,220,509 trace-identical instructions at M7 (current G1 startup/menu counts are in its acceptance record); both games played by the user | [Runtime and Tetris](docs/rv32-runtime.md); `make run-rv32-capstone`, `make test-rv32-capstone`, `make run-rv32-capstone-rtl` |
| Standalone FP32 hardware (F1) | All five rounding modes; 70,407 vectors on both simulators; 259,407 additional stress vectors; reset/backpressure checks; 25,685 generic cells and 1,530 flip-flops without latches | [FP32 contract and walkthrough](docs/fp32.md); `make test-fp32`, `make test-fp32-verilator`, `make waves-fp32` |
| RV32F CPU and emulator (F2) | Complete F/Zicsr state and retirement; eleven architectural tests on both simulators, 1,080 seeded CPU vectors and 49 literal anchors in static/dynamic modes; ILP32 C float programs, RV32I software benchmark; 42,006-cell CPU, latch-free | [F2 contract and walkthrough](docs/rv32-f.md); `make test-rv32-f`, `make bench-rv32-f`, `make waves-rv32-f` |

These rows record each milestone’s acceptance baseline; the F1 session also reran the preserved machine and lab regressions. SAP8 stays frozen as the small teaching CPU. SIMD4 is a parallel compute prototype, not an integrated GPU or NPU. Its multiply/matrix extension (A1) and CPU-commanded bus integration (A2) are complete.

## Selected sequence

| Phase | Milestones | Finish line |
| --- | --- | --- |
| Firmware foundation | M1 machine contract and complete C toolchain; M2 headless RV32I emulator | A linked C image executes correctly with inspectable instructions and state |
| Our hardware CPU | M3 multicycle RTL subset; M4 broader RV32I coverage and freestanding C | RTL and independent reference agree at retirement; stalls, edge cases, faults, lint, and synthesis checked |
| A usable computer | M5 matched memory/devices; M6 native window, runtime drawing/input, and Pong | Same firmware/device contract on emulator and RTL; Pong is playable |
| First capstone | M7 boot menu, runtime services, and Tetris | Build, boot, select game, play, lose/restart; deterministic tests and bounded RTL replay pass |
| CPU floating point | F1 standalone FP32 unit; F2 CPU/emulator F-extension integration | Arithmetic and rounding match an independent reference; compiled C uses verified floating-point instructions |
| Accelerator foundation | A1 multiplication/matrix kernel; A2 CPU commands, shared memory, and drivers | CPU launches work and verifies results, including stalled/error/reset cases |
| Graphics and inference | G1 2D acceleration; N1 digit recognition; G2 programmable 3D | Guest demos produce reference-checked pixels and inference results |
| Advanced SoC | S1 unified CPU/GPU/NPU machine | One firmware/menu exercises graphics and inference with verified integration |

The recommended post-Tetris order is F1 → F2 → A1 → A2 → G1 → N1 → G2 → S1. Floating-point integration must pass before programmable 3D; N1 and G2 can be reordered once their prerequisites pass. CPU FP32 support does not select the GPU or NPU numeric format. A small programmable 3D demonstration is the selected graphics goal, not commercial graphics API compatibility. Neural inference uses a small pretrained model; training hardware is outside the initial goal.

## Next milestone: G2

N1 adds handwritten-digit inference: a vendored MNIST test set with recorded
provenance, an integer 196-32-10 classifier trained once off-line, one shared
preprocessing contract, dense SIMD4 kernels with the CPU doing bias, rounding and
saturation, and a menu screen where a digit drawn with the keyboard is classified
by the guest. It answered the deferred saturation and rounding question without
changing the RTL. The [N1 record](docs/rv32-digit.md) contains the numeric
contract, the kernel layout, inspected waves and measured CPU/accelerator costs.

Next, G2: a software 3D transform and rasterization reference first, then one
limited programmable stage with its ISA and precision defined before RTL, ending
in a rotating shaded object compared against images with documented tolerances.
Unified advanced integration remains S1.

## Later optional tracks

Expand the OS with shell/files/program loading and eventually scheduling/protection; build a small language/compiler/software VM; explore pipelining, GPU scheduling/masks, deeper NPU designs, a browser frontend, or an FPGA. Each track gets its own contract and completion checks when chosen. No fabricated chip or Linux-compatible platform is required by the current plan.
