/* High scores (Track 2, O3): the file "scores" holds one line per game,
 * `NAME BEST`. score_record keeps the better of the old best and `value`,
 * prints `NAME: best N` and returns the best; without a disk it prints
 * nothing and returns `value`. */
#ifndef RV32_OS_SCORE_H
#define RV32_OS_SCORE_H

#include <stdint.h>

uint32_t score_record(const char *game, uint32_t value);

#endif
