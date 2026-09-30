/* Reports from programs that run side by side (Track 2, O4). Their console
 * lines would interleave differently on every backend, so each writes its one
 * line to a file of its own, which the disk image already holds (so creating
 * it cannot depend on the order either); the shell prints the files after
 * `wait`. Without a disk the line goes to the console. */
#ifndef RV32_OS_REPORT_H
#define RV32_OS_REPORT_H

#include <stdint.h>

/* "NAME: WHAT, sum XXXXXXXX, preempted yes|no" into NAME.out; returns 0, or 2 (after saying so on
 * the console) when the file could not be written. */
int report(const char *name, const char *what, uint32_t sum);

#endif
