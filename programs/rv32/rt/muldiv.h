/* Integer multiply and divide for RV32I, which has no M extension.
 *
 * Results follow the RISC-V M-extension definitions exactly, so future
 * MUL/DIV/REM hardware and these routines agree on every input, including
 * division by zero and the signed overflow case INT32_MIN / -1.
 */
#ifndef RV32_RT_MULDIV_H
#define RV32_RT_MULDIV_H

#include <stdint.h>

uint32_t rv32_mul(uint32_t a, uint32_t b);
uint32_t rv32_divu(uint32_t n, uint32_t d);
uint32_t rv32_remu(uint32_t n, uint32_t d);
int32_t rv32_div(int32_t n, int32_t d);
int32_t rv32_rem(int32_t n, int32_t d);

#endif
