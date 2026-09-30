/* CSR access from C (O1): the numbers of the machine CSRs this machine has and
 * the csrr/csrw/csrs/csrc instructions as inline assembly. The CSR number is a
 * compile-time constant in every use, as the instruction encodes it. */
#ifndef RV32_CSR_H
#define RV32_CSR_H

#include <stdint.h>

#define CSR_MSTATUS  0x300
#define CSR_MIE      0x304
#define CSR_MTVEC    0x305
#define CSR_MSCRATCH 0x340
#define CSR_MEPC     0x341
#define CSR_MCAUSE   0x342
#define CSR_MTVAL    0x343
#define CSR_MIP      0x344

#define MSTATUS_MIE  0x8u
#define MSTATUS_MPIE 0x80u
#define MSTATUS_MPP  0x1800u
#define IRQ_MSI 3u
#define IRQ_MTI 7u
#define IRQ_MEI 11u
#define MIP_MSIP (1u << IRQ_MSI)
#define MIP_MTIP (1u << IRQ_MTI)
#define MIP_MEIP (1u << IRQ_MEI)
#define MCAUSE_INTERRUPT 0x80000000u

#define STR_(x) #x
#define STR(x) STR_(x)
#define csr_read(csr) ({ uint32_t v_; __asm__ volatile("csrr %0, " STR(csr) : "=r"(v_)); v_; })
#define csr_write(csr, value) __asm__ volatile("csrw " STR(csr) ", %0" :: "r"((uint32_t)(value)) : "memory")
#define csr_set(csr, bits) __asm__ volatile("csrs " STR(csr) ", %0" :: "r"((uint32_t)(bits)) : "memory")
#define csr_clear(csr, bits) __asm__ volatile("csrc " STR(csr) ", %0" :: "r"((uint32_t)(bits)) : "memory")

static inline void wait_for_interrupt(void)
{
    __asm__ volatile("wfi" ::: "memory");
}

#endif
