# Berkeley SoftFloat test reference

Source: https://github.com/ucb-bar/berkeley-softfloat-3
Pinned commit: a0c6494cdc11865811dec815d5c0049fba9d82a8 (Release 3e)

This directory contains the unchanged C source dependency closure for the F1
binary32 oracle, upstream include headers, RISCV specialization files, and the
Linux-RISCV64-GCC platform header. The platform header uses compiler builtins,
not RISC-V instructions, and also builds with Apple Clang on arm64. The reference
is host-only and is never linked into firmware or synthesized into hardware.
See COPYING.txt; each upstream source also retains its license notice.
