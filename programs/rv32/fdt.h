/* A small read-only flattened device tree (FDT) reader (Track 1).
 *
 * At reset a1 holds the address of the platform's device tree: QEMU's virt
 * board puts one in RAM, our backends serve one from the boot ROM
 * (docs/rv32.md, "Boot convention"). This reader checks the header, walks the
 * structure block once per query, and decodes `reg` with the parent's
 * #address-cells and #size-cells (0, 1 or 2 each; 0 size cells is a size of 0,
 * as under /cpus; a 64-bit value must fit in 32 bits). It reads the blob a
 * byte at a time, so it needs no alignment and works from ROM or RAM alike,
 * and never writes it.
 *
 * Every query returns an fdt_status. A malformed tree is FDT_BAD_LAYOUT and
 * is never read outside the blocks the header describes; only FDT_NOT_FOUND
 * means the tree has no such node or entry, so only it may be read as
 * "absent".
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

typedef enum {
    FDT_OK = 0,
    FDT_BAD_MAGIC = 1,
    FDT_BAD_VERSION = 2,
    FDT_BAD_LAYOUT = 3,  /* a block lies outside the blob, or the structure is malformed */
    FDT_NOT_FOUND = 4,   /* no node matches, or the node has fewer reg entries than asked for */
    FDT_TOO_WIDE = 5,    /* more than 2 cells, or an address or size that does not fit in 32 bits */
    FDT_NO_REG = 6,      /* a node matches but has no reg, a partial entry, or no cells to size one */
    FDT_NO_PROPERTY = 7, /* a node matches but lacks the property asked for, or it is not whole cells */
} fdt_status;

/* Check the header of the blob at `address` and fill `t`; on failure `t` is left unchanged. */
fdt_status fdt_open(fdt *t, uintptr_t address);

/* The `index`th reg entry of the first node, in tree order, whose `property` matches `value`:
 * for "compatible" the string list contains it, for any other property the value is exactly that
 * string (e.g. "device_type" = "memory"). The first matching node answers even when a later one
 * would have the entry asked for. */
fdt_status fdt_find(const fdt *t, const char *property, const char *value, uint32_t index,
                    uint32_t *base, uint32_t *size);

/* fdt_find for the `node`th matching node (0 is the first), in tree order: QEMU's virt lists eight
 * "virtio,mmio" slots, of which only the ones with a device behind them are of use (O3). */
fdt_status fdt_find_nth(const fdt *t, const char *property, const char *value, uint32_t node, uint32_t index,
                        uint32_t *base, uint32_t *size);

/* The `index`th 32-bit cell of property `name` (e.g. "interrupts") of the first node whose `property`
 * matches `value`, found as fdt_find finds it; FDT_NOT_FOUND when no node matches or the property has
 * fewer cells (O1). */
fdt_status fdt_cell(const fdt *t, const char *property, const char *value, const char *name, uint32_t index,
                    uint32_t *cell);

/* A string property of the root node ("model", or the first entry of "compatible"), in `*value`. */
fdt_status fdt_root_string(const fdt *t, const char *property, const char **value);

/* Whether two NUL-terminated strings are equal (the firmware has no C library). */
int fdt_same(const char *a, const char *b);

#endif
