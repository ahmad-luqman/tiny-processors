/* The console's line discipline (Track 3, L1; docs/rv32-libc.md). The host
 * does not echo (issue #30), so a program reading fd 0 through the C library
 * gets what the shell gets: each key echoed as it is typed, and the line
 * handed over at Enter. The editing rules are sh.c's:
 *
 *   Enter            \n, \r or \r\n; the line goes to the program with \n
 *   Backspace, ^H    removes the last character
 *   ^U               clears the line
 *   Tab              a space
 *   ^D               at the start of a line, the end of input (read returns 0)
 *   escape sequences dropped (arrow and function keys)
 *   other control
 *   and non-ASCII    ring the bell and are dropped
 *
 * A key past the line's LINE - 2 characters rings the bell and is dropped,
 * and at Enter the line is refused with a message (the program reads an empty
 * line), so a cut line never reaches it, as in the shell. */
#include <stdint.h>
#include <sys/types.h>

#include "ulib.h"

#define LINE 256
#define CTRL_D 0x04
#define CTRL_H 0x08
#define CTRL_U 0x15
#define ESC 0x1b
#define DEL 0x7f

enum escape { TEXT, AFTER_ESC, SEQUENCE };

static char line[LINE];
static uint32_t length, taken; /* the line's bytes, and how many the program has had */
static char previous;          /* the last byte read, to take \r\n as one Enter */

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

/* A line into `line`, ending in \n; 0 at the end of input. */
static uint32_t edit_line(void)
{
    enum escape escape = TEXT;
    uint32_t n = 0;
    int fits = 1;
    for (;;) {
        char c;
        uint32_t got = sys_read(0, &c, 1);
        if (got == SYS_ERROR) { /* the console is fd 0; this would be a kernel bug, not input */
            return 0;
        }
        if (got != 1) {
            continue;
        }
        char before = previous;
        previous = c;
        if (c == '\n' && before == '\r') {
            continue;
        }
        if (c == '\r' || c == '\n') {
            break;
        }
        if (escape == AFTER_ESC) {
            escape = c == '[' || c == 'O' ? SEQUENCE : c == ESC ? AFTER_ESC : TEXT;
            continue;
        }
        if (escape == SEQUENCE) {
            if (c >= 0x40 && c <= 0x7e && c != '[') {
                escape = TEXT;
            }
            continue;
        }
        if (c == '\t') {
            c = ' ';
        }
        if (c == CTRL_D && n == 0 && fits) {
            return 0;
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
        } else if ((uint8_t)c < 0x20 || (uint8_t)c > 0x7e) {
            echo("\a", 1);
        } else if (n + 2 < LINE) {
            line[n++] = c;
            echo(&c, 1);
        } else {
            fits = 0;
            echo("\a", 1);
        }
    }
    echo("\n", 1);
    if (!fits) {
        echo("libc: line too long, dropped\n", 29);
        n = 0;
    }
    line[n++] = '\n';
    return n;
}

ssize_t __tty_read(void *buf, size_t len)
{
    if (len == 0) {
        return 0;
    }
    if (taken == length) {
        length = edit_line();
        taken = 0;
        if (length == 0) {
            return 0;
        }
    }
    size_t n = 0;
    while (n < len && taken < length) {
        ((char *)buf)[n++] = line[taken++];
    }
    return (ssize_t)n;
}
