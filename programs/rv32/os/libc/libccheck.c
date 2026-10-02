/* libccheck: the C library on the kernel (Track 3, L1; docs/rv32-libc.md).
 * Each group checks picolibc and the system calls under it against values
 * written here by hand, prints one line, and counts what failed; the last
 * line is `libccheck: ok` or the number of failures, and so is the exit code.
 *
 * It reads one line from the console (the session types it) and writes,
 * rewrites and reads back a file, libc.out, on the disk. */
#include <errno.h>
#include <inttypes.h>
#include <math.h>
#include <setjmp.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

static int failures;

static void check(const char *what, int ok)
{
    if (!ok) {
        printf("libccheck: FAILED %s\n", what);
        failures++;
    }
}

static void check_text(const char *what, const char *got, const char *want)
{
    if (strcmp(got, want)) {
        printf("libccheck: FAILED %s: got \"%s\" want \"%s\"\n", what, got, want);
        failures++;
    }
}

static void formatting(void)
{
    char text[96];
    snprintf(text, sizeof text, "%d %u %x %X %o %c %5s|%-5s|", -42, 42u, 0xbeefu, 0xbeefu, 8u, 'z', "ab", "cd");
    check_text("integers", text, "-42 42 beef BEEF 10 z    ab|cd   |");
    snprintf(text, sizeof text, "%lld %llu %" PRIx64, -9007199254740993ll, 18446744073709551615ull, (uint64_t)0x123456789abcdefull);
    check_text("long long", text, "-9007199254740993 18446744073709551615 123456789abcdef");
    snprintf(text, sizeof text, "%.3f %g %e %.14g %a", 3.14159265358979, 1e-5, 12345.678, 0.1 + 0.2, 1.0);
    check_text("doubles", text, "3.142 1e-05 1.234568e+04 0.3 0x1p+0");
    /* picolibc prints the shortest digits that read back exactly, then zeros: %.17g of 0.1 is 0.1,
     * where glibc gives 0.10000000000000001 (docs/rv32-libc.md). */
    snprintf(text, sizeof text, "%.17g %.17g %.20f", 0.1 + 0.2, 0.1, 0.1);
    check_text("digits", text, "0.30000000000000004 0.1 0.10000000000000000000");
    snprintf(text, sizeof text, "%g %g %g %f", 1.0 / 0.0, -1.0 / 0.0, 0.0 / 0.0, -0.0);
    check_text("special doubles", text, "inf -inf nan -0.000000");
    check("snprintf's length", snprintf(text, 4, "%s", "truncated") == 9 && !strcmp(text, "tru"));
    printf("libccheck: printf %d %.2f %s\n", 7, 2.5, "ok");
}

static void conversion(void)
{
    char *end;
    check("strtol", strtol("  -1234xyz", &end, 10) == -1234 && *end == 'x');
    check("strtoul hex", strtoul("0xffffffff", 0, 16) == 0xffffffffu);
    check("strtoll", strtoll("-9223372036854775807", 0, 10) == -9223372036854775807ll);
    check("atoi", atoi("77") == 77);
    check("strtod", strtod("2.5e3", &end) == 2500.0 && !*end);
    check("strtod 0.1", strtod("0.1", 0) == 0.1);
    check("strtod hex", strtod("0x1.8p1", 0) == 3.0);
    double back;
    char text[32];
    snprintf(text, sizeof text, "%.17g", 1.0 / 3.0);
    check("round trip", sscanf(text, "%lf", &back) == 1 && back == 1.0 / 3.0);
    int a, b;
    check("sscanf", sscanf("12,34", "%d,%d", &a, &b) == 2 && a == 12 && b == 34);
    puts("libccheck: conversions");
}

static int compare(const void *a, const void *b)
{
    int x = *(const int *)a, y = *(const int *)b;
    return (x > y) - (x < y);
}

static void strings(void)
{
    char text[32] = "hello, world";
    check("strlen", strlen(text) == 12);
    check("strchr", strchr(text, 'w') == text + 7);
    check("strrchr", strrchr(text, 'o') == text + 8);
    check("strstr", strstr(text, "wor") == text + 7);
    check("strcmp", strcmp("abc", "abd") < 0 && strcmp("b", "a") > 0 && !strcmp("x", "x"));
    check("strncpy", !strcmp(strncpy(text, "abc", sizeof text), "abc"));
    memmove(text + 1, text, 4); /* overlapping */
    check("memmove", !memcmp(text, "aabc", 4));
    int numbers[] = {5, -3, 9, 0, 12, 7, -8};
    qsort(numbers, 7, sizeof numbers[0], compare);
    int sorted[] = {-8, -3, 0, 5, 7, 9, 12};
    check("qsort", !memcmp(numbers, sorted, sizeof sorted));
    int key = 7;
    check("bsearch", bsearch(&key, numbers, 7, sizeof numbers[0], compare) == &numbers[4]);
    puts("libccheck: strings");
}

static void heap(void)
{
    char *blocks[32];
    for (int i = 0; i < 32; i++) {
        blocks[i] = malloc(100 + 37 * i);
        if (blocks[i]) {
            memset(blocks[i], i, 100 + 37 * i);
        }
    }
    int intact = 1;
    for (int i = 0; i < 32; i++) {
        intact &= blocks[i] && blocks[i][0] == i && blocks[i][99 + 37 * i] == i;
    }
    check("malloc", intact);
    for (int i = 0; i < 32; i += 2) {
        free(blocks[i]);
    }
    char *grown = realloc(blocks[1], 5000);
    check("realloc keeps the contents", grown && grown[0] == 1 && grown[136] == 1);
    blocks[1] = grown;
    int *zeros = calloc(256, sizeof *zeros);
    int all_zero = zeros != 0;
    for (int i = 0; zeros && i < 256; i++) {
        all_zero &= zeros[i] == 0;
    }
    check("calloc", all_zero);
    free(zeros);
    for (int i = 1; i < 32; i += 2) {
        free(blocks[i]);
    }
    errno = 0;
    check("malloc beyond the stack", malloc(16u << 20) == 0 && errno == ENOMEM);
    char *again = malloc(4000);
    check("malloc after a refusal", again != 0);
    free(again);
    puts("libccheck: heap");
}

static jmp_buf jump;

static void deep(int depth)
{
    if (depth == 0) {
        longjmp(jump, 42);
    }
    deep(depth - 1);
}

static void control(void)
{
    volatile int passes = 0;
    int value = setjmp(jump);
    passes++;
    if (value == 0) {
        deep(10);
    }
    check("setjmp and longjmp", value == 42 && passes == 2);
    puts("libccheck: setjmp");
}

static int near(double got, double want)
{
    return fabs(got - want) <= 1e-15 * fabs(want);
}

static void maths(void)
{
    check("sqrt", sqrt(2.0) == 1.4142135623730951);
    check("sin", near(sin(1.0), 0.8414709848078965));
    check("cos", near(cos(1.0), 0.5403023058681398));
    check("exp", near(exp(1.0), 2.718281828459045));
    check("log", near(log(10.0), 2.302585092994046));
    check("pow", near(pow(2.0, 0.5), 1.4142135623730951) && pow(2.0, 10.0) == 1024.0);
    check("floor and ceil", floor(-2.5) == -3.0 && ceil(-2.5) == -2.0);
    check("fmod", fmod(10.5, 3.0) == 1.5);
    check("atan2", near(atan2(1.0, 1.0), 0.7853981633974483));
    check("float", sqrtf(2.0f) == 1.41421354f && near(sinf(1.0f), 0.8414709568023682));
    printf("libccheck: maths %.15g %.15g\n", sqrt(2.0), sin(1.0));
}

static void files(void)
{
    FILE *f = fopen("libc.out", "w");
    check("fopen to write", f != 0);
    if (f) {
        for (int i = 1; i <= 3; i++) {
            fprintf(f, "line %d of %s\n", i, "libc.out");
        }
        check("fclose after writing", fclose(f) == 0);
    }
    char text[64];
    f = fopen("libc.out", "r");
    check("fopen to read", f != 0);
    if (f) {
        check("fgets", fgets(text, sizeof text, f) && !strcmp(text, "line 1 of libc.out\n"));
        check("ftell", ftell(f) == 19);
        check("fseek to the end", fseek(f, 0, SEEK_END) == 0 && ftell(f) == 57);
        check("fseek back", fseek(f, -19, SEEK_END) == 0 && fgets(text, sizeof text, f) &&
                                !strcmp(text, "line 3 of libc.out\n"));
        check("end of file", fgets(text, sizeof text, f) == 0 && feof(f));
        rewind(f);
        check("rewind and fread", fread(text, 1, 6, f) == 6 && !memcmp(text, "line 1", 6));
        check("fseek past the end", fseek(f, 100, SEEK_SET) != 0);
        fclose(f);
    }
    errno = 0;
    check("no such file", fopen("nosuch", "r") == 0 && errno == ENOENT);
    printf("libccheck: nosuch: %s\n", strerror(ENOENT));
    errno = 0;
    check("append refused", fopen("libc.out", "a") == 0 && errno == EINVAL);
    f = fopen("libc.out", "w"); /* writing replaces the contents */
    if (f) {
        fputs("rewritten\n", f);
        fclose(f);
    }
    f = fopen("libc.out", "r");
    check("rewritten", f && fgets(text, sizeof text, f) && !strcmp(text, "rewritten\n") && !fgets(text, sizeof text, f));
    if (f) {
        fclose(f);
    }
    puts("libccheck: files");
}

static void console(void)
{
    char text[64];
    fputs("libccheck: type a line: ", stdout);
    fflush(stdout);
    int got = fgets(text, sizeof text, stdin) != 0;
    int a = 0, b = 0;
    check("fgets from the console", got && sscanf(text, "%d plus %d", &a, &b) == 2);
    printf("libccheck: %d plus %d is %d\n", a, b, a + b);
}

static void clock_and_time(void)
{
    time_t now = time(0);
    struct tm *t = gmtime(&now);
    char text[32];
    strftime(text, sizeof text, "%Y-%m-%d %H:%M:%S", t);
    check_text("the epoch", text, "1970-01-01 00:00:00"); /* no real-time clock */
    clock_t before = clock();
    check("clock runs", clock() >= before);
    puts("libccheck: time");
}

int main(int argc, char **argv)
{
    check("argv", argc == 3 && !strcmp(argv[0], "libccheck") && !strcmp(argv[1], "one") &&
                      !strcmp(argv[2], "two words") && argv[3] == 0);
    printf("libccheck: %d arguments, \"%s\"\n", argc - 1, argc > 2 ? argv[2] : "");
    formatting();
    conversion();
    strings();
    heap();
    control();
    maths();
    files();
    console();
    clock_and_time();
    if (failures) {
        printf("libccheck: %d FAILED\n", failures);
    } else {
        puts("libccheck: ok");
    }
    return failures;
}
