/* Link script for programs (Track 2, O2): one slot of 256 KiB at SLOT_BASE,
 * given with --defsym. Code, data and .bss from the bottom, the heap above
 * them, the stack in the top 32 KiB (programs/rv32/os/sys.h). */
OUTPUT_ARCH(riscv)
ENTRY(_start)

PHDRS
{
    slot PT_LOAD FLAGS(7);
}

SECTIONS
{
    . = SLOT_BASE;
    .text : {
        KEEP(*(.text.init))
        *(.text .text.*)
    } :slot
    .rodata : {
        *(.rodata .rodata.*)
        *(.srodata .srodata.*)
    } :slot
    .data : ALIGN(4) {
        __data_start = .;
        *(.data .data.*)
        *(.sdata .sdata.*)
        . = ALIGN(4);
        __data_end = .;
    } :slot
    .bss (NOLOAD) : ALIGN(4) {
        __bss_start = .;
        *(.bss .bss.*)
        *(.sbss .sbss.*)
        *(COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } :slot
    _end = .;
    _stack_top = SLOT_BASE + 0x40000;
    _stack_bottom = _stack_top - 0x8000;
    ASSERT(_end <= _stack_bottom, "the program and its .bss overlap the stack")
}
