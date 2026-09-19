# RV32 emulator: design, trace contract, and walkthrough

Our headless emulator for the [RV32 machine](rv32.md). It loads the same firmware image QEMU ran in M1, executes RV32I with the contract's trap, alignment, console, and done-register behavior, and records a retirement trace that the M3 RTL testbench must reproduce.

## Implementation plan (M2)

1. Measure a tiny interpreter loop in C and Python to pick the language, and probe the host compiler.
2. Loader, RAM, decode, and straight-line execution with hand-computed edge tests.
3. Jumps and branches, including the instruction-address-misaligned trap.
4. Traps, the minimum CSR subset, and the console and done devices, with every "undefined" edge pinned to a fault.
5. Retirement trace; run `selfcheck.elf` and require the same `PASS 807d9fad` QEMU produced; compare the PC sequence with QEMU.
6. Documentation, trace walkthrough, roadmap update.

## Language choice

Roadmap step 1 asked for a short performance and tooling check before choosing the emulator language. The check was a three-opcode fetch/decode/execute loop (`addi`, `add`, `bne`) written the same way in C and in Python, run on the development Mac on 2026-09-19:

| Implementation | Instructions | Wall time | Rate |
| --- | --- | --- | --- |
| C, Apple clang 21, `-O2` | 200,000,000 | 0.159 s | about 1,260 M instructions/s |
| Python 3.14 | 5,000,000 | 3.44 s | about 1.5 M instructions/s |

The C figure is an upper bound: a real emulator adds address decoding, byte-lane merging, device checks, and trace output on every instruction, so expect tens to a few hundred million instructions per second. The Python figure is about what a straightforward Python emulator would achieve. The M1 self-check would run under either, but the first playable computer (M6) needs to clear and redraw a framebuffer many times per second on top of game logic, which a 1.5 M instructions/s core cannot do. The emulator is therefore written in C: one file, C11, no dependencies, compiled by the host `cc` that ships with Xcode's command line tools (`make toolchain-rv32-emu` checks it). The ELF loader stays in Python: `tools/rv32_image.py` already flattens the image, and the emulator reads that flat file, exactly as the RTL testbench will.
