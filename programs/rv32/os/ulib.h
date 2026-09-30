/* The user library (Track 2, O2): system call wrappers and the few string and
 * number helpers a program needs without a C library. Programs link ustart.S,
 * ulib.c and udecimal.c; console.h's rv32_putc/rv32_puts/rv32_put_hex32/rv32_put_udec and
 * board.h's rv32_exit are defined here too, on top of system calls, so code
 * written for the bare machine (the games, the digit screen) links unchanged. */
#ifndef RV32_OS_ULIB_H
#define RV32_OS_ULIB_H

#include <stddef.h>
#include <stdint.h>

#include "sys.h"
#include "udecimal.h"

static inline uint32_t syscall3(uint32_t number, uint32_t a, uint32_t b, uint32_t c)
{
    register uint32_t a0 __asm__("a0") = a;
    register uint32_t a1 __asm__("a1") = b;
    register uint32_t a2 __asm__("a2") = c;
    register uint32_t a7 __asm__("a7") = number;
    __asm__ volatile("ecall" : "+r"(a0) : "r"(a1), "r"(a2), "r"(a7) : "memory");
    return a0;
}

_Noreturn void sys_exit(uint32_t code);
static inline uint32_t sys_write(uint32_t fd, const void *buf, uint32_t len) { return syscall3(SYS_WRITE, fd, (uint32_t)(uintptr_t)buf, len); }
static inline uint32_t sys_read(uint32_t fd, void *buf, uint32_t len) { return syscall3(SYS_READ, fd, (uint32_t)(uintptr_t)buf, len); }
static inline uint32_t sys_event(void) { return syscall3(SYS_EVENT, 0, 0, 0); }
static inline uint32_t sys_keys(void) { return syscall3(SYS_KEYS, 0, 0, 0); }
static inline uint32_t sys_present(void) { return syscall3(SYS_PRESENT, 0, 0, 0); }
static inline void *sys_sbrk(uint32_t increment) { return (void *)(uintptr_t)syscall3(SYS_SBRK, increment, 0, 0); }
static inline uint32_t sys_spawn(const char *name, const char *args) { return syscall3(SYS_SPAWN, (uint32_t)(uintptr_t)name, (uint32_t)(uintptr_t)args, 0); }
static inline uint32_t sys_wait(uint32_t pid) { return syscall3(SYS_WAIT, pid, 0, 0); }
static inline uint32_t sys_list(uint32_t index, char *buf, uint32_t len) { return syscall3(SYS_LIST, index, (uint32_t)(uintptr_t)buf, len); }
static inline void sys_yield(void) { (void)syscall3(SYS_YIELD, 0, 0, 0); }
static inline void sys_sleep(uint32_t ticks) { (void)syscall3(SYS_SLEEP, ticks, 0, 0); }
static inline void sys_halt(uint32_t code) { (void)syscall3(SYS_HALT, code, 0, 0); }
static inline uint32_t sys_time(void) { return syscall3(SYS_TIME, 0, 0, 0); }
static inline uint32_t sys_getpid(void) { return syscall3(SYS_GETPID, 0, 0, 0); }
static inline uint8_t *sys_display(void) { return (uint8_t *)(uintptr_t)syscall3(SYS_DISPLAY, 0, 0, 0); }
static inline uint32_t sys_open(const char *name, uint32_t flags) { return syscall3(SYS_OPEN, (uint32_t)(uintptr_t)name, flags, 0); }
static inline uint32_t sys_close(uint32_t fd) { return syscall3(SYS_CLOSE, fd, 0, 0); }
static inline uint32_t sys_files(uint32_t index, char *buf, uint32_t len) { return syscall3(SYS_FILES, index, (uint32_t)(uintptr_t)buf, len); }
static inline uint32_t sys_ps(uint32_t index, char *buf, uint32_t len) { return syscall3(SYS_PS, index, (uint32_t)(uintptr_t)buf, len); }
static inline uint32_t sys_switches(void) { return syscall3(SYS_SWITCHES, 0, 0, 0); }

uint32_t u_strlen(const char *s);
int u_strcmp(const char *a, const char *b);
void *memcpy(void *dst, const void *src, size_t n); /* mem.c */
void *memset(void *dst, int value, size_t n);
void u_puts(const char *s);                        /* the string, no newline */
void u_putdec(uint32_t value);
void u_puthex(uint32_t value);                     /* eight lowercase digits */
uint32_t u_parse(const char *s, const char **end); /* a decimal number */

#endif
