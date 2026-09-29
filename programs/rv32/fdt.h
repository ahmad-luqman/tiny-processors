/* A small read-only flattened device tree (FDT) reader (Track 1).
 *
 * At reset a1 holds the address of the platform's device tree: QEMU's virt
 * board puts one in RAM, our backends serve one from the boot ROM
 * (docs/rv32.md, "Boot convention"). This reader checks the header, walks the
 * structure block once per query, and decodes `reg` with the parent's
 * #address-cells and #size-cells (1 or 2 each; a 64-bit value must fit in 32
 * bits). It reads the blob a byte at a time, so it needs no alignment and
 * works from ROM or RAM alike, and never writes it.
 */
#ifndef RV32_FDT_H
#define RV32_FDT_H

#include <stdint.h>

typedef struct {
    const uint8_t *blob;
    uint32_t total;               /* totalsize */
    uint32_t structs, structs_end; /* structure block offsets */
    uint32_t strings, strings_end; /* strings block offsets */
} fdt;

enum {
    FDT_OK = 0,
    FDT_BAD_MAGIC = 1,
    FDT_BAD_VERSION = 2,
    FDT_BAD_LAYOUT = 3,  /* a block lies outside the blob, or the structure is malformed */
    FDT_NOT_FOUND = 4,
    FDT_TOO_WIDE = 5,    /* an address or size does not fit in 32 bits */
};

/* Check the header of the blob at `address` and fill `t`. */
int fdt_open(fdt *t, uintptr_t address);

/* The `index`th reg entry of the first node, in tree order, whose `property` matches `value`:
 * for "compatible" the string list contains it, for any other property the value is exactly that
 * string (e.g. "device_type" = "memory"). */
int fdt_find(const fdt *t, const char *property, const char *value, uint32_t index,
             uint32_t *base, uint32_t *size);

/* A string property of the root node ("model", "compatible"'s first entry), or 0. */
const char *fdt_root_string(const fdt *t, const char *property);

#endif
