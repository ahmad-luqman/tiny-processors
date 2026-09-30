/* write NAME TEXT: make NAME hold TEXT and a newline, creating it if needed (Track 2, O3). */
#include "ulib.h"

int main(const char *args)
{
    char name[20];
    uint32_t n = 0;
    while (args[n] && args[n] != ' ' && n + 1 < sizeof name) {
        name[n] = args[n];
        n++;
    }
    name[n] = 0;
    const char *text = args + n;
    while (*text == ' ') {
        text++;
    }
    uint32_t fd = n ? sys_open(name, O_WRITE | O_CREATE) : SYS_ERROR;
    if (fd == SYS_ERROR) {
        u_puts("write: cannot write ");
        u_puts(name);
        u_puts("\n");
        return 1;
    }
    uint32_t length = u_strlen(text);
    int ok = sys_write(fd, text, length) == length && sys_write(fd, "\n", 1) == 1;
    ok = sys_close(fd) == 0 && ok;
    return ok ? 0 : 2;
}
