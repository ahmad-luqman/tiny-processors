# Tiny Processors

Project vocabulary for the learning machines and the future full-stack computer. Architectural specifications and implementation choices live in the roadmap and individual design documents.

## Language

**Full-stack computer**:
Our own educational computer spanning hardware, system software, and a playable application, inspired by the end-to-end learning style of Nand2Tetris.
_Avoid_: reproduction of the Hack/Jack course platform, completed advanced SoC.

**First OS/runtime**:
The system software for the first playable computer: boot and menu behavior plus display, input, timing, and memory services used by the game.
_Avoid_: Unix-like OS, multitasking OS, host operating system.

**Guest machine**:
The computer whose CPU, memory, and devices our software sees, implemented by our emulator and RTL simulation.
_Avoid_: the host Mac, a virtualized macOS instance.

**Machine contract**:
The documented instructions, memory layout, device interfaces, and observable behavior that guest software relies on across execution backends.

**Firmware image**:
The compiled and linked guest program, including startup code, runtime, and applications, loaded into the machine's specified memory layout.

**Native frontend**:
The Mac window and host input adapter presenting the guest framebuffer and forwarding events to the machine.
_Avoid_: guest renderer, guest operating system.

**Instruction retirement**:
The point where an executed instruction commits its architectural effects, used to compare CPU behavior across implementations.

**RV32 machine**:
Our RISC-V computer as defined by the machine contract in docs/rv32.md: an RV32I CPU, RAM at 0x8000_0000, and memory-mapped devices, implemented by our emulator and RTL.
_Avoid_: SAP8, the QEMU virt board, a Linux-capable platform.

**Reference runner**:
An existing, independent implementation of enough of the machine contract to execute our firmware image and confirm its observable results; QEMU's virt board serves this role in M1.
_Avoid_: our emulator, the RTL simulator, a timing oracle.

**SAP8**:
The project's SAP-inspired teaching CPU and its instruction set.
_Avoid_: Hack computer, RISC-V CPU.

**SIMD4**:
The project's teaching compute engine, whose lanes execute a shared instruction stream on separate data.
_Avoid_: complete GPU, graphics renderer, NPU.

**Learning milestone**:
A bounded working artifact with verified behavior, an explanation connecting its source to the hardware or software it represents, and exercises for understanding it.

**CPU FPU**:
The floating-point execution unit serving our CPU instructions, introduced after the first playable computer.
_Avoid_: GPU floating-point lanes, NPU numeric format, software floating-point runtime.

**Advanced SoC**:
The longer-term project direction of integrating general-purpose processing, graphics processing, and neural-network acceleration with the memory and peripherals that make them a computer system.
_Avoid_: completed hardware, current SIMD4 implementation.
