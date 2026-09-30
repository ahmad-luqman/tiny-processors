/* Read-only FDT reader; see fdt.h. The header's own 40 bytes are read as they
 * are; after that every offset is checked against the blocks the header
 * describes before it is read, so a malformed tree ends a query with an error
 * status rather than a read outside the blob. */
#include "fdt.h"

#define FDT_MAGIC 0xd00dfeedu
#define FDT_BEGIN_NODE 1u
#define FDT_END_NODE 2u
#define FDT_PROP 3u
#define FDT_NOP 4u
#define FDT_END 9u
#define FDT_MAX_DEPTH 8
#define FDT_MAX_CELLS 2  /* 64-bit values; wider ones cannot describe this 32-bit machine */

static uint32_t be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

int fdt_same(const char *a, const char *b)
{
    while (*a && *a == *b) {
        a++;
        b++;
    }
    return *a == *b;
}

fdt_status fdt_open(fdt *t, uintptr_t address)
{
    const uint8_t *h = (const uint8_t *)address;
    if (be32(h) != FDT_MAGIC) {
        return FDT_BAD_MAGIC;
    }
    /* version 17 is current; a blob is readable by us if it is at least 16 compatible */
    if (be32(h + 20) < 16 || be32(h + 24) > 17) {
        return FDT_BAD_VERSION;
    }
    fdt r;
    r.blob = h;
    r.total = be32(h + 4);
    r.structs = be32(h + 8);
    r.strings = be32(h + 12);
    r.structs_end = r.structs + be32(h + 36);
    r.strings_end = r.strings + be32(h + 32);
    if (r.total < 40 || r.structs < 40 || r.structs_end < r.structs || r.structs_end > r.total ||
        r.strings < 40 || r.strings_end < r.strings || r.strings_end > r.total || (r.structs & 3u)) {
        return FDT_BAD_LAYOUT;
    }
    *t = r;
    return FDT_OK;
}

/* A property's value, as found while walking. */
typedef struct {
    uint32_t offset, length;
} span;

/* Is there a NUL in [at, end)? A property name must end inside the strings block before it is
 * compared with anything, or the comparison could run past the blob. */
static int terminated(const fdt *t, uint32_t at, uint32_t end)
{
    for (; at < end; at++) {
        if (t->blob[at] == 0) {
            return 1;
        }
    }
    return 0;
}

/* Does `value` (a NUL-separated list for "compatible") match? */
static int matches(const fdt *t, span v, int list, const char *want)
{
    uint32_t at = v.offset, end = v.offset + v.length;
    while (at < end) {
        const char *s = (const char *)t->blob + at;
        uint32_t n = 0;
        while (at + n < end && s[n]) {
            n++;
        }
        if (at + n == end) {
            return 0; /* not NUL-terminated */
        }
        if (fdt_same(s, want)) {
            return 1;
        }
        if (!list) {
            return 0;
        }
        at += n + 1;
    }
    return 0;
}

/* Read a `cells`-wide number (0, 1 or 2 cells, checked by the caller) that must fit in 32 bits;
 * zero cells is 0, as a cpu's size under #size-cells = <0>. */
static fdt_status number(const fdt *t, uint32_t at, uint32_t cells, uint32_t *out)
{
    if (cells == 0) {
        *out = 0;
        return FDT_OK;
    }
    if (cells == 2) {
        if (be32(t->blob + at) != 0) {
            return FDT_TOO_WIDE;
        }
        at += 4;
    }
    *out = be32(t->blob + at);
    return FDT_OK;
}

/* Entry `index` of a matched node's reg, sized by its parent's cells. The cell counts are bounded
 * before any arithmetic, and the index is compared with the entry count rather than multiplied,
 * so neither can wrap a 32-bit offset (review on PR #18). */
static fdt_status reg_entry(const fdt *t, span reg, uint32_t ac, uint32_t sc, uint32_t index,
                            uint32_t *base, uint32_t *size)
{
    if (ac > FDT_MAX_CELLS || sc > FDT_MAX_CELLS) {
        return FDT_TOO_WIDE;
    }
    uint32_t entry = 4u * (ac + sc);
    if (entry == 0 || reg.length == 0 || reg.length % entry != 0) {
        return FDT_NO_REG;
    }
    if (index >= reg.length / entry) {
        return FDT_NOT_FOUND;
    }
    uint32_t e = reg.offset + entry * index;
    fdt_status status = number(t, e, ac, base);
    return status != FDT_OK ? status : number(t, e + 4u * ac, sc, size);
}

/* The PROP token whose length word is at `*at`: check that its value lies in the structure block
 * and its name ends inside the strings block, then step `*at` past the value. */
static fdt_status read_prop(const fdt *t, uint32_t *at, span *value, const char **key)
{
    if (*at + 8 > t->structs_end) {
        return FDT_BAD_LAYOUT;
    }
    span v = {*at + 8, be32(t->blob + *at)};
    uint32_t name = t->strings + be32(t->blob + *at + 4);
    if (v.offset + v.length > t->structs_end || v.offset + v.length < v.offset ||
        name < t->strings || !terminated(t, name, t->strings_end)) {
        return FDT_BAD_LAYOUT;
    }
    *at = (v.offset + v.length + 3u) & ~3u;
    *value = v;
    *key = (const char *)t->blob + name;
    return FDT_OK;
}

/* Step `*at` past a node name (the word after BEGIN_NODE), NUL included, to the next word. */
static void skip_name(const fdt *t, uint32_t *at)
{
    while (*at < t->structs_end && t->blob[*at]) {
        (*at)++;
    }
    *at = (*at + 4u) & ~3u;
}

/* Walk the whole tree for the first node whose `property` matches `value`, and decode its reg
 * entry `index`, or with `cell_name` the `index`th 32-bit cell of that property instead. A node's
 * properties come before its children, so a node is decided at the first BEGIN_NODE, END_NODE or
 * END after its BEGIN_NODE. */
static fdt_status find(const fdt *t, const char *property, const char *value, uint32_t node, uint32_t index,
                       uint32_t *base, uint32_t *size, const char *cell_name)
{
    uint32_t address_cells[FDT_MAX_DEPTH], size_cells[FDT_MAX_DEPTH];
    int depth = -1;
    int matched = 0, in_props = 0;
    span reg = {0, 0}, named = {0, 0};
    int has_named = 0;
    uint32_t at = t->structs;
    int list = fdt_same(property, "compatible");
    int by_name = fdt_same(property, "@name"); /* match the node's own name, not a property (O4) */

    for (;;) {
        if (at + 4 > t->structs_end) {
            return FDT_BAD_LAYOUT;
        }
        uint32_t token = be32(t->blob + at);
        at += 4;
        if (token == FDT_BEGIN_NODE || token == FDT_END_NODE || token == FDT_END) {
            /* The properties of the current node end here: decide on it. */
            if (in_props && matched && node) {
                node--; /* an earlier match: keep looking for the one asked for */
                matched = 0;
            }
            if (in_props && matched && cell_name) {
                if (!has_named || named.length % 4 != 0) {
                    return FDT_NO_PROPERTY;
                }
                if (index >= named.length / 4) {
                    return FDT_NOT_FOUND;
                }
                *base = be32(t->blob + named.offset + 4u * index);
                return FDT_OK;
            }
            if (in_props && matched) {
                if (depth < 1) {
                    return FDT_NO_REG; /* the root has no parent to size a reg with */
                }
                return reg_entry(t, reg, address_cells[depth - 1], size_cells[depth - 1], index, base, size);
            }
            in_props = 0;
        }
        if (token == FDT_BEGIN_NODE) {
            uint32_t name = at;
            skip_name(t, &at);
            if (++depth >= FDT_MAX_DEPTH) {
                return FDT_BAD_LAYOUT;
            }
            address_cells[depth] = 2; /* the specification's defaults for the children */
            size_cells[depth] = 1;
            matched = by_name && at <= t->structs_end && terminated(t, name, at) &&
                      fdt_same((const char *)t->blob + name, value);
            in_props = 1;
            reg.length = 0;
            has_named = 0;
        } else if (token == FDT_END_NODE) {
            if (depth-- < 0) {
                return FDT_BAD_LAYOUT;
            }
        } else if (token == FDT_PROP) {
            span v;
            const char *key;
            if (depth < 0 || read_prop(t, &at, &v, &key) != FDT_OK) {
                return FDT_BAD_LAYOUT;
            }
            int address_key = fdt_same(key, "#address-cells"), size_key = fdt_same(key, "#size-cells");
            if ((address_key || size_key) && v.length != 4) {
                return FDT_BAD_LAYOUT; /* a cell count is one 32-bit word */
            }
            if (address_key) {
                address_cells[depth] = be32(t->blob + v.offset);
            } else if (size_key) {
                size_cells[depth] = be32(t->blob + v.offset);
            } else if (fdt_same(key, "reg")) {
                reg = v;
            }
            if (cell_name && fdt_same(key, cell_name)) {
                named = v;
                has_named = 1;
            }
            if (fdt_same(key, property) && matches(t, v, list, value)) {
                matched = 1;
            }
        } else if (token == FDT_END) {
            return depth == -1 ? FDT_NOT_FOUND : FDT_BAD_LAYOUT;
        } else if (token != FDT_NOP) {
            return FDT_BAD_LAYOUT;
        }
    }
}

fdt_status fdt_find(const fdt *t, const char *property, const char *value, uint32_t index,
                    uint32_t *base, uint32_t *size)
{
    return find(t, property, value, 0, index, base, size, 0);
}

fdt_status fdt_find_nth(const fdt *t, const char *property, const char *value, uint32_t node, uint32_t index,
                        uint32_t *base, uint32_t *size)
{
    return find(t, property, value, node, index, base, size, 0);
}

fdt_status fdt_cell(const fdt *t, const char *property, const char *value, const char *name, uint32_t index,
                    uint32_t *cell)
{
    return find(t, property, value, 0, index, cell, 0, name);
}

/* Only the root's own properties: they end at its first child's BEGIN_NODE or its END_NODE. */
fdt_status fdt_root_string(const fdt *t, const char *property, const char **value)
{
    uint32_t at = t->structs;
    if (at + 4 > t->structs_end || be32(t->blob + at) != FDT_BEGIN_NODE) {
        return FDT_BAD_LAYOUT;
    }
    at += 4;
    skip_name(t, &at);
    for (;;) {
        if (at + 4 > t->structs_end) {
            return FDT_BAD_LAYOUT;
        }
        uint32_t token = be32(t->blob + at);
        at += 4;
        if (token == FDT_PROP) {
            span v;
            const char *key;
            if (read_prop(t, &at, &v, &key) != FDT_OK) {
                return FDT_BAD_LAYOUT;
            }
            if (fdt_same(key, property)) {
                if (v.length == 0 || t->blob[v.offset + v.length - 1] != 0) {
                    return FDT_BAD_LAYOUT; /* not a string */
                }
                *value = (const char *)t->blob + v.offset;
                return FDT_OK;
            }
        } else if (token == FDT_BEGIN_NODE || token == FDT_END_NODE) {
            return FDT_NOT_FOUND;
        } else if (token != FDT_NOP) {
            return FDT_BAD_LAYOUT;
        }
    }
}
