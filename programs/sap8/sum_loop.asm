; Sum 3 + 2 + 1. Separate data memory holds one, count, and sum.
; Output: 6. Final data[0xf1] = 0 and data[0xf2] = 6.
.data 0xf0 1
.data 0xf1 3
.data 0xf2 0

loop:
    LDA 0xf1
    JZ done
    ADD 0xf2
    STA 0xf2
    LDA 0xf1
    SUB 0xf0
    STA 0xf1
    JMP loop
done:
    LDA 0xf2
    OUT
    HLT
