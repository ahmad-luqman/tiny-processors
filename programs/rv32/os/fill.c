/* fill NAME BYTES: make NAME hold BYTES bytes of numbered 64-byte lines, creating it if needed,
 * and say how many the file took (Track 2). A file's capacity is 4 KiB, so a larger request is
 * cut there: the write call returns what fitted, and fill exits 1. */
#include "ulib.h"

int main(const char *args)
{
    char name[20];
    uint32_t n = 0;
    while (args[n] && args[n] != ' ') {
        if (n + 1 == sizeof name) {
            u_puts("fill: a name is at most 19 bytes\n");
            return 1;
        }
        name[n] = args[n];
        n++;
    }
    name[n] = 0;
    const char *end;
    uint32_t want = u_parse(args + n + (args[n] == ' '), &end);
    uint32_t fd = n && !*end ? sys_open(name, O_WRITE | O_CREATE) : SYS_ERROR;
    if (fd == SYS_ERROR) {
        u_puts("fill: NAME BYTES\n");
        return 2;
    }
    static char block[512]; /* eight lines, one sector: the fewest calls and transfers */
    uint32_t done = 0, line = 0;
    while (done < want) {
        for (uint32_t row = 0; row < 8; row++, line++) {
            char *text = block + 64 * row, digits[11];
            uint32_t d = u_decimal(line, digits), k = 0;
            for (uint32_t pad = 10 - d; pad < 4; pad++) {
                text[k++] = '0';
            }
            while (digits[d]) {
                text[k++] = digits[d++];
            }
            char c = (char)('a' + (line & 15u)); /* no divide: this is RV32I */
            for (; k < 63; k++) {
                text[k] = c;
                c = c == 'z' ? 'a' : (char)(c + 1);
            }
            text[63] = '\n';
        }
        uint32_t chunk = want - done < sizeof block ? want - done : sizeof block;
        uint32_t wrote = sys_write(fd, block, chunk);
        if (wrote == SYS_ERROR) {
            sys_close(fd);
            u_puts("fill: write error\n");
            return 2;
        }
        done += wrote;
        if (wrote < chunk) {
            break; /* the file is full */
        }
    }
    int closed = sys_close(fd) == 0;
    u_puts("fill: ");
    u_puts(name);
    u_puts(" holds ");
    u_putdec(done);
    u_puts(" of ");
    u_putdec(want);
    u_puts(" bytes\n");
    return !closed ? 2 : done < want ? 1 : 0;
}
