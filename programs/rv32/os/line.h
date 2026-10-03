/* The console line editor the shell and the C library's line discipline share
 * (Track 3; it was sh.c's, issue #30). The host does not echo, so each key is
 * echoed as it is read:
 *
 *   Enter            \n, \r or \r\n (the \n after a \r is skipped even when
 *                    it arrives with the next line); echoed as \n
 *   Backspace, ^H    removes the last character
 *   ^U               clears the line
 *   Tab              a space
 *   ^D               at the start of a line, the end of input, unecho'd, when
 *                    the caller asks for it (LINE_EOF); otherwise as below
 *   escape sequences dropped (arrow and function keys)
 *   other control
 *   and non-ASCII    ring the bell and are dropped, so a character is one byte
 *                    and one column
 *
 * A key past the line's `room` characters rings the bell and is dropped, and
 * the line is then refused at Enter however it is edited after, unless ^U
 * clears it: a line that lost a byte is never handed over.
 *
 * Uses only system calls, so programs on the user library and on picolibc can
 * both link it. */
#ifndef RV32_OS_LINE_H
#define RV32_OS_LINE_H

#include <stdint.h>

#define LINE_EOF 1u /* flags: ^D at the start of a line ends the input */

enum line_result {
    LINE_OK,       /* line[0..*length) holds the line, without its newline */
    LINE_TOO_LONG, /* refused: a key was dropped; *length is 0 */
    LINE_END,      /* ^D at the start of a line (LINE_EOF only) */
    LINE_ERROR,    /* reading the console failed: a kernel bug, not input */
};

enum line_result line_edit(char *line, uint32_t room, uint32_t *length, uint32_t flags);

#endif
