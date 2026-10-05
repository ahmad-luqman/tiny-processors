/* The operating-system layer under picolibc (Track 3, L1; docs/rv32-libc.md):
 * the POSIX calls the library's stdio, malloc and time functions are built
 * on, each served by one or two of the kernel's system calls (sys.h).
 *
 *   read, write      fd 0 is the console through the line discipline (tty.c);
 *                    fds 1 and 2 the console; 3 to 6 open files on the disk
 *   open, close      tfs files: read ("r") or written from the start ("w");
 *                    append and read-write are refused (EINVAL), since tfs
 *                    replaces a file's contents when it is opened for writing
 *   lseek, fstat     positions within a file (the kernel's seek call), sizes
 *   sbrk             the kernel's heap, between the program and its stack
 *   gettimeofday     the epoch, with no system call: there is no real-time clock
 *   times            the low word of mtime, in device ticks (see times())
 *   unlink, rename,
 *   stat, mkdir,
 *   getentropy       not provided by the kernel: ENOSYS (mkdir since issue #35:
 *                    Doom makes its save directory, and tfs has none)
 *
 * A failed call sets errno and returns -1 as POSIX says. Streams the program
 * opened and never closed are flushed when it exits (the end of this file). */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <stdio.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/times.h>
#include <unistd.h>

#include "sys.h"
#include "ulib.h"

/* lseek passes whence to the kernel unchanged, and an off_t to its 32-bit offset. */
_Static_assert(SEEK_SET == SEEK_FROM_START && SEEK_CUR == SEEK_FROM_CURRENT && SEEK_END == SEEK_FROM_END,
               "C's whence values are the kernel's");
_Static_assert(sizeof(off_t) == sizeof(int32_t), "an off_t is the kernel's 32-bit offset");

ssize_t __tty_read(void *buf, size_t len); /* tty.c */

void _exit(int code)
{
    for (;;) {
        (void)syscall3(SYS_EXIT, (uint32_t)code, 0, 0);
    }
}

static int fail(int error)
{
    errno = error;
    return -1;
}

static int is_file(int fd)
{
    return fd >= (int)OS_FIRST_FILE && fd < (int)(OS_FIRST_FILE + OS_OPEN_FILES);
}

/* Whether descriptor fd is an open file: the kernel's seek refuses any other. */
static int is_open(int fd)
{
    return is_file(fd) && sys_seek((uint32_t)fd, 0, SEEK_FROM_CURRENT) != SYS_ERROR;
}

ssize_t read(int fd, void *buf, size_t len)
{
    if (fd == 0) {
        return __tty_read(buf, len);
    }
    if (!is_file(fd)) {
        return fail(EBADF);
    }
    uint32_t got = sys_read((uint32_t)fd, buf, len);
    return got == SYS_ERROR ? fail(is_open(fd) ? EIO : EBADF) : (ssize_t)got;
}

ssize_t write(int fd, const void *buf, size_t len)
{
    if (fd != 1 && fd != 2 && !is_file(fd)) {
        return fail(EBADF);
    }
    uint32_t put = sys_write((uint32_t)fd, buf, len);
    if (put == SYS_ERROR) {
        return fail(is_file(fd) && !is_open(fd) ? EBADF : EIO);
    }
    if (put == 0 && len) { /* a tfs file is full at its capacity (4 KiB) */
        return fail(ENOSPC);
    }
    return (ssize_t)put;
}

/* Whether the disk holds a file of this name, so a refused open can say why. */
static int exists(const char *name)
{
    char entry[OS_FILE_NAME];
    for (uint32_t i = 0; sys_files(i, entry, sizeof entry) != SYS_ERROR; i++) {
        if (!strncmp(entry, name, sizeof entry)) {
            return 1;
        }
    }
    return 0;
}

int open(const char *name, int flags, ...)
{
    uint32_t mode;
    if ((flags & O_ACCMODE) == O_RDONLY && !(flags & (O_APPEND | O_TRUNC))) {
        mode = O_READ;
    } else if ((flags & O_ACCMODE) == O_WRONLY && (flags & O_TRUNC) && !(flags & O_APPEND)) {
        mode = O_WRITE | (flags & O_CREAT ? O_CREATE : 0u);
    } else {
        return fail(EINVAL);
    }
    if (strlen(name) >= OS_FILE_NAME) {
        return fail(ENAMETOOLONG);
    }
    uint32_t fd = sys_open(name, mode);
    if (fd != SYS_ERROR) {
        return (int)fd;
    }
    /* The kernel gives no reason, so work one out: every descriptor taken; the file exists but is
     * in use (any process, this one included, writing it, or, to open it for writing, reading
     * it); it does not exist; or it would have been new and the directory or the disk is full. */
    uint32_t open_files = 0;
    for (uint32_t i = 0; i < OS_OPEN_FILES; i++) {
        open_files += is_open((int)(OS_FIRST_FILE + i));
    }
    if (open_files == OS_OPEN_FILES) {
        return fail(EMFILE);
    }
    if (exists(name)) {
        return fail(EBUSY);
    }
    return fail(mode & O_CREATE ? ENOSPC : ENOENT);
}

int close(int fd)
{
    if (!is_open(fd)) {
        return fail(EBADF);
    }
    /* The descriptor is released either way; a failure means a written file's new size did not
     * reach the disk. */
    return sys_close((uint32_t)fd) == SYS_ERROR ? fail(EIO) : 0;
}

off_t lseek(int fd, off_t offset, int whence)
{
    if (!is_file(fd)) {
        return fail(fd >= 0 && fd < (int)OS_FIRST_FILE ? ESPIPE : EBADF);
    }
    if (whence != SEEK_SET && whence != SEEK_CUR && whence != SEEK_END) {
        return fail(EINVAL);
    }
    uint32_t to = sys_seek((uint32_t)fd, (int32_t)offset, (uint32_t)whence);
    if (to == SYS_ERROR) {
        return fail(is_open(fd) ? EINVAL : EBADF); /* outside the file, or not open */
    }
    return (off_t)to;
}

int fstat(int fd, struct stat *st)
{
    memset(st, 0, sizeof *st);
    if (fd >= 0 && fd < (int)OS_FIRST_FILE) {
        st->st_mode = S_IFCHR;
        return 0;
    }
    uint32_t here = is_file(fd) ? sys_seek((uint32_t)fd, 0, SEEK_FROM_CURRENT) : SYS_ERROR;
    if (here == SYS_ERROR) {
        return fail(EBADF);
    }
    uint32_t size = sys_seek((uint32_t)fd, 0, SEEK_FROM_END);
    if (size == SYS_ERROR || sys_seek((uint32_t)fd, (int32_t)here, SEEK_FROM_START) != here) {
        return fail(EIO);
    }
    st->st_mode = S_IFREG;
    st->st_size = (off_t)size;
    return 0;
}

int isatty(int fd)
{
    if (fd >= 0 && fd < (int)OS_FIRST_FILE) {
        return 1;
    }
    errno = is_open(fd) ? ENOTTY : EBADF;
    return 0;
}

void *sbrk(ptrdiff_t increment)
{
    /* The kernel's break only grows. */
    uint32_t old = increment < 0 ? SYS_ERROR : (uint32_t)(uintptr_t)sys_sbrk((uint32_t)increment);
    if (old == SYS_ERROR) {
        errno = ENOMEM;
        return (void *)-1;
    }
    return (void *)(uintptr_t)old;
}

int gettimeofday(struct timeval *restrict tv, void *restrict tz)
{
    (void)tz;
    if (tv) {
        tv->tv_sec = 0;
        tv->tv_usec = 0;
    }
    return 0;
}

/* clock() divides this by CLOCKS_PER_SEC, which picolibc fixes at 1,000,000 whatever a tick is,
 * so clock() and Lua's os.clock() count device ticks, not seconds (a tick has no rate on our
 * machine and is 100 ns or one instruction's 8 ns on QEMU). The low word of mtime wraps, and as
 * a clock_t it turns negative after 2^31 ticks. */
clock_t times(struct tms *buf)
{
    clock_t ticks = (clock_t)sys_time();
    if (buf) {
        buf->tms_utime = ticks;
        buf->tms_stime = 0;
        buf->tms_cutime = 0;
        buf->tms_cstime = 0;
    }
    return ticks;
}

int unlink(const char *name)
{
    (void)name;
    return fail(ENOSYS);
}

int rename(const char *from, const char *to)
{
    (void)from;
    (void)to;
    return fail(ENOSYS);
}

int stat(const char *restrict name, struct stat *restrict st)
{
    (void)name;
    (void)st;
    return fail(ENOSYS);
}

int mkdir(const char *name, mode_t mode)
{
    (void)name;
    (void)mode;
    return fail(ENOSYS);
}

int getentropy(void *buf, size_t len)
{
    (void)buf;
    (void)len;
    return fail(ENOSYS);
}

/* picolibc's strerror calls this weak hook for a number it has no message for. Defining it
 * changes nothing (NULL makes strerror fall back to its own text); it is here because the image
 * checker refuses any undefined symbol, weak ones included. */
char *_user_strerror(int error, int internal, int *errptr)
{
    (void)error;
    (void)internal;
    (void)errptr;
    return 0;
}

/* Signals are picolibc's own table: raise() calls a handler or ends the process. Nothing
 * delivers them asynchronously, so there is no mask to keep. */
int sigprocmask(int how, const sigset_t *restrict set, sigset_t *restrict old)
{
    (void)how;
    (void)set;
    if (old) {
        memset(old, 0, sizeof *old);
    }
    return 0;
}

/* Streams. picolibc's exit flushes stdout and stderr, but it keeps no list of the streams fopen
 * made, so one the program never closed would lose its buffered bytes (Lua's os.exit() does not
 * close files). The link wraps fdopen, which fopen calls, and fclose (-Wl,--wrap, see the
 * Makefile) to keep that list here; there can be at most one stream per open file. freopen
 * keeps its FILE, so the list stays right. */
FILE *__real_fdopen(int fd, const char *mode);
int __real_fclose(FILE *stream);
FILE *__wrap_fdopen(int fd, const char *mode);
int __wrap_fclose(FILE *stream);

static FILE *streams[OS_OPEN_FILES];

FILE *__wrap_fdopen(int fd, const char *mode)
{
    FILE *stream = __real_fdopen(fd, mode);
    for (uint32_t i = 0; stream && i < OS_OPEN_FILES; i++) {
        if (!streams[i]) {
            streams[i] = stream;
            break;
        }
    }
    return stream;
}

int __wrap_fclose(FILE *stream)
{
    for (uint32_t i = 0; i < OS_OPEN_FILES; i++) {
        if (streams[i] == stream) {
            streams[i] = 0;
        }
    }
    return __real_fclose(stream);
}

__attribute__((destructor)) static void flush_streams(void)
{
    for (uint32_t i = 0; i < OS_OPEN_FILES; i++) {
        if (streams[i]) {
            (void)fflush(streams[i]); /* the kernel closes the file, and writes its size, at exit */
        }
    }
}
