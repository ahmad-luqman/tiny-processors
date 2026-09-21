# Berkeley SoftFloat reference and software execution

Source: https://github.com/ucb-bar/berkeley-softfloat-3
Pinned commit: a0c6494cdc11865811dec815d5c0049fba9d82a8 (Release 3e)

This directory contains the unchanged C source dependency closure for the F1
binary32 oracle, upstream include headers, RISCV specialization files, and the
Linux-RISCV64-GCC platform header. The platform header uses compiler builtins,
not RISC-V instructions, and also builds with Apple Clang on arm64. F1 uses these sources as a host oracle. F2 also links them into the emulator's
host arithmetic adapter and compiles them separately into the optional RV32I
`floatsoft` benchmark. The benchmark defines `opts_GCC_h` to bypass the host
intrinsics header and use the generic primitives, with no edits to upstream
sources. It supplies guest compiler libcalls in `programs/rv32/rt/softfloat_abi.c`.
No SoftFloat code is synthesized into hardware or linked into the F-enabled
firmware images. The emulator and oracle share this arithmetic implementation;
literal anchors independently check their adapters.
See COPYING.txt; each upstream source also retains its license notice.
