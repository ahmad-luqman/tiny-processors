/* Decimal digits without a divide instruction; see udecimal.c. */
#ifndef RV32_OS_UDECIMAL_H
#define RV32_OS_UDECIMAL_H

#include <stdint.h>

uint32_t u_decimal(uint32_t value, char digits[11]); /* the digits end at digits[10] = 0; returns where they start */

#endif
