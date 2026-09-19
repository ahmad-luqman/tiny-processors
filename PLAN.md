# Tiny Processors: our computer and advanced SoC

Updated 2026-09-19 after the [planning interview](docs/planning/full-stack-plan.md). This is a planning milestone; no new CPU, emulator, OS, or accelerator has been implemented in this session.

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

These are recorded prior verification results, not newly rerun during planning. SAP8 stays frozen as the small teaching CPU. SIMD4 is a parallel compute prototype, not an integrated GPU or NPU. Its multiply/matrix extension moves after the playable computer.

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

## Next milestone: M1

Define the machine contract and make a complete, reproducible RV32I C firmware build: startup, linker script, image, disassembly, runtime helpers as needed, and an executable smoke test on an independent reference runner. Record assumptions and limitations before the CPU RTL depends on them. See the [next-session checklist](docs/planning/roadmap.md#next-implementation-session-m1-only).

The planning probe found that existing Homebrew Clang 22.1.8 can emit RV32I/ILP32 objects. Default Apple Clang 21 could not. A suitable executable linker was not found in the checked locations, and firmware linking remains unverified. This is the first toolchain gap to resolve, not evidence that C firmware already runs.

Keep the first implementation bounded to M1, commit verified work, then explain C → instructions → memory layout. Do not begin implementation automatically as part of this planning conversation.

## Later optional tracks

Expand the OS with shell/files/program loading and eventually scheduling/protection; build a small language/compiler/software VM; explore pipelining, GPU scheduling/masks, deeper NPU designs, a browser frontend, or an FPGA. Each track gets its own contract and completion checks when chosen. No fabricated chip or Linux-compatible platform is required by the current plan.
