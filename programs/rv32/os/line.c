/* The console line editor; see line.h. */
#include "line.h"

#include "ulib.h"

#define CTRL_D 0x04
#define CTRL_H 0x08
#define CTRL_U 0x15
#define ESC 0x1b
#define DEL 0x7f

enum escape { TEXT, AFTER_ESC, SEQUENCE }; /* SEQUENCE: after ESC [ or ESC O, to a final byte */

static char previous; /* the last byte read, only to take \r\n as one Enter */

static void echo(const char *s, uint32_t n)
{
    (void)sys_write(1, s, n);
}

static void rub_out(uint32_t count)
{
    while (count--) {
        echo("\b \b", 3);
    }
}

enum line_result line_edit(char *line, uint32_t room, uint32_t *length, uint32_t flags)
{
    enum escape escape = TEXT;
    uint32_t n = 0;
    int fits = 1;
    for (;;) {
        char c;
        uint32_t got = sys_read(0, &c, 1);
        if (got == SYS_ERROR) {
            return LINE_ERROR;
        }
        if (got != 1) {
            continue;
        }
        char before = previous;
        previous = c;
        if (c == '\n' && before == '\r') {
            continue;
        }
        if (c == '\r' || c == '\n') { /* Enter, even inside a cut-off escape sequence */
            break;
        }
        if (escape == AFTER_ESC) {
            escape = c == '[' || c == 'O' ? SEQUENCE : c == ESC ? AFTER_ESC : TEXT;
            continue;
        }
        if (escape == SEQUENCE) {
            if (c >= 0x40 && c <= 0x7e && c != '[') { /* ESC [ [ A is a Linux console F1 */
                escape = TEXT;
            }
            continue;
        }
        if (c == '\t') {
            c = ' ';
        }
        if (c == CTRL_D && (flags & LINE_EOF) && n == 0 && fits) {
            return LINE_END;
        }
        if (c == DEL || c == CTRL_H) {
            if (n) {
                n--;
                rub_out(1);
            }
        } else if (c == CTRL_U) {
            rub_out(n);
            n = 0;
            fits = 1;
        } else if (c == ESC) {
            escape = AFTER_ESC;
        } else if ((uint8_t)c < 0x20 || (uint8_t)c > 0x7e) { /* dropped */
            echo("\a", 1);
        } else if (n < room) {
            line[n++] = c;
            echo(&c, 1);
        } else { /* lost: the line will be refused */
            fits = 0;
            echo("\a", 1);
        }
    }
    echo("\n", 1);
    *length = fits ? n : 0;
    return fits ? LINE_OK : LINE_TOO_LONG;
}
