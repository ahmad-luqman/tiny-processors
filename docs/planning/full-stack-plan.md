# Full-stack computer and advanced SoC: planning interview

Status: Q1–Q12 and the floating-point follow-up answered on 2026-09-19. This document records the interview and factual checks; the resulting [detailed roadmap](roadmap.md) is ready for review. Implementation details labeled proposed in that roadmap remain adjustable. This session changes planning documents only.

## Confirmed intent and existing work

- The user chose our own end-to-end computer, inspired by Nand2Tetris, preserving existing work. Following the actual Hack/VM/Jack course is not the selected route. The stack includes a small OS/runtime and a playable game, with advanced SoC integration later.
- The user prefers complete verified implementation milestones followed by a walkthrough, rather than required prediction/approval stops before each small implementation step. Explanations and exercises remain part of each milestone.
- There is no fixed weekly schedule or delivery deadline. Begin with achievable working milestones, observe progress, and revise approximate effort estimates from experience. Do not turn the absence of a deadline into a requirement to finish every advanced feature before the first playable system.
- Use C and an existing compiler to reach the first playable machine. A separate language/compiler/software-VM learning track belongs later; building those tools is not a prerequisite for the first game.
- Implement our own RV32I CPU and matching emulator, reusing an existing C compiler. The standard ISA defines instruction behavior; we design and learn the datapath and control ourselves. SAP8 remains the preserved smaller teaching CPU. The complete firmware toolchain still needs to be established and verified.
- The first OS/runtime boots into a menu and supplies display, input, timing, and memory services for a game. Shells, persistent files, multiple-program support, and more advanced OS features belong to later expansion milestones, not the first playable-system acceptance criteria.
- Tetris is the first complete-system capstone. Pong comes earlier to demonstrate working input, animation, and timing before adding the menu and Tetris game.
- Present the interactive emulator in a native Mac window first. The host displays the guest framebuffer and forwards input; our OS/runtime and game execute inside the emulated machine. Browser delivery is an optional later frontend.
- Finish the playable computer with CPU-drawn graphics before GPU/NPU development and integration. Move the previously proposed SIMD multiplication/matrix-kernel work after the playable capstone; preserve the existing SIMD4 prototype for the accelerator track.
- An integrated CPU/GPU/NPU SoC is an advanced direction to include in the roadmap.
- Graphics progresses from a 2D accelerator to a small programmable 3D pipeline rendering a rotating shaded object. Commercial graphics API compatibility is outside the initial scope.
- The NPU demonstration recognizes a handwritten digit drawn in the guest UI using a small pretrained network. Build inference hardware and the driver, compare against a software reference, and reuse trained weights initially.
- Prior background includes logic design, computer architecture, and a breadboard SAP CPU; the project began as a practical Verilog refresher.
- Keep useful explanations in `docs/`, connect RTL constructs to gates and storage, preserve working prototypes and commands, and commit verified milestones incrementally.
- The counter, ALU, SAP8 CPU/assembler, and SIMD4 vector-add engine are implemented and verified. They remain the starting inventory; they do not yet form one integrated computer.
- The finished target is a complete emulated computer on the existing Apple Silicon Mac, with RTL simulation used to verify the hardware design. FPGA deployment is an optional later track, not a completion requirement. Physical chip fabrication is not a selected project requirement.

## Interview method

Use the user-requested [grill-with-docs approach](https://www.aihero.dev/skills-grill-with-docs), with its [grilling](https://github.com/mattpocock/skills/blob/main/skills/productivity/grilling/SKILL.md) and [domain-modeling](https://github.com/mattpocock/skills/blob/main/skills/engineering/domain-modeling/SKILL.md) instructions. Ask decisions in rounds, resolve factual questions from the repository or sources, and record answers as they arrive.

Project vocabulary belongs in [CONTEXT.md](../../CONTEXT.md). This planning document retains the user's answers and roadmap decisions, as explicitly requested, including decisions too small to merit an architecture decision record. Use ADRs only for consequential trade-offs that would be costly to revisit and unclear without their original reasoning.

## Round 1: settled direction

| ID | Decision | Answer or pending recommendation | Status |
| --- | --- | --- | --- |
| Q1 | Actual Nand2Tetris Hack/VM/Jack course, our own inspired machine, or both paths? | User selected our own inspired computer, retaining current work, with advanced SoC integration later | Settled |
| Q2 | Guided small checkpoints, user-written components, or complete agent-built milestones with walkthroughs? | User selected complete milestones, then walk through them | Settled |
| Q3 | Fully emulated destination, eventual FPGA requirement, or eventual fabricated-chip ambition? | User selected a complete emulated machine; FPGA deployment is optional | Settled |
| Q4 | Weekly time available and any deadline for the first playable system? | No fixed budget/deadline; start and evaluate how far and how quickly the project progresses | Settled |

## Round 2: first complete system

| ID | Decision | Answer | Status |
| --- | --- | --- | --- |
| Q5 | Reuse a C compiler, or make our own language/compiler/software VM part of the required learning stack? | User selected C first, with a language/compiler track later | Settled |
| Q6 | Minimum OS/runtime services, shell/files/multiple programs, or Unix-like OS as the first goal? | User selected boot/menu/game services first, with OS expansion later | Settled |
| Q7 | First playable capstone: Tetris, another 2D game, or a 3D demo? | User selected Tetris, with Pong as an earlier input/animation/timing demonstration | Settled |

C-first, the initial runtime scope, the games, RV32I, native Mac presentation, and playable-computer-first ordering are settled.

## Round 3: architecture and sequencing

Open recommendations remain proposals until answered.

| ID | Decision | Recommendation, awaiting answer | Status |
| --- | --- | --- | --- |
| Q8 | Standard ISA with our own CPU implementation, or another custom ISA with additional compiler work? | User selected our own RV32I CPU with an existing C compiler; retain SAP8 as the smaller teaching CPU | Settled |
| Q9 | Where should the interactive emulator run? | User selected a native Mac window first; browser delivery can be a later frontend | Settled |
| Q10 | Reach the playable computer first, or interleave graphics/neural accelerator development? | User selected the playable computer first, with GPU/NPU afterward; the matrix kernel moves later | Settled |

### C-toolchain feasibility checked locally

On 2026-09-19, the existing Homebrew Clang 22.1.8 at `/opt/homebrew/opt/llvm@22/bin/clang` successfully compiled a temporary freestanding C probe with `--target=riscv32-unknown-elf -march=rv32i -mabi=ilp32 -ffreestanding -O2 -c` into an ELF32 RISC-V object. The default Apple Clang 21.0.0 could not generate code for that target. No tools were installed and no implementation files were changed.

This proves compilation to an object, **not a complete firmware build**. No suitable linker was found on PATH or in the checked LLVM bin directory; the attempted executable link fell back to the system linker and failed. With RV32I selected in Q8, a toolchain milestone must supply a suitable ELF linker, startup code, linker script, applicable runtime helpers, and an executable smoke test.

The probe generated `__mulsi3` and `__udivsi3` calls for variable multiplication and division. RV32I keeps hardware multiply/divide outside the base ISA; those operations can initially use software helpers. Freestanding C may also require memory routines. See the [RV32I specification](https://docs.riscv.org/reference/isa/v20240411/unpriv/rv32.html), [LLVM RISC-V support](https://llvm.org/docs/RISCVUsage.html), [GCC integer runtime routines](https://gcc.gnu.org/onlinedocs/gccint/Integer-library-routines.html), and [Clang freestanding documentation](https://clang.llvm.org/docs/UsersManual.html#freestanding-builds).

A standard instruction set still leaves us designing the CPU's datapath and control. A bare-metal game machine using RV32I is also distinct from a full privileged platform or Linux-compatible system; those would require separate scope and verification. See the [privileged architecture introduction](https://docs.riscv.org/reference/isa/priv/priv-intro.html).

## Round 4: concrete advanced demonstrations

Both demonstration goals are settled. Neither accelerator is required for the first playable computer.

| ID | Decision | Recommendation, awaiting answer | Status |
| --- | --- | --- | --- |
| Q11 | What graphics capability should the advanced track ultimately demonstrate? | User selected 2D acceleration, then programmable 3D | Settled |
| Q12 | What real inference workload should demonstrate the NPU? | User selected handwritten-digit recognition with a small pretrained network | Settled |

The initial accelerator designs should be bounded enough to verify independently, then integrated with CPU-visible commands, explicit memory ownership, and reference comparisons. Detailed GPU stages and NPU arithmetic/model choice follow from the demonstration goals; they are not selected yet. The sequenced milestones, completion criteria, and deferred decisions are in the [detailed roadmap](roadmap.md).

## Follow-up: floating-point instructions

| ID | Decision | Answer | Status |
| --- | --- | --- | --- |
| Q13 | Make hardware floating point an explicit milestone or leave it optional? | User selected an FP32 unit and CPU F-extension integration after Tetris, before programmable 3D | Settled |

Implement this as two verified increments: standalone FP32 arithmetic, then full F-extension integration into the CPU and emulator. The first playable system remains RV32I. GPU and NPU numeric formats are independent decisions; double precision and lower-precision floating-point extensions remain later possibilities. The complete F-extension claim requires its specified instruction, rounding, status, and exceptional-value behavior, not just floating-point addition/multiplication.

## Result

The interview establishes the scope and ordering. [PLAN.md](../../PLAN.md) is the project entry point; the [detailed roadmap](roadmap.md) defines acceptance scenarios, interfaces, milestone checks, learning exercises, effort ranges, and decisions deferred until implementation evidence is available.

There is no fixed delivery deadline. The next implementation milestone is the machine contract and complete C firmware toolchain, not a matrix kernel or an entire CPU in one step. This planning session does not start that implementation.
