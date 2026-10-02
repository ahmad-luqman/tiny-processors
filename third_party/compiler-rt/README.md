# compiler-rt builtins

The runtime functions clang calls on RV32I for what the hardware lacks: soft
double-precision floating point (`__adddf3`, `__muldf3`, comparisons and
conversions), 32- and 64-bit multiply and divide (`__mulsi3`, `__divdi3`, ...).
From [LLVM](https://github.com/llvm/llvm-project) `llvmorg-18.1.8`, commit
`3b5b5c1ec4a3095ab096dd780e84d7ab81f3d7ff`, `compiler-rt/lib/builtins`.

Only the 26 functions the Track 3 programs link are here ([record](../../docs/rv32-libc.md)),
listed in `SOURCES` (the Makefile's input), with the headers and `.inc` files
they include, copied unmodified at their upstream paths. The software is under
the Apache License 2.0 with LLVM exceptions (`LICENSE.TXT`).
[tools/rv32_vendor_libc.py](../../tools/rv32_vendor_libc.py) chose them.
`SHA256SUMS.json` holds each file's SHA-256.
