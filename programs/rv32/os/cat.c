/* cat NAME: print a file from the disk (Track 2, O3). */
#include "ulib.h"

int main(const char *name)
{
    uint32_t fd = sys_open(name, O_READ);
    if (fd == SYS_ERROR) {
        u_puts("cat: ");
        u_puts(name);
        u_puts(": no such file\n");
        return 1;
    }
    char buffer[64];
    for (uint32_t n = sys_read(fd, buffer, sizeof buffer); n && n != SYS_ERROR; n = sys_read(fd, buffer, sizeof buffer)) {
        (void)sys_write(1, buffer, n);
    }
    sys_close(fd);
    return 0;
}
