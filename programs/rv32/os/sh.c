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
 * The shell is pid 1: when it exits, the kernel halts with its code. */
#include "ulib.h"

#define LINE 80
#define BACKGROUND 4

static char line[LINE];
static uint32_t background[BACKGROUND];

/* One line into `line`; returns 0 when it was too long (its end is read and dropped). */
static int read_line(void)
{
    uint32_t n = 0;
    int fits = 1;
    for (;;) {
        char c;
        if (sys_read(0, &c, 1) != 1 || c == '\r') {
            continue;
        }
        if (c == '\n') {
            break;
        }
        if (n + 1 < LINE) {
            line[n++] = c;
        } else {
            fits = 0;
        }
    }
    line[n] = 0;
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
        u_puts(line);
        u_puts("\n");
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
