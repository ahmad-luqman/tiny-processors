/* Link script for programs (Track 2, O2): SLOT_SPAN bytes (one or more slots
 * of 128 KiB since O4) at SLOT_BASE, both given with --defsym. Code, data and
 * .bss from the bottom, the heap above them, the stack in the top STACK_SIZE
 * bytes (--defsym too since Track 3; 32 KiB, programs/rv32/os/sys.h, unless a
 * program needs more). */
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
        KEEP(*(.text.fault)) /* faulting instructions (fault.c, dmaprobe.c), at a fixed place */
        *(.text .text.*)
    } :slot
    .rodata : {
        *(.rodata .rodata.*)
        *(.srodata .srodata.*)
        /* Constructors and destructors, which only programs built with the C library have (Track 3:
         * picolibc's stdout is flushed by one); read-only, so they stay in this section. */
        . = ALIGN(4);
        __preinit_array_start = .;
        __preinit_array_end = .;
        __bothinit_array_start = .;
        __init_array_start = .;
        KEEP(*(SORT_BY_INIT_PRIORITY(.init_array.*)))
        KEEP(*(.init_array))
        __init_array_end = .;
        __bothinit_array_end = .;
        __fini_array_start = .;
        KEEP(*(SORT_BY_INIT_PRIORITY(.fini_array.*)))
        KEEP(*(.fini_array))
        __fini_array_end = .;
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
    _stack_top = SLOT_BASE + SLOT_SPAN;
    /* STACK_SIZE, given with --defsym too, is 32 KiB unless a program asks for more (Track 3: Lua).
     * The kernel leaves the stack's lowest page unmapped, so an overflow faults rather than running
     * into the heap. */
    _stack_bottom = _stack_top - STACK_SIZE;
    ASSERT(_end <= _stack_bottom, "the program and its .bss overlap the stack")
}
