/* The architectural tests' layout: the test file's .text.init (entry point first) at the
 * reset PC, then its code and data. Everything must fit the machine's 4 MiB of RAM. */
OUTPUT_ARCH(riscv)
ENTRY(rvtest_entry_point)
SECTIONS
{
    . = 0x80000000;
    .text.init : { *(.text.init) }
    .text : { *(.text) }
    .data : { *(.data) *(.data.*) }
    .bss : { *(.bss) }
    _end = .;
    ASSERT(_end <= 0x80400000, "the test does not fit the machine's 4 MiB of RAM")
}
