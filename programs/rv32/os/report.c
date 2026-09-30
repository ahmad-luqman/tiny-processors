/* Reports to files; see report.h. */
#include "report.h"

#include "ulib.h"

static char line[96];
static uint32_t length;

static void add(const char *s)
{
    while (*s && length + 1 < sizeof line) {
        line[length++] = *s++;
    }
}

int report(const char *name, const char *what, uint32_t sum)
{
    char hex[9], file[20];
    for (int i = 7; i >= 0; i--) {
        uint32_t digit = (sum >> (4 * (7 - i))) & 15u;
        hex[i] = (char)(digit < 10 ? '0' + digit : 'a' + digit - 10);
    }
    hex[8] = 0;
    length = 0;
    add(name);
    add(": ");
    add(what);
    add(", sum ");
    add(hex);
    add(sys_switches() ? ", preempted yes\n" : ", preempted no\n");
    uint32_t n = 0;
    for (; name[n] && n + 5 < sizeof file; n++) {
        file[n] = name[n];
    }
    file[n] = '.';
    file[n + 1] = 'o';
    file[n + 2] = 'u';
    file[n + 3] = 't';
    file[n + 4] = 0;
    uint32_t fd = sys_open(file, O_WRITE);
    if (fd == SYS_ERROR) {
        (void)sys_write(1, line, length); /* no disk: the console instead */
        return 0;
    }
    int ok = sys_write(fd, line, length) == length;
    ok = sys_close(fd) == 0 && ok;
    if (!ok) {
        u_puts(name);
        u_puts(": cannot write ");
        u_puts(file);
        u_puts("\n");
    }
    return ok ? 0 : 2;
}
