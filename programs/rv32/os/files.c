/* files: the files on the disk and their sizes (Track 2, O3). */
#include "ulib.h"

int main(void)
{
    char name[20];
    uint32_t count = 0;
    for (uint32_t size = sys_files(0, name, sizeof name); size != SYS_ERROR; size = sys_files(++count, name, sizeof name)) {
        u_puts(name);
        u_puts(" ");
        u_putdec(size);
        u_puts("\n");
    }
    if (!count) {
        u_puts("files: no disk, or no files\n");
    }
    return 0;
}
