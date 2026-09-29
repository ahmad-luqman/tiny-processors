/* Read-only FDT reader; see fdt.h. Offsets are checked against the blocks
 * before every read, so a malformed tree ends a query with FDT_BAD_LAYOUT
 * rather than a read outside the blob. */
#include "fdt.h"

#define FDT_MAGIC 0xd00dfeedu
#define FDT_BEGIN_NODE 1u
#define FDT_END_NODE 2u
#define FDT_PROP 3u
#define FDT_NOP 4u
#define FDT_END 9u
#define FDT_MAX_DEPTH 8

static uint32_t be32(const uint8_t *p)
{
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 | (uint32_t)p[2] << 8 | p[3];
}

static int same(const char *a, const char *b)
{
    while (*a && *a == *b) {
        a++;
        b++;
    }
    return *a == *b;
}

int fdt_open(fdt *t, uintptr_t address)
{
    const uint8_t *h = (const uint8_t *)address;
    if (be32(h) != FDT_MAGIC) {
        return FDT_BAD_MAGIC;
    }
    /* version 17 is current; a blob is readable by us if it is at least 16 compatible */
    if (be32(h + 20) < 16 || be32(h + 24) > 17) {
        return FDT_BAD_VERSION;
    }
    t->blob = h;
    t->total = be32(h + 4);
    t->structs = be32(h + 8);
    t->strings = be32(h + 12);
    t->structs_end = t->structs + be32(h + 36);
    t->strings_end = t->strings + be32(h + 32);
    if (t->total < 40 || t->structs < 40 || t->structs_end < t->structs || t->structs_end > t->total ||
        t->strings_end < t->strings || t->strings_end > t->total || (t->structs & 3u)) {
        return FDT_BAD_LAYOUT;
    }
    return FDT_OK;
}

/* A property's value, as found while walking. */
typedef struct {
    uint32_t offset, length;
} span;

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
        if (same(s, want)) {
            return 1;
        }
        if (!list) {
            return 0;
        }
        at += n + 1;
    }
    return 0;
}

/* Read a `cells`-wide number (0, 1 or 2 cells) that must fit in 32 bits; zero cells is 0, as a
 * cpu's size under #size-cells = <0>. */
static int number(const fdt *t, uint32_t at, uint32_t cells, uint32_t *out)
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
    } else if (cells != 1) {
        return FDT_TOO_WIDE;
    }
    *out = be32(t->blob + at);
    return FDT_OK;
}

/* Walk the tree. With `property` set, stop at the first node that matches and decode reg entry
 * `index`; with `property` 0, look `root_property` up on the root node instead. */
static int walk(const fdt *t, const char *property, const char *value, uint32_t index,
                uint32_t *base, uint32_t *size, const char *root_property, const char **root_value)
{
    uint32_t address_cells[FDT_MAX_DEPTH], size_cells[FDT_MAX_DEPTH];
    int depth = -1;
    int matched = 0, in_props = 0;
    span reg = {0, 0};
    uint32_t at = t->structs;
    int list = property && same(property, "compatible");

    for (;;) {
        if (at + 4 > t->structs_end) {
            return FDT_BAD_LAYOUT;
        }
        uint32_t token = be32(t->blob + at);
        at += 4;
        if (token == FDT_BEGIN_NODE || token == FDT_END_NODE || token == FDT_END) {
            /* The properties of the current node end here: decide on it. */
            if (in_props && matched) {
                if (depth < 1) {
                    return FDT_NOT_FOUND; /* the root has no parent to size its reg */
                }
                uint32_t ac = address_cells[depth - 1], sc = size_cells[depth - 1];
                uint32_t entry = 4u * (ac + sc);
                if (entry == 0 || reg.length < entry * (index + 1)) {
                    return FDT_NOT_FOUND;
                }
                uint32_t e = reg.offset + entry * index;
                int status = number(t, e, ac, base);
                return status ? status : number(t, e + 4u * ac, sc, size);
            }
            in_props = 0;
        }
        if (token == FDT_BEGIN_NODE) {
            while (at < t->structs_end && t->blob[at]) {
                at++;
            }
            at = (at + 4u) & ~3u; /* past the NUL, to the next word */
            if (++depth >= FDT_MAX_DEPTH) {
                return FDT_BAD_LAYOUT;
            }
            address_cells[depth] = 2; /* the specification's defaults for the children */
            size_cells[depth] = 1;
            matched = 0;
            in_props = 1;
            reg.length = 0;
        } else if (token == FDT_END_NODE) {
            if (depth-- < 0) {
                return FDT_BAD_LAYOUT;
            }
        } else if (token == FDT_PROP) {
            if (at + 8 > t->structs_end || depth < 0) {
                return FDT_BAD_LAYOUT;
            }
            span v = {at + 8, be32(t->blob + at)};
            uint32_t name = t->strings + be32(t->blob + at + 4);
            if (v.offset + v.length > t->structs_end || v.offset + v.length < v.offset || name >= t->strings_end) {
                return FDT_BAD_LAYOUT;
            }
            at = (v.offset + v.length + 3u) & ~3u;
            const char *key = (const char *)t->blob + name;
            if (same(key, "#address-cells") && v.length == 4) {
                address_cells[depth] = be32(t->blob + v.offset);
            } else if (same(key, "#size-cells") && v.length == 4) {
                size_cells[depth] = be32(t->blob + v.offset);
            } else if (same(key, "reg")) {
                reg = v;
            }
            if (property && same(key, property) && matches(t, v, list, value)) {
                matched = 1;
            }
            if (!property && depth == 0 && same(key, root_property)) {
                if (v.length == 0 || t->blob[v.offset + v.length - 1] != 0) {
                    return FDT_BAD_LAYOUT;
                }
                *root_value = (const char *)t->blob + v.offset;
                return FDT_OK;
            }
        } else if (token == FDT_END) {
            return depth == -1 ? FDT_NOT_FOUND : FDT_BAD_LAYOUT;
        } else if (token != FDT_NOP) {
            return FDT_BAD_LAYOUT;
        }
    }
}

int fdt_find(const fdt *t, const char *property, const char *value, uint32_t index,
             uint32_t *base, uint32_t *size)
{
    return walk(t, property, value, index, base, size, 0, 0);
}

const char *fdt_root_string(const fdt *t, const char *property)
{
    const char *value = 0;
    uint32_t unused;
    return walk(t, 0, 0, 0, &unused, &unused, property, &value) == FDT_OK ? value : 0;
}
