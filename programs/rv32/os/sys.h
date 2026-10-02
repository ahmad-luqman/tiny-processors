/* The system call interface (Track 2, O2; docs/rv32-os.md).
 *
 * A program executes `ecall` with the call number in a7 and its arguments in
 * a0..a5; the kernel returns the result in a0 and resumes after the ecall.
 * Every other register is preserved. A call that fails returns SYS_ERROR.
 * Pointers must lie inside the calling program's own slots.
 *
 * Shared by the kernel and the user library; numbers never change meaning. */
#ifndef RV32_OS_SYS_H
#define RV32_OS_SYS_H

#define SYS_EXIT     0  /* exit(code): end the process; a waiting parent gets code */
#define SYS_WRITE    1  /* write(fd, buf, len): fd 1 and 2 are the console; returns len */
#define SYS_READ     2  /* read(fd, buf, len): fd 0 is the console; waits for at least one byte */
#define SYS_EVENT    3  /* event(): the next input event (docs/rv32.md, "Input"), or 0 */
#define SYS_KEYS     4  /* keys(): the held-key mask */
#define SYS_PRESENT  5  /* present(): show the framebuffer; returns the frame count */
#define SYS_SBRK     6  /* sbrk(increment): grow the heap; returns the old break */
#define SYS_SPAWN    7  /* spawn(name, args): start a program from the RAM disk; returns its pid */
#define SYS_WAIT     8  /* wait(pid): wait for a child to exit; returns its exit code */
#define SYS_LIST     9  /* list(index, buf, len): the name of program `index`; returns its length */
#define SYS_YIELD   10  /* yield(): let another process run */
#define SYS_SLEEP   11  /* sleep(ticks): wait at least this many device ticks (O4) */
#define SYS_HALT    12  /* halt(code): stop the machine (0 passes) */
#define SYS_TIME    13  /* time(): the low word of mtime */
#define SYS_GETPID  14  /* getpid() */
#define SYS_DISPLAY 15  /* display(): the framebuffer's address, or 0 when there is no display */
#define SYS_OPEN    16  /* open(name, flags): a file on the disk (O3); returns a descriptor */
#define SYS_CLOSE   17  /* close(fd) */
#define SYS_FILES   18  /* files(index, buf, len): the name of file `index` into buf (O3); returns its size in bytes */
#define SYS_PS      19  /* ps(index, buf, len): one line about process table entry `index` (O4) */
#define SYS_SWITCHES 20 /* switches(): how often the timer took the machine from the caller (O4) */
#define SYS_SEEK    21  /* seek(fd, offset, whence): move an open file's position (Track 3); returns the new one */
#define SYS_CALLS   22

#define SYS_ERROR 0xffffffffu

/* open() flags (O3). */
#define O_READ   1u
#define O_WRITE  2u  /* write from the start; the file's size becomes what is written */
#define O_CREATE 4u

/* seek() whence values (Track 3), as in C's SEEK_SET, SEEK_CUR and SEEK_END. The offset is signed;
 * the new position must lie between 0 and the file's size. */
#define SEEK_FROM_START   0u
#define SEEK_FROM_CURRENT 1u
#define SEEK_FROM_END     2u

/* The layout every program is linked for: from slot n of 128 KiB above the kernel's 1 MiB, one
 * or more slots (a program's span, O4); the top 32 KiB of the span is its stack, or as much as
 * its RAM disk entry says (Track 3), of which the lowest page is a guard. */
#define OS_KERNEL_SIZE 0x00100000u
#define OS_SLOT_BASE   0x80100000u
#define OS_SLOT_SIZE   0x00020000u
#define OS_SLOTS       24u
#define OS_STACK_SIZE  0x00008000u /* the default; sbrk stops below a program's stack */
#define OS_GUARD_SIZE  0x00001000u /* the stack's lowest page, left unmapped (Track 3) */

#endif
