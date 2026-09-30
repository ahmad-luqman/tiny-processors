/* Host harness for programs/rv32/fdt.c (Track 1): the firmware's device-tree
 * reader compiled natively, so tests/test_rv32_platform.py can run it on our
 * blob, QEMU's and malformed ones under the address sanitizer.
 *
 *   rv32_fdt_native FILE model                       -> the root's model, or "error N"
 *   rv32_fdt_native FILE PROPERTY VALUE INDEX        -> "BASE SIZE" in hex, or "error N"
 *   rv32_fdt_native FILE PROPERTY VALUE NAME INDEX   -> fdt_cell's cell in hex, or "error N" (O1)
 *   rv32_fdt_native FILE PROPERTY VALUE nth NODE INDEX -> fdt_find_nth's "BASE SIZE" (O3)
 *
 * The file is copied into a heap buffer of exactly its size (a file shorter
 * than the 40-byte header is refused before the reader sees it), so a read
 * past the blob is a sanitizer error rather than a lucky zero. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "fdt.h"

int main(int argc, char **argv)
{
    if (argc != 3 && argc != 5 && argc != 6 && argc != 7) {
        fprintf(stderr, "usage: %s FILE model | FILE PROPERTY VALUE INDEX | FILE PROPERTY VALUE NAME INDEX\n", argv[0]);
        return 2;
    }
    FILE *in = fopen(argv[1], "rb");
    if (!in) {
        perror(argv[1]);
        return 2;
    }
    long length = -1;
    if (fseek(in, 0, SEEK_END) == 0) {
        length = ftell(in);
    }
    if (length < 0 || fseek(in, 0, SEEK_SET) != 0) {
        perror(argv[1]);
        return 2;
    }
    if (length < 40) {
        fclose(in);
        printf("error %d\n", FDT_BAD_LAYOUT); /* the firmware trusts a1; the harness knows the file */
        return 0;
    }
    uint8_t *blob = malloc((size_t)length);
    if (!blob || fread(blob, 1, (size_t)length, in) != (size_t)length) {
        fprintf(stderr, "%s: cannot read\n", argv[1]);
        return 2;
    }
    fclose(in);
    fdt t;
    fdt_status status = fdt_open(&t, (uintptr_t)blob);
    if (status == FDT_OK && t.total > (uint32_t)length) {
        status = FDT_BAD_LAYOUT; /* the firmware trusts totalsize; the harness knows the file's */
    }
    uint32_t base = 0, size = 0;
    const char *model = 0;
    if (status == FDT_OK && argc == 3) {
        status = fdt_root_string(&t, "model", &model);
    } else if (status == FDT_OK && argc == 7) {
        status = fdt_find_nth(&t, argv[2], argv[3], (uint32_t)strtoul(argv[5], 0, 10), (uint32_t)strtoul(argv[6], 0, 10),
                              &base, &size);
    } else if (status == FDT_OK && argc == 6) {
        status = fdt_cell(&t, argv[2], argv[3], argv[4], (uint32_t)strtoul(argv[5], 0, 10), &base);
    } else if (status == FDT_OK) {
        status = fdt_find(&t, argv[2], argv[3], (uint32_t)strtoul(argv[4], 0, 10), &base, &size);
    }
    if (status != FDT_OK) {
        printf("error %d\n", status);
    } else if (model) {
        printf("%s\n", model);
    } else if (argc == 6) {
        printf("%08x\n", base);
    } else {
        printf("%08x %08x\n", base, size);
    }
    free(blob);
    return 0;
}
