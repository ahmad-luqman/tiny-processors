# Dhrystone

Reinhold Weicker's Dhrystone synthetic integer benchmark as packaged in
[riscv-tests](https://github.com/riscv-software-src/riscv-tests)
(`benchmarks/dhrystone`), commit `bcffa2b3188b040c611f90dc0b6e422f54775a09`
(2026-09-25), used by Track 0's performance baseline
([record](../../docs/rv32-groundwork.md#coremark-and-dhrystone)). The files are
copied unmodified; `LICENSE` is riscv-tests' licence, which the files refer to.
They expect riscv-tests' `util.h` and a hosted C library; the small freestanding
replacements (`util.h`, `stdio.h`, `string.h`, `alloca.h`) are in
`programs/rv32/bench/dhrystone/`.

| File | SHA-256 |
| --- | --- |
| `dhrystone.c` | `11eb8a10d14d4a0b2e087f7d5e9ab67f76b1e9b1fd682457b5ed3ac1e7534e97` |
| `dhrystone.h` | `a66aa6e73b38afb85021b2ac751402c8b27d47e48beffed852afb36aa94a2af5` |
| `dhrystone_main.c` | `45f1aeb092b50ccbd2f37782e5b054b446c93c49771a45a36c940386f72d338d` |
| `LICENSE` | `43b7309213293b4323d65301a90e05472579abcd51feb260a850354bcb49ff8f` |
