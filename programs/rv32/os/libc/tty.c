/* The console's line discipline (Track 3, L1; docs/rv32-libc.md). The host
 * does not echo (issue #30), so a program reading fd 0 through the C library
 * gets what the shell gets: each key echoed and edited as line.h describes,
 * and the line handed over, with its \n, at Enter. ^D at the start of a line
 * is the end of input (read returns 0).
 *
 * A key past the line's LINE - 2 characters rings the bell and is dropped,
 * and at Enter the line is refused with a message (the program reads an empty
 * line), so a cut line never reaches it, as in the shell. */
#include <errno.h>
#include <stdint.h>
#include <sys/types.h>

#include "line.h"
#include "ulib.h"

#define LINE 256
#define TOO_LONG "libc: line too long, dropped\n"

static char line[LINE];
static uint32_t length, taken; /* the line's bytes, and how many the program has had */

ssize_t __tty_read(void *buf, size_t len)
{
    if (len == 0) {
        return 0;
    }
    if (taken == length) {
        uint32_t n;
        switch (line_edit(line, LINE - 2, &n, LINE_EOF)) {
        case LINE_END:
            return 0;
        case LINE_ERROR: /* the console is fd 0; this would be a kernel bug, not input */
            errno = EIO;
            return -1;
        case LINE_TOO_LONG:
            (void)sys_write(1, TOO_LONG, sizeof TOO_LONG - 1);
            break;
        case LINE_OK:
            break;
        }
        line[n++] = '\n';
        length = n;
        taken = 0;
    }
    size_t n = 0;
    while (n < len && taken < length) {
        ((char *)buf)[n++] = line[taken++];
    }
    return (ssize_t)n;
}
