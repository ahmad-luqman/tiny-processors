# CoreMark

The benchmark core of [EEMBC CoreMark](https://github.com/eembc/coremark), commit
`1f483d5b8316753a742cbf5590caf5bd0a4e4777` (2025-05-01), used by Track 0's
performance baseline ([record](../../docs/rv32-groundwork.md#coremark-and-dhrystone)).
The files are copied unmodified; the port for our machine (`core_portme.h`,
`core_portme.c`, and our own `ee_printf`) is `programs/rv32/bench/`. The
software is under the Apache License 2.0 and the name under EEMBC's acceptable
use agreement, both in `LICENSE.md`. Our figures are cycle counts from a
simulated machine, not certified CoreMark scores.

| File | SHA-256 |
| --- | --- |
| `core_list_join.c` | `ca00e4e010ece47d7f040cb92aa50a95345a00d3171b59d088f6b243be06ce7b` |
| `core_main.c` | `17884c93c5b94378eb0ff02b4df3725756cf2addb9b8cbcaa6200a4649ff5ca7` |
| `core_matrix.c` | `ecdff717b5a5c4907d221a606760e25499899cbf617582c05d40db71c91351e4` |
| `core_state.c` | `f4b84bb0a3452c45a4daa664ab502bfdccbd31cb57d93e9ac490c60937717a4e` |
| `core_util.c` | `a3fbfcb9bb943b638624b8ece01c5836dd56a96d7bcde2697b248d077447327f` |
| `coremark.h` | `42642b9a06c7ed2b3bd9eda971b7c3868c4f5d27bf7ef6c4bba11291a0c2598a` |
| `LICENSE.md` | `9577b9c846f61fd69a0d8ac965998c6450c7d8969f71f0bb4a931b91aebad28a` |
