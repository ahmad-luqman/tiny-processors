# Lua

[Lua](https://www.lua.org) 5.4.7, from the Lua team's repository
<https://github.com/lua/lua>, tag `v5.4.7`, commit
`1ab3208a1fceb12fca8f24ba57d6e13c5bff15e3` (2024-06-13). Track 3's `lua`
program ([record](../../docs/rv32-libc.md#l2-lua)) is the stand-alone
interpreter `lua.c` with the library, built on picolibc for the OS.

The files are the interpreter's sources from the repository's top level,
copied unmodified; the repository's test suite (`ltests.c`, `ltests.h`,
`testes/`), the amalgamation `onelua.c` and the manual are left out. Nothing in
`luaconf.h` is changed: the build gives Lua its defaults (64-bit integers,
double floats) and only names the platform's C library through the compiler
flags in the Makefile. The software is under the MIT license, whose text is
at the end of `lua.h`. `SHA256SUMS.json` holds each file's SHA-256.
