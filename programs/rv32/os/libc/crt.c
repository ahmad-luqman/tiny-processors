/* C startup for programs built with the C library (Track 3, L1; docs/rv32-libc.md).
 *
 * The kernel hands a program one argument string (what follows its name on
 * the shell's line, at most 63 bytes). It is split here at spaces; a part in
 * double quotes keeps its spaces ("print(1 + 2)"), and there are no escapes.
 * argv[0] is the program's name, which the kernel does not pass, so it is
 * compiled in: this file is built once per program with LIBC_PROGRAM set. */
#include <stdlib.h>

#ifndef LIBC_PROGRAM
#error "build crt.c with -DLIBC_PROGRAM='\"name\"'"
#endif

#define MAX_ARGS 16

int main(int argc, char **argv);
void __libc_init_array(void);
_Noreturn void __libc_start(char *args);

static char *argv[MAX_ARGS + 1];

_Noreturn void __libc_start(char *args)
{
    int argc = 0;
    argv[argc++] = LIBC_PROGRAM;
    char *s = args;
    while (argc < MAX_ARGS) {
        while (*s == ' ') {
            s++;
        }
        if (!*s) {
            break;
        }
        char end = ' ';
        if (*s == '"') {
            end = '"';
            s++;
        }
        argv[argc++] = s;
        while (*s && *s != end) {
            s++;
        }
        if (*s) {
            *s++ = 0;
        }
    }
    argv[argc] = 0;
    __libc_init_array(); /* constructors; picolibc's exit runs the destructors (stdout's flush) */
    exit(main(argc, argv));
}
