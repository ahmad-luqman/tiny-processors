/* hello: the smallest program (Track 2, O2): its pid and its arguments. */
#include "ulib.h"

int main(const char *args)
{
    u_puts("hello from pid ");
    u_putdec(sys_getpid());
    if (*args) {
        u_puts(", args: ");
        u_puts(args);
    }
    u_puts("\n");
    return 0;
}
