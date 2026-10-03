# compiler-rt builtins

The runtime functions clang calls on RV32I for what the hardware lacks:
multiply and divide (`__mulsi3`, `__divdi3`, ...), 64-bit integers (shifts,
counts, overflow-checked multiplies), and soft single- and double-precision
floating point (`__addsf3`, `__muldf3`, comparisons and conversions).
From [LLVM](https://github.com/llvm/llvm-project) `llvmorg-18.1.8`, commit
`3b5b5c1ec4a3095ab096dd780e84d7ab81f3d7ff`, `compiler-rt/lib/builtins`.

Only part of the builtins is here ([record](../../docs/rv32-libc.md)): the 65
functions clang calls on RV32I for those (and `fp_mode.c`, which the float
functions use), whichever of them today's programs
reach, since that depends on the compiler's version as well as the program
(clang 20 calls `__floatundidf` for Lua's `math.random`, clang 18 does not).
They are listed in `SOURCES` (the Makefile's input), with the headers and `.inc` files
they include, copied unmodified at their upstream paths. The software is under
the Apache License 2.0 with LLVM exceptions (`LICENSE.TXT`).
[tools/rv32_vendor_libc.py](../../tools/rv32_vendor_libc.py) chose them.
`SHA256SUMS.json` holds each file's SHA-256.
