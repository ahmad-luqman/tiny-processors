# Tiny Processors: our computer and advanced SoC

Updated 2026-09-21 after the [planning interview](docs/planning/full-stack-plan.md) and the completed M1 to M7 and F1 sessions. The machine contract, the firmware toolchain, the emulator with its native window, a full RV32I multicycle RTL CPU, the machine around it (bus decoder, timer, input queue, display and framebuffer, matched on both backends), Pong, the boot menu/runtime/Tetris capstone, and a standalone verified FP32 FPU exist; no accelerator for the new computer does yet.

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
| RV32 machine contract and M1 firmware | 28-check freestanding C self-check runs on QEMU virt with the bare `rv32i` model: `PASS 807d9fad`, exit status 0; 17 tool tests and 6 host runtime tests; ELF image checks pass | [Contract](docs/rv32.md), [C to instructions](docs/c-to-instructions.md); `make test-rv32`, `make check-rv32-image`, `make run-rv32-qemu`, `make disasm-rv32` |
| RV32 headless emulator (M2) | C emulator runs the same image to `PASS 807d9fad` in 32,610 instructions and matches QEMU's PC sequence instruction for instruction; 30 hand-computed edge tests cover arithmetic, branches, jumps, loads/stores, traps, CSRs, and devices; about 400 M instructions/s untraced | [Emulator record and trace walkthrough](docs/rv32-emulator.md); `make test-rv32-emu`, `make run-rv32-emu`, `make trace-rv32-emu`, `make diff-rv32-qemu` |
| RV32 multicycle RTL CPU slice (M3) | Eleven-instruction core with one ready/valid memory port; a 78-instruction loop produces the emulator's trace line for line on Icarus and Verilator with 0 to 3 fixed and seeded random stall cycles; every other encoding halts as a terminal fault or `unsupported`; 20 tests (differential, decode sweep, harness), lint, and synthesis (5,777 cells, 1,331 flip-flops, no latches) pass | [RTL contract](docs/rv32-rtl.md), [gates and cycles](docs/rv32-to-gates.md); `make test-rv32-rtl`, `make test-rv32-rtl-verilator`, `make lint-rv32`, `make synth-rv32`, `make waves-rv32`, `make bench-rv32-rtl` |
| RV32 full RV32I RTL CPU (M4) | Every RV32I instruction plus the four trap CSRs and `mret`; traps vector and a double fault halts as in the emulator; byte strobes on reads and writes; the C self-check runs on the core to `PASS 807d9fad` with the emulator's 32,610-line trace on Icarus and Verilator, unstalled and stalled, in 138,495 cycles; lint and synthesis (8,175 cells, 1,457 flip-flops, no latches) pass | [RTL record and coverage table](docs/rv32-rtl.md), [gates, waves, and cycles](docs/rv32-to-gates.md); `make test-rv32-rtl`, `make run-rv32-rtl`, `make run-rv32-rtl-verilator`, `make waves-rv32` |
| RV32 machine with devices (M5) | Bus decoder and RTL peripherals (timer, 16-event input queue with held keys, display controller, 320×240 framebuffer, console, done) matched by emulator models with the same fault edges; the device diagnostic passes on the emulator, Icarus (1,666,436 cycles), and Verilator with identical console lines and frame checkpoints, its checksum and frame hash derived independently; 36 RTL tests per simulator, 31 emulator tests, 18 tool tests; lint and synthesis of the machine (23,664 cells with 64-word memories, no latches) pass | [SoC record](docs/rv32-soc.md), [contract](docs/rv32.md#behavior-fixed-in-m5); `make run-rv32-diag-emu`, `make run-rv32-diag-rtl`, `make lint-rv32-soc`, `make synth-rv32-soc` |
| RV32 native window and Pong (M6) | The emulator as a core library with a headless main and an SDL3 window that shows presents and queues host keys as frame events, with a recording that replays; drawing routines and Pong in device-free C tested natively at -O0 and -O2; a 200-frame scripted session gives the same 200 checkpoints and `PASS 8fef54bc` on the native build, the emulator, Icarus (1,998,070 cycles), and Verilator, trace-identical for 478,797 instructions; 7 Pong tests, 5 window tests, 33 emulator tests, 38 RTL tests per simulator, 19 tool tests; no RTL change | [Window record](docs/rv32-window.md), [contract](docs/rv32.md#behavior-fixed-in-m6); `make run-rv32-pong`, `make run-rv32-pong-emu`, `make run-rv32-pong-rtl`, `make test-rv32-pong` |
| First complete computer (M7) | One RV32I image boots the menu and runs Pong/Tetris; native rules and rendering tests at -O0/-O2; 68 checkpoints, `PASS ea60197e`, 2,220,509 trace-identical instructions on emulator and both RTL simulators; both games played by the user | [Runtime and Tetris](docs/rv32-runtime.md); `make run-rv32-capstone`, `make test-rv32-capstone`, `make run-rv32-capstone-rtl` |
| Standalone FP32 hardware (F1) | All five rounding modes; 70,407 vectors on both simulators; 259,407 additional stress vectors; reset/backpressure checks; 25,685 generic cells and 1,530 flip-flops without latches | [FP32 contract and walkthrough](docs/fp32.md); `make test-fp32`, `make test-fp32-verilator`, `make waves-fp32` |

These rows record each milestone’s acceptance baseline; the F1 session also reran the preserved machine and lab regressions. SAP8 stays frozen as the small teaching CPU. SIMD4 is a parallel compute prototype, not an integrated GPU or NPU. Its multiply/matrix extension moves after the playable computer.

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

## Next milestone: F2

F1's standalone FP32 hardware is verified against pinned Berkeley SoftFloat.
Next, integrate the complete F instruction/state contract into the CPU and
emulator: floating registers, CSR/Zicsr behavior, loads/stores, moves, sign and
classification operations, arithmetic requests, flags, and safe writeback.
Select compatible compiler ISA/ABI flags, run float programs with runtime inputs,
and retain every integer firmware regression. See the [F2 checklist](docs/planning/roadmap.md#next-implementation-session-f2).

## Later optional tracks

Expand the OS with shell/files/program loading and eventually scheduling/protection; build a small language/compiler/software VM; explore pipelining, GPU scheduling/masks, deeper NPU designs, a browser frontend, or an FPGA. Each track gets its own contract and completion checks when chosen. No fabricated chip or Linux-compatible platform is required by the current plan.
