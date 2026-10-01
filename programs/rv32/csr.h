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
/* O5: the counters user mode may read, and PMP (eight entries). */
#define CSR_MCOUNTEREN 0x306
#define CSR_PMPCFG0    0x3a0
#define CSR_PMPCFG1    0x3a1
#define CSR_PMPADDR0   0x3b0
#define CSR_PMPADDR1   0x3b1
#define CSR_PMPADDR2   0x3b2
#define CSR_PMPADDR3   0x3b3
#define CSR_PMPADDR4   0x3b4
#define CSR_PMPADDR5   0x3b5
#define CSR_PMPADDR6   0x3b6
#define CSR_PMPADDR7   0x3b7

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
/* A PMP configuration byte (O5): permissions, the address-matching mode and the lock. */
#define PMP_R     0x01u
#define PMP_W     0x02u
#define PMP_X     0x04u
#define PMP_TOR   0x08u
#define PMP_NA4   0x10u
#define PMP_NAPOT 0x18u
#define PMP_L     0x80u
/* Issue #20: S-mode and Sv32. */
#define CSR_SSTATUS    0x100
#define CSR_SIE        0x104
#define CSR_STVEC      0x105
#define CSR_SCOUNTEREN 0x106
#define CSR_SSCRATCH   0x140
#define CSR_SEPC       0x141
#define CSR_SCAUSE     0x142
#define CSR_STVAL      0x143
#define CSR_SIP        0x144
#define CSR_SATP       0x180
#define CSR_MEDELEG    0x302
#define CSR_MIDELEG    0x303

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
