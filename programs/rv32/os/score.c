/* High scores on the disk; see score.h. */
#include "score.h"

#include "ulib.h"

#define GAMES 4
#define TEXT 128

static char text[TEXT];

uint32_t score_record(const char *game, uint32_t value)
{
    char names[GAMES][16];
    uint32_t bests[GAMES], games = 0, length = 0;
    uint32_t fd = sys_open("scores", O_READ);
    if (fd != SYS_ERROR) {
        length = sys_read(fd, text, TEXT - 1);
        sys_close(fd);
        if (length == SYS_ERROR) {
            length = 0;
        }
    }
    text[length] = 0;
    /* Parse `NAME N` lines. */
    for (const char *p = text; *p && games < GAMES;) {
        uint32_t n = 0;
        while (*p && *p != ' ' && *p != '\n' && n + 1 < sizeof names[0]) {
            names[games][n++] = *p++;
        }
        names[games][n] = 0;
        while (*p == ' ') {
            p++;
        }
        bests[games] = u_parse(p, &p);
        while (*p && *p != '\n') {
            p++;
        }
        if (*p) {
            p++;
        }
        if (n) {
            games++;
        }
    }
    uint32_t i = 0;
    while (i < games && u_strcmp(names[i], game)) {
        i++;
    }
    if (i == games && games < GAMES) {
        uint32_t n = 0;
        for (; game[n] && n + 1 < sizeof names[0]; n++) {
            names[i][n] = game[n];
        }
        names[i][n] = 0;
        bests[i] = 0;
        games++;
    }
    if (i < games && value > bests[i]) {
        bests[i] = value;
    }
    uint32_t best = i < games ? bests[i] : value;
    fd = sys_open("scores", O_WRITE | O_CREATE);
    if (fd == SYS_ERROR) {
        return value; /* no disk: nothing to keep */
    }
    for (uint32_t k = 0; k < games; k++) {
        (void)sys_write(fd, names[k], u_strlen(names[k]));
        (void)sys_write(fd, " ", 1);
        char digits[11];
        uint32_t d = 10, v = bests[k];
        digits[d] = 0;
        do {
            digits[--d] = (char)('0' + v % 10u);
            v /= 10u;
        } while (v);
        (void)sys_write(fd, digits + d, 10 - d);
        (void)sys_write(fd, "\n", 1);
    }
    sys_close(fd);
    u_puts(game);
    u_puts(": best ");
    u_putdec(best);
    u_puts("\n");
    return best;
}
