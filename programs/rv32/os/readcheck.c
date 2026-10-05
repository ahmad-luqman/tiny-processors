/* readcheck: a user program's read of a file through every path of the kernel's fs_read (issue
 * #35). It reads `pad`, whose byte i is i mod 256, so every byte read can be checked:
 *   whole sectors into an aligned buffer (straight from the device), the same into a misaligned
 *   one (through the sector buffer), a read that starts and ends mid-sector, a whole-sector read
 *   followed by a partial sector with a canary word after the buffer, a read that runs past the
 *   end of the file with a canary after what the file holds, and a read at the end (nothing).
 * It also spawns itself, which the kernel must refuse while it runs (its slots are in use). It
 * lives on the disk, beside diskprog, and prints one line per check. */
#include "ulib.h"

#define CANARY 0xc0ffee11u

static uint32_t words[1100]; /* 4,400 bytes, word aligned */

static uint32_t pattern_ok(const uint8_t *bytes, uint32_t position, uint32_t length)
{
    for (uint32_t i = 0; i < length; i++) {
        if (bytes[i] != (uint8_t)(position + i)) {
            return 0;
        }
    }
    return 1;
}

/* Read `length` bytes of `pad` from `position` into `to`; 1 when exactly `expect` came back,
 * every one of them right. */
static uint32_t check(uint32_t fd, uint32_t position, uint8_t *to, uint32_t length, uint32_t expect)
{
    if (sys_seek(fd, (int32_t)position, 0) != position) {
        return 0;
    }
    uint32_t got = sys_read(fd, to, length);
    return got == expect && pattern_ok(to, position, got);
}

static void report(const char *what, uint32_t ok, uint32_t *failures)
{
    u_puts("readcheck: ");
    u_puts(what);
    u_puts(ok ? ": ok\n" : ": wrong\n");
    *failures += !ok;
}

int main(const char *args)
{
    (void)args;
    uint32_t failures = 0, fd = sys_open("pad", O_READ);
    if (fd == SYS_ERROR) {
        u_puts("readcheck: no pad\n");
        return 1;
    }
    uint8_t *bytes = (uint8_t *)words;
    uint32_t size = sys_seek(fd, 0, 2);
    report("eight whole sectors, aligned", check(fd, 0, bytes, 4096, 4096), &failures);
    report("two whole sectors, misaligned buffer", check(fd, 512, bytes + 1, 1024, 1024), &failures);
    report("mid-sector to mid-sector", check(fd, 500, bytes, 1500, 1500), &failures);
    words[175] = CANARY; /* the word after 700 bytes */
    report("a sector and a part, canary kept", check(fd, 0, bytes, 700, 700) && words[175] == CANARY, &failures);
    words[150] = CANARY; /* the word after the 600 bytes the file has left */
    report("past the end of the file, canary kept",
           check(fd, size - 600, bytes, 1024, 600) && words[150] == CANARY, &failures);
    report("at the end of the file", check(fd, size, bytes, 512, 0), &failures);
    sys_close(fd);
    report("a second instance refused", sys_spawn("readcheck", "") == SYS_ERROR, &failures);
    return failures != 0;
}
