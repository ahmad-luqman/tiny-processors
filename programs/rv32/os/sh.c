/* sh: the shell (Track 2, O2). It reads lines from the console and echoes
 * them, since the host does not, then runs them:
 *
 *   ls              the programs on the RAM disk
 *   NAME [ARGS]     run a program and wait for it; a nonzero exit is reported
 *   NAME [ARGS] &   run it in the background (O4) and print its pid
 *   wait            wait for every background program
 *   halt [CODE]     stop the machine; 0 passes
 *
 * A line longer than LINE - 1 bytes, or a halt code that is not a number, is
 * refused with a message rather than cut or read as 0.
 *
 * Enter is \n, \r or \r\n. Backspace (DEL or ^H) removes a character, ^U the
 * line, and a tab is a space. Escape sequences (arrow keys), other control
 * bytes and non-ASCII bytes are dropped, so a character is one byte and one
 * column. Until a \r arrives, as in the scripted sessions, each line is echoed
 * whole once read. A terminal sends \r for Enter (issue #30), so from the line
 * after the first \r the shell echoes each key as it comes and rings the bell
 * for a key it drops or refuses. That echo can interleave with a background
 * job's output, which is why the scripted sessions, whose files .gitattributes
 * pins to \n endings, keep the whole-line echo. (There is no `sh -i`: the
 * kernel runs one instance of a program at a time, and pid 1 is the shell.)
 *
 * The shell is pid 1: when it exits, the kernel halts with its code. */
#include "ulib.h"

#define LINE 80
#define BACKGROUND 4
#define CTRL_H 0x08
#define CTRL_U 0x15
#define ESC 0x1b
#define DEL 0x7f

enum escape { TEXT, AFTER_ESC, SEQUENCE }; /* SEQUENCE: after ESC [ or ESC O, to a final byte */

static char line[LINE];
static uint32_t background[BACKGROUND];
static int interactive; /* a \r has been read: echo each key, from the next line on */
static char previous;   /* the last byte read, only to take \r\n as one Enter */

/* Rub out the last `count` characters on the terminal. */
static void rub_out(uint32_t count)
{
    while (count--) {
        u_puts("\b \b");
    }
}

/* One line into `line`, echoed with its newline; returns 0 when it was too long.
 * A byte past LINE - 1 is dropped and the line is refused at Enter however it is
 * edited after, unless ^U clears it: a line that lost a byte is never run. */
static int read_line(void)
{
    int echo = interactive;
    enum escape escape = TEXT;
    uint32_t n = 0;
    int fits = 1;
    for (;;) {
        char c;
        uint32_t got = sys_read(0, &c, 1);
        if (got == SYS_ERROR) { /* the console is fd 0; this would be a kernel bug, not input */
            u_puts("sh: cannot read the console\n");
            sys_halt(1);
            for (;;) {
            }
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
            if (c == '\r') {
                interactive = 1;
            }
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
        if (c == DEL || c == CTRL_H) {
            if (n) {
                n--;
                if (echo) {
                    rub_out(1);
                }
            }
        } else if (c == CTRL_U) {
            if (echo) {
                rub_out(n);
            }
            n = 0;
            fits = 1;
        } else if (c == ESC) {
            escape = AFTER_ESC;
        } else if ((uint8_t)c < 0x20 || (uint8_t)c > 0x7e) { /* dropped */
            if (echo) {
                u_putc('\a');
            }
        } else if (n + 1 < LINE) {
            line[n++] = c;
            if (echo) {
                u_putc(c);
            }
        } else { /* lost: the line will be refused */
            fits = 0;
            if (echo) {
                u_putc('\a');
            }
        }
    }
    line[n] = 0;
    if (!echo) {
        u_puts(line);
    }
    u_puts("\n");
    return fits;
}

static void report(const char *name, uint32_t pid, uint32_t code)
{
    if (code) {
        u_puts("sh: ");
        u_puts(name);
        if (pid) {
            u_puts(" [");
            u_putdec(pid);
            u_puts("]");
        }
        u_puts(" exited ");
        u_putdec(code);
        u_puts("\n");
    }
}

static void wait_all(void)
{
    for (uint32_t i = 0; i < BACKGROUND; i++) {
        if (background[i]) {
            report("job", background[i], sys_wait(background[i]));
            background[i] = 0;
        }
    }
}

static void list(void)
{
    char name[24];
    for (uint32_t i = 0; sys_list(i, name, sizeof name) != SYS_ERROR; i++) {
        u_puts(name);
        u_puts("\n");
    }
}

static void run(char *command, char *args, int in_background)
{
    uint32_t pid = sys_spawn(command, args);
    if (pid == SYS_ERROR) {
        u_puts("sh: ");
        u_puts(command);
        u_puts(": cannot run\n");
        return;
    }
    if (!in_background) {
        report(command, 0, sys_wait(pid));
        return;
    }
    for (uint32_t i = 0; i < BACKGROUND; i++) {
        if (!background[i]) {
            background[i] = pid;
            u_puts("[");
            u_putdec(pid);
            u_puts("]\n");
            return;
        }
    }
    u_puts("sh: too many jobs, waiting\n");
    report(command, pid, sys_wait(pid));
}

int main(void)
{
    u_puts("sh: ls, NAME [ARGS] [&], wait, halt [CODE]\n");
    for (;;) {
        u_puts("$ ");
        int fits = read_line();
        if (!fits) {
            u_puts("sh: line too long\n");
            continue;
        }
        char *command = line;
        while (*command == ' ') {
            command++;
        }
        uint32_t n = u_strlen(command);
        int in_background = 0;
        while (n && command[n - 1] == ' ') {
            command[--n] = 0;
        }
        if (n && command[n - 1] == '&') {
            in_background = 1;
            command[--n] = 0;
            while (n && command[n - 1] == ' ') {
                command[--n] = 0;
            }
        }
        if (!n) {
            continue;
        }
        char *args = command;
        while (*args && *args != ' ') {
            args++;
        }
        if (*args) {
            *args++ = 0;
            while (*args == ' ') {
                args++;
            }
        }
        if (!u_strcmp(command, "ls")) {
            list();
        } else if (!u_strcmp(command, "wait")) {
            wait_all();
        } else if (!u_strcmp(command, "halt")) {
            const char *end;
            uint32_t code = u_parse(args, &end);
            if (*end) {
                u_puts("sh: halt: CODE is a number\n");
                continue;
            }
            wait_all();
            sys_halt(code);
        } else {
            run(command, args, in_background);
        }
    }
}
