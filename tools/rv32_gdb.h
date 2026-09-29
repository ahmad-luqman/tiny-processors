/* rv32_gdb: a GDB remote serial protocol stub for the headless emulator (docs/rv32-gdb.md).
 *
 * rv32emu --gdb PORT listens on 127.0.0.1, accepts one client, and lets it drive the machine:
 * registers, memory, breakpoints, single step, continue, and Ctrl-C. The guarantee: the guest
 * executes only through emu_run_until and memory is touched only through emu_debug_read/write
 * (never a device), so the machine under a debugger executes exactly the same steps, trace
 * lines, and device effects as without. Like rv32emu.c and rv32win.c, the stub reads and sets
 * the architectural registers (x, f, pc) and the halt reason in the machine struct directly;
 * CSRs go through emu_csr_read/write for their WARL masks. The window does not link it.
 */
#ifndef RV32_GDB_H
#define RV32_GDB_H

#include <stdint.h>
#include "rv32emu_core.h"

/* How a session ended.
 * GDB_HALTED:   the guest halted (done, double fault, limit); the client is still connected and
 *               waits for the exit packet that gdb_report_exit sends once the status is known.
 * GDB_DETACHED: the client detached; the connection is closed and the run continues on its own.
 * GDB_KILLED:   the client killed the run (k, vKill; quietly) or the connection was lost (one
 *               `gdb: connection ...` line on stderr says why); the connection is closed and
 *               the machine is halted as HALT_STOPPED. */
typedef enum { GDB_HALTED, GDB_DETACHED, GDB_KILLED } gdb_end;

/* Listen on 127.0.0.1:port (0 picks a free port), print `<prog>: gdb listening on 127.0.0.1:N`
 * to stderr, and accept one connection. Returns the connected socket, or -1 after a message. */
int gdb_accept(uint16_t port);

/* Serve the client with the machine stopped at its current pc until the session ends. Takes
 * ownership of fd: the stub closes it, here or in gdb_report_exit. Once per process. */
gdb_end gdb_serve(int fd, machine *m);

/* Send `W<status>` (the process exit status, 0 pass) and close the socket if the session ended
 * GDB_HALTED; otherwise (no session, detached, killed) do nothing. */
void gdb_report_exit(int status);

#endif
