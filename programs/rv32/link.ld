/* Memory layout for the RV32 machine's M1 firmware image.
 *
 * One RAM region holds code, read-only data, initialized data, zeroed data,
 * and the stack. Load addresses equal run addresses, so the image is placed
 * in RAM exactly as linked and startup copies nothing. The contract RAM is
 * 4 MiB; this slice deliberately fits in the first 256 KiB (see board.h and
 * docs/rv32.md).
 */
OUTPUT_ARCH(riscv)
ENTRY(_start)

MEMORY
{
    RAM (rwx) : ORIGIN = 0x80000000, LENGTH = 256K
}

_stack_size = 16K;

/* A single loadable segment: without this, the linker would emit separate
 * read-only and read-write segments padded to page boundaries. */
PHDRS
{
    ram PT_LOAD FLAGS(7);
}

SECTIONS
{
    .text : {
        KEEP(*(.text.init))    /* _start must be the first instruction */
        *(.text .text.*)
    } > RAM :ram

    .rodata : {
        *(.rodata .rodata.*)
        *(.srodata .srodata.*)
    } > RAM :ram

    .data : ALIGN(4) {
        __data_start = .;
        *(.data .data.*)
        *(.sdata .sdata.*)
        . = ALIGN(4);
        __data_end = .;
    } > RAM :ram

    .bss (NOLOAD) : ALIGN(4) {
        __bss_start = .;
        *(.bss .bss.*)
        *(.sbss .sbss.*)
        *(COMMON)
        . = ALIGN(4);
        __bss_end = .;
    } > RAM :ram

    _end = .;
    _stack_bottom = ORIGIN(RAM) + LENGTH(RAM) - _stack_size;
    _stack_top = ORIGIN(RAM) + LENGTH(RAM);

    ASSERT(_end <= _stack_bottom, "image and .bss overlap the stack")
    ASSERT(_stack_top % 16 == 0, "stack top must be 16-byte aligned (ILP32 ABI)")
}
