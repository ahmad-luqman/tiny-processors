/* syscheck: the system calls' edge cases (Track 2, O2). Every call that must
 * fail returns SYS_ERROR, every call that must succeed returns what sys.h
 * promises; one line per group, then `syscheck: ok` or the failures. */
#include "ulib.h"

extern char _stack_top[], _stack_bottom[]; /* the end of my slots, and my stack's guard page (user.ld) */

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
    /* Calls that write into my memory refuse a buffer outside it: the kernel's, one that runs
     * past the end of my slots, and one whose end wraps around the address space. */
    uint32_t top = (uint32_t)(uintptr_t)_stack_top, kernel = 0x80000000u, wraps = 0xfffffff0u;
    check("read into kernel memory", sys_read(0, (void *)(uintptr_t)kernel, 4), SYS_ERROR);
    check("read across my end", sys_read(0, (void *)(uintptr_t)(top - 4), 8), SYS_ERROR);
    check("read wrapping around", sys_read(0, (void *)(uintptr_t)wraps, 0x20), SYS_ERROR);
    check("write across my end", sys_write(1, (const void *)(uintptr_t)(top - 4), 8), SYS_ERROR);
    check("list into kernel memory", sys_list(0, (char *)(uintptr_t)kernel, 8), SYS_ERROR);
    check("list across my end", sys_list(0, (char *)(uintptr_t)(top - 4), 8), SYS_ERROR);
    check("ps into kernel memory", sys_ps(0, (char *)(uintptr_t)kernel, 48), SYS_ERROR);
    check("ps across my end", sys_ps(0, (char *)(uintptr_t)(top - 4), 48), SYS_ERROR);
    check("ps wrapping around", sys_ps(0, (char *)(uintptr_t)wraps, 48), SYS_ERROR);
    check("files into kernel memory", sys_files(0, (char *)(uintptr_t)kernel, 20), SYS_ERROR);
    check("files across my end", sys_files(0, (char *)(uintptr_t)(top - 4), 20), SYS_ERROR);
    check("open a kernel name", sys_open((const char *)(uintptr_t)kernel, O_READ), SYS_ERROR);
    /* Track 3: the guard page below my stack is in my slots but never mine to use, through a
     * system call either. */
    uint32_t guard = (uint32_t)(uintptr_t)_stack_bottom;
    check("write from the guard page", sys_write(1, (const void *)(uintptr_t)guard, 4), SYS_ERROR);
    check("read into the guard page", sys_read(0, (void *)(uintptr_t)(guard + 0xffcu), 4), SYS_ERROR);
    check("write across into the guard", sys_write(1, (const void *)(uintptr_t)(guard - 2), 4), SYS_ERROR);
    check("write across out of the guard", sys_write(1, (const void *)(uintptr_t)(guard + 0xffeu), 4), SYS_ERROR);
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
    /* Seeking (Track 3): within 0 and the file's size, from the start, the current position or
     * the end, on an open file only. */
    char name[20];
    uint32_t size = SYS_ERROR;
    for (uint32_t i = 0; size == SYS_ERROR && sys_files(i, name, sizeof name) != SYS_ERROR; i++) {
        size = u_strcmp(name, "welcome") ? SYS_ERROR : sys_files(i, name, sizeof name);
    }
    uint32_t fd = sys_open("welcome", O_READ);
    check("seek on the console", sys_seek(0, 0, SEEK_FROM_START), SYS_ERROR);
    check("seek on a closed file", sys_seek(fd + 1, 0, SEEK_FROM_START), SYS_ERROR);
    check("seek from nowhere", sys_seek(fd, 0, 3), SYS_ERROR);
    check("seek before the start", sys_seek(fd, -1, SEEK_FROM_START), SYS_ERROR);
    check("seek past the end", sys_seek(fd, 1, SEEK_FROM_END), SYS_ERROR);
    check("seek to the end", sys_seek(fd, 0, SEEK_FROM_END), size);
    check("seek to 10", sys_seek(fd, 10, SEEK_FROM_START), 10);
    check("seek back far", sys_seek(fd, INT32_MIN, SEEK_FROM_CURRENT), SYS_ERROR);
    check("a refused seek stays", sys_seek(fd, 0, SEEK_FROM_CURRENT), 10);
    check("seek back 10", sys_seek(fd, -10, SEEK_FROM_CURRENT), 0);
    check("read from there", sys_read(fd, buffer, 7) == 7 && buffer[0] == 'W', 1);
    check("close", sys_close(fd), 0);
    check("seek after close", sys_seek(fd, 0, SEEK_FROM_START), SYS_ERROR);
    u_puts("syscheck: seek\n");
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
