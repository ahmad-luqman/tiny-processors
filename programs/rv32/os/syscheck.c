/* syscheck: the system calls' edge cases (Track 2, O2). Every call that must
 * fail returns SYS_ERROR, every call that must succeed returns what sys.h
 * promises; one line per group, then `syscheck: ok` or the failures. */
#include "ulib.h"

static uint32_t failures;

static void check(const char *what, uint32_t got, uint32_t want)
{
    if (got != want) {
        u_puts("syscheck: FAILED ");
        u_puts(what);
        u_puts(" got ");
        u_puthex(got);
        u_puts(" want ");
        u_puthex(want);
        u_puts("\n");
        failures++;
    }
}

int main(void)
{
    char buffer[8];
    /* Pointers and descriptors. */
    check("write from kernel memory", sys_write(1, (const void *)0x80000000u, 4), SYS_ERROR);
    check("write past the slot", sys_write(1, buffer, 0x40000u), SYS_ERROR);
    check("write to fd 3", sys_write(3, "x", 1), SYS_ERROR);
    check("read from fd 1", sys_read(1, buffer, 1), SYS_ERROR);
    check("read nothing", sys_read(0, buffer, 0), SYS_ERROR);
    check("unknown call", syscall3(999, 0, 0, 0), SYS_ERROR);
    u_puts("syscheck: pointers\n");
    /* The heap: sbrk moves the break and stops below the stack. */
    uint32_t start = (uint32_t)(uintptr_t)sys_sbrk(0);
    check("sbrk 16", (uint32_t)(uintptr_t)sys_sbrk(16), start);
    check("sbrk 0 after", (uint32_t)(uintptr_t)sys_sbrk(0), start + 16);
    check("sbrk into the stack", (uint32_t)(uintptr_t)sys_sbrk(0x40000u), SYS_ERROR);
    check("break unchanged", (uint32_t)(uintptr_t)sys_sbrk(0), start + 16);
    u_puts("syscheck: sbrk\n");
    /* Processes. */
    check("wait for a stranger", sys_wait(12345), SYS_ERROR);
    check("spawn myself", sys_spawn("syscheck", ""), SYS_ERROR); /* my slot is in use */
    check("spawn nothing", sys_spawn("nosuch", ""), SYS_ERROR);
    check("spawn a kernel pointer", sys_spawn((const char *)0x80000000u, ""), SYS_ERROR);
    uint32_t child = sys_spawn("hello", "from syscheck");
    check("wait for hello", sys_wait(child), 0);
    check("wait twice", sys_wait(child), SYS_ERROR);
    check("pid", sys_getpid() > 1, 1);
    u_puts("syscheck: processes\n");
    /* The RAM disk. */
    check("list 0", sys_list(0, buffer, sizeof buffer), 2); /* "sh" */
    check("list into too little", sys_list(0, buffer, 2), SYS_ERROR);
    check("list past the end", sys_list(99, buffer, sizeof buffer), SYS_ERROR);
    u_puts("syscheck: list\n");
    /* Time. */
    uint32_t before = sys_time();
    sys_sleep(100);
    check("slept", sys_time() - before >= 100, 1);
    sys_yield();
    check("no event", sys_event(), 0);
    u_puts("syscheck: time\n");
    u_puts(failures ? "syscheck: FAILED\n" : "syscheck: ok\n");
    return failures ? 1 : 0;
}
