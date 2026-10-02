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
 *   gettimeofday     the epoch: the machine has no real-time clock
 *   times            device ticks (clock() and Lua's os.clock() count them)
 *   unlink, rename,
 *   stat, getentropy not provided by the kernel: ENOSYS
 *
 * A failed call sets errno and returns -1 as POSIX says. */
#include <errno.h>
#include <fcntl.h>
#include <signal.h>
#include <stdint.h>
#include <string.h>
#include <sys/stat.h>
#include <sys/time.h>
#include <sys/times.h>
#include <unistd.h>

#include "sys.h"
#include "ulib.h"

#define FIRST_FILE 3
#define FILES 4 /* the kernel's descriptors 3 to 6 */

ssize_t __tty_read(void *buf, size_t len); /* tty.c */

_Noreturn void sys_exit(uint32_t code)
{
    for (;;) {
        (void)syscall3(SYS_EXIT, code, 0, 0);
    }
}

void _exit(int code)
{
    sys_exit((uint32_t)code);
}

static int fail(int error)
{
    errno = error;
    return -1;
}

static int is_file(int fd)
{
    return fd >= FIRST_FILE && fd < FIRST_FILE + FILES;
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
    return got == SYS_ERROR ? fail(EIO) : (ssize_t)got;
}

ssize_t write(int fd, const void *buf, size_t len)
{
    if (fd != 1 && fd != 2 && !is_file(fd)) {
        return fail(EBADF);
    }
    uint32_t put = sys_write((uint32_t)fd, buf, len);
    return put == SYS_ERROR ? fail(EIO) : (ssize_t)put;
}

/* Whether the disk holds a file of this name, so a refused open can say why. */
static int exists(const char *name)
{
    char entry[20];
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
    if (strlen(name) >= 20) {
        return fail(ENAMETOOLONG);
    }
    uint32_t fd = sys_open(name, mode);
    if (fd != SYS_ERROR) {
        return (int)fd;
    }
    /* Every descriptor taken, no such file, or (it exists) another process has it open for
     * writing, or (it would be new) the directory or the disk is full. */
    int open_files = 0;
    for (int i = FIRST_FILE; i < FIRST_FILE + FILES; i++) {
        open_files += sys_seek((uint32_t)i, 0, SEEK_FROM_CURRENT) != SYS_ERROR;
    }
    if (open_files == FILES) {
        return fail(EMFILE);
    }
    if (exists(name)) {
        return fail(EBUSY);
    }
    return fail(mode & O_CREATE ? ENOSPC : ENOENT);
}

int close(int fd)
{
    if (!is_file(fd) || sys_close((uint32_t)fd) == SYS_ERROR) {
        return fail(EBADF);
    }
    return 0;
}

off_t lseek(int fd, off_t offset, int whence)
{
    if (!is_file(fd)) {
        return fail(fd >= 0 && fd < FIRST_FILE ? ESPIPE : EBADF);
    }
    if (whence != SEEK_SET && whence != SEEK_CUR && whence != SEEK_END) {
        return fail(EINVAL);
    }
    if (offset < INT32_MIN || offset > INT32_MAX) {
        return fail(EINVAL);
    }
    uint32_t to = sys_seek((uint32_t)fd, (int32_t)offset, (uint32_t)whence);
    if (to == SYS_ERROR) {
        return fail(sys_seek((uint32_t)fd, 0, SEEK_FROM_CURRENT) == SYS_ERROR ? EBADF : EINVAL);
    }
    return (off_t)to;
}

int fstat(int fd, struct stat *st)
{
    memset(st, 0, sizeof *st);
    if (fd >= 0 && fd < FIRST_FILE) {
        st->st_mode = S_IFCHR;
        return 0;
    }
    uint32_t here = is_file(fd) ? sys_seek((uint32_t)fd, 0, SEEK_FROM_CURRENT) : SYS_ERROR;
    if (here == SYS_ERROR) {
        return fail(EBADF);
    }
    st->st_mode = S_IFREG;
    st->st_size = (off_t)sys_seek((uint32_t)fd, 0, SEEK_FROM_END);
    (void)sys_seek((uint32_t)fd, (int32_t)here, SEEK_FROM_START);
    return 0;
}

int isatty(int fd)
{
    return fd >= 0 && fd < FIRST_FILE ? 1 : (errno = is_file(fd) ? ENOTTY : EBADF, 0);
}

void *sbrk(ptrdiff_t increment)
{
    if (increment < 0) {
        errno = ENOMEM; /* the kernel's break only grows */
        return (void *)-1;
    }
    uint32_t old = (uint32_t)(uintptr_t)sys_sbrk((uint32_t)increment);
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

int getentropy(void *buf, size_t len)
{
    (void)buf;
    (void)len;
    return fail(ENOSYS);
}

/* picolibc's strerror asks this hook first for a message it does not know; there are none. */
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
