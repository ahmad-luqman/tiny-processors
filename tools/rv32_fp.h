/* Host-only exact arithmetic adapter; no guest or RTL dependency. */
#ifndef RV32_FP_H
#define RV32_FP_H
#include <stdint.h>
enum { OP_ADD=0, OP_SUB=1, OP_MUL=2, OP_FMADD=3, OP_FMSUB=4, OP_FNMSUB=5, OP_FNMADD=6, OP_DIV=7, OP_SQRT=8, OP_I32_TO_F32=9, OP_U32_TO_F32=10, OP_F32_TO_I32=11, OP_F32_TO_U32=12, OP_EQ=13, OP_LT=14, OP_LE=15, OP_MIN=16, OP_MAX=17 };
uint32_t rv32_fp(unsigned op, unsigned rm, uint32_t a, uint32_t b, uint32_t c, uint8_t *flags);
#endif
