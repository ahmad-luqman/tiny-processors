/* rv32_gdb: a GDB remote serial protocol stub for the headless emulator (docs/rv32-gdb.md).
 *
 * rv32emu --gdb PORT listens on 127.0.0.1, accepts one client, and lets it drive the machine:
 * registers, memory, breakpoints, single step, continue, and Ctrl-C. The stub only calls the
 * core's public API (emu_run_until, emu_debug_read/write, emu_csr_read/write), so the machine
 * under a debugger executes exactly the same steps, trace lines, and device effects as without.
 * The window (rv32win) does not link it.
 */
#ifndef RV32_GDB_H
#define RV32_GDB_H

#include <stdint.h>
#include "rv32emu_core.h"

/* How a session ended.
 * GDB_HALTED:   the guest halted (done, double fault, limit); the client is still connected and
 *               waits for the exit packet that gdb_report_exit sends once the status is known.
 * GDB_DETACHED: the client detached; the connection is closed and the run continues on its own.
 * GDB_KILLED:   the client killed the run or hung up; the connection is closed and the machine
 *               is halted as HALT_STOPPED. */
typedef enum { GDB_HALTED, GDB_DETACHED, GDB_KILLED } gdb_end;

/* Listen on 127.0.0.1:port (0 picks a free port), print `<prog>: gdb listening on 127.0.0.1:N`
 * to stderr, and accept one connection. Returns the connected socket, or -1 after a message. */
int gdb_accept(uint16_t port);

/* Serve the client with the machine stopped at its current pc until the session ends. */
gdb_end gdb_serve(int fd, machine *m);

/* After GDB_HALTED: send `W<status>` (the process exit status, 0 pass) and close the socket. */
void gdb_report_exit(int fd, int status);

#endif
