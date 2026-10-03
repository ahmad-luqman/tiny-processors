# picolibc

[picolibc](https://github.com/picolibc/picolibc) 1.8.10, commit
`51a8b32857e75345c37652a80b5cda98b28d69e5` (2025-04-16), the C library of the
OS's Track 3 programs ([record](../../docs/rv32-libc.md)).

Only part of picolibc is here: the 143 source files that `libccheck` and `lua`
link (listed in `SOURCES`, the Makefile's input), every header those sources
and the programs include, and the licences (`COPYING.picolibc`,
`COPYING.NEWLIB`; each file keeps its own notice). The files are copied
unmodified, at their upstream paths. `picolibc.h` is not an upstream file: it
is the configuration header meson generated for this build,

```sh
meson setup build picolibc --cross-file cross-rv32i.txt -Dmultilib=false -Dpicocrt=false \
  -Dsemihost=false -Dtests=false -Dthread-local-storage=false -Dsingle-thread=true \
  -Dtinystdio=true -Dio-long-long=true -Dposix-console=true -Dnewlib-global-errno=true \
  -Dspecsdir=none --buildtype=release
```

with a cross file naming clang for `--target=riscv32-unknown-elf -march=rv32i
-mabi=ilp32 -mcmodel=medlow -mno-relax` (`system = 'none'`, `cpu_family =
'riscv32'`). [tools/rv32_vendor_libc.py](../../tools/rv32_vendor_libc.py)
copied the files from that build and the programs' link maps; run it again
when a program needs a function that is not here. `SHA256SUMS.json` holds each
file's SHA-256.

Issue #33 builds the same sources a second time for the single-float ABI
(`-march=rv32imf_zicsr -mabi=ilp32f`, the Makefile's `RV32_ARCH_HF`), for
`mandel`. Under `__riscv_flen` picolibc's `machine/fenv.h` includes
`machine/fenv-fp.h`, the one header that build needs beyond the soft one's; it
was copied from the same commit, unmodified. No source was added: the
hard-float link resolves from `SOURCES` as it is.
