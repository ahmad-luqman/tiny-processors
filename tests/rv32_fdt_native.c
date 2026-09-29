/* Host harness for programs/rv32/fdt.c (Track 1): the firmware's device-tree
 * reader compiled natively, so tests/test_rv32_platform.py can run it on our
 * blob, QEMU's and malformed ones under the address sanitizer.
 *
 *   rv32_fdt_native FILE model                       -> the root's model, or "error N"
 *   rv32_fdt_native FILE PROPERTY VALUE INDEX        -> "BASE SIZE" in hex, or "error N"
 *
 * The file is copied into a buffer of exactly its size, so a read past the
 * blob is a sanitizer error rather than a lucky zero. */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>

#include "fdt.h"

int main(int argc, char **argv)
{
    if (argc != 3 && argc != 5) {
        fprintf(stderr, "usage: %s FILE model | FILE PROPERTY VALUE INDEX\n", argv[0]);
        return 2;
    }
    FILE *in = fopen(argv[1], "rb");
    if (!in) {
        perror(argv[1]);
        return 2;
    }
    fseek(in, 0, SEEK_END);
    long length = ftell(in);
    rewind(in);
    uint8_t *blob = malloc(length < 40 ? 40 : (size_t)length);
    if (!blob || fread(blob, 1, (size_t)length, in) != (size_t)length) {
        return 2;
    }
    fclose(in);
    fdt t;
    int status = fdt_open(&t, (uintptr_t)blob);
    if (status == FDT_OK && t.total > (uint32_t)length) {
        status = FDT_BAD_LAYOUT; /* the firmware trusts totalsize; the harness knows the file's */
    }
    uint32_t base = 0, size = 0;
    const char *model = 0;
    if (status == FDT_OK && argc == 3) {
        model = fdt_root_string(&t, "model");
        status = model ? FDT_OK : FDT_NOT_FOUND;
    } else if (status == FDT_OK) {
        status = fdt_find(&t, argv[2], argv[3], (uint32_t)strtoul(argv[4], 0, 10), &base, &size);
    }
    if (status != FDT_OK) {
        printf("error %d\n", status);
    } else if (model) {
        printf("%s\n", model);
    } else {
        printf("%08x %08x\n", base, size);
    }
    free(blob);
    return 0;
}
