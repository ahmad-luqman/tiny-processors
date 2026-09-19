# From C to instructions to memory

Start with the [RV32 machine contract](rv32.md), then keep [selfcheck.c](../programs/rv32/selfcheck.c), [start.S](../programs/rv32/start.S), and [link.ld](../programs/rv32/link.ld) open. The new idea is that a compiled function is not yet a program, and a linked program is not yet a computer: each step below adds one thing the previous one lacked.

## 1. One function

```c
unsigned gcd(unsigned a, unsigned b)
{
    while (b != 0) {
        unsigned t = a % b;
        a = b;
        b = t;
    }
    return a;
}
```

The self-check contains this function (as `static`, so the optimizer inlines it into `main`). To see it alone, put it in `gcd.c` and compile with the project's flags:

```sh
CLANG=/opt/homebrew/opt/llvm@22/bin/clang
$CLANG --target=riscv32-unknown-elf -march=rv32i -mabi=ilp32 -mcmodel=medlow -mno-relax \
  -O2 -ffreestanding -S -o gcd.s gcd.c
```

## 2. The ABI decides the registers

Before any instruction exists, the ILP32 calling convention has already decided where `a` and `b` arrive and where the result leaves:

| Register | ABI name | Role here |
| --- | --- | --- |
| `x10`, `x11` | `a0`, `a1` | Arguments `a` and `b`; `a0` also carries the result |
| `x1` | `ra` | Return address written by the caller's `jal`/`jalr` |
| `x2` | `sp` | Stack pointer, 16-byte aligned at every call |
| `x8` | `s0` | Callee-saved: a function may use it only if it restores it |
| `x5`–`x7`, `x28`–`x31` | `t0`–`t6` | Caller-saved temporaries, free to clobber |

Nothing in the hardware enforces this; it is a convention shared by every compiled function, our startup code, and the runtime routines.

## 3. What the compiler emits

`gcd.s`, trimmed of directives:

```asm
gcd:
        addi    sp, sp, -16
        sw      ra, 12(sp)          # this function calls another, so ra must survive
        sw      s0, 8(sp)           # it needs one value across that call, so it borrows s0
        beqz    a1, .LBB0_2
.LBB0_1:
        mv      s0, a1              # remember b
        call    __umodsi3           # a0 = a0 % a1 ... by calling a function
        mv      a1, a0              # b = t
        mv      a0, s0              # a = old b
        bnez    a1, .LBB0_1
        j       .LBB0_3
.LBB0_2:
        mv      s0, a0
.LBB0_3:
        mv      a0, s0
        lw      ra, 12(sp)
        lw      s0, 8(sp)
        addi    sp, sp, 16
        ret
```

Two things are visible that C hides. The frame exists only because of the call: `ra` and `s0` are spilled to the stack, sixteen bytes to keep `sp` aligned. And `%` became `call __umodsi3`, because RV32I has no remainder instruction (section 8).

## 4. The object file is not a program

Assemble the same source to an object and disassemble it with relocations:

```sh
$CLANG --target=riscv32-unknown-elf -march=rv32i -mabi=ilp32 -mcmodel=medlow -mno-relax \
  -O2 -ffreestanding -c -o gcd.o gcd.c
/opt/homebrew/opt/llvm@22/bin/llvm-objdump -d -r gcd.o
```

```text
00000000 <gcd>:
       0: ff010113      addi   sp, sp, -0x10
       4: 00112623      sw     ra, 0xc(sp)
       8: 00812423      sw     s0, 0x8(sp)
       c: 02058063      beqz   a1, 0x2c <gcd+0x2c>
      10: 00058413      mv     s0, a1
      14: 00000097      auipc  ra, 0x0
                        R_RISCV_CALL_PLT  __umodsi3
      18: 000080e7      jalr   ra
      ...
```

`gcd` sits at address 0 because the object has no idea where it will live. The `call` became `auipc ra, 0` followed by `jalr ra`: the two instructions reserve room for a 32-bit PC-relative offset whose value is unknown, so the assembler attached a relocation record, `R_RISCV_CALL_PLT __umodsi3`, meaning "when you learn where `__umodsi3` is, patch these two instructions". `llvm-nm -u gcd.o` lists `__umodsi3` as undefined. An object file has code but no addresses, no entry point, no memory, and no devices. It cannot run.

## 5. Linking gives everything an address

`link.ld` declares one memory region, RAM at `0x8000_0000`, and lays sections into it in order: `.text` starting with `start.o`'s `.text.init`, then `.rodata`, `.data`, `.bss`, with the stack at the top of the first 256 KiB. The linker resolves every relocation and writes `build/rv32/selfcheck.map`, which records the decision for each input:

```text
80000000  5c   build/rv32/start.o:(.text.init)
80000000  30           _start
8000005c  9cc  build/rv32/selfcheck.o:(.text)
8000005c  8a0          main
80000a28  210  build/rv32/console.o:(.text)
80000c38  f0   build/rv32/muldiv.o:(.text)
80000ccc  4c           rv32_remu
80000ccc  4c           __umodsi3
80000d28  1d   .rodata
80000d48  10   .data
80000d58  20   .bss
```

Now `__umodsi3` is a number, `0x80000ccc`, and it is the same number as `rv32_remu` because the runtime defines one as an alias of the other. `_start` is at exactly `0x8000_0000` because `KEEP(*(.text.init))` is the first line of `.text`; the image checker refuses any other arrangement. With `-mno-relax` the linker did not shorten any sequence, so `build/rv32/selfcheck.lst` shows the same instructions as the objects, only with the blanks filled in.

## 6. The bytes in memory

The first lines of `build/rv32/selfcheck.hex` are the first words of RAM:

```text
00040117
00010113
00001297
d5028293
```

Decode `0x00040117` by fields (bit 0 on the right): opcode `0010111` is `auipc`; `rd` = `00010` = `x2` = `sp`; the upper twenty bits are `0x00040`. So `sp = pc + (0x00040 << 12) = 0x80000000 + 0x40000 = 0x80040000`, which is `_stack_top`. The next word `0x00010113` is `addi sp, sp, 0`, the second half of the `la` expansion that happened to need no low part. Two instructions, eight bytes, and the machine has a stack.

The same word in `build/rv32/selfcheck.bin` is stored little-endian: `xxd -l 8 build/rv32/selfcheck.bin` prints `17 01 04 00 13 01 01 00`. The least significant byte comes first; check 27 of the self-check observes the same thing from inside the guest.

Constants for devices take the same route. `rv32_exit` begins `lui t0, 0x100`: `0x100 << 12 = 0x00100000`, the done register. Every address in the contract is an immediate somewhere in the listing.

## 7. What happens at reset

1. QEMU (`-bios none`) starts at its mask ROM, `0x1000`, which jumps to `0x8000_0000`. On our own machine the PC simply resets there.
2. `_start` sets `sp`, zeroes `.bss` (`__bss_start` to `__bss_end`), and calls `main`. There is no copying of `.data`: the image was loaded exactly where it was linked.
3. `main` runs the checks. Each `rv32_putc` polls the console status byte at `0x1000_0005` and stores one byte to `0x1000_0000`; QEMU's UART forwards it to stdout.
4. `main` returns a code; `rv32_exit` stores `0x5555` or `(code << 16) | 0x3333` to `0x0010_0000`. QEMU's test device ends the process with that code as the exit status.

You can watch all of this. The driver saves the console bytes to `build/rv32/selfcheck.transcript`, and `qemu-system-riscv32 ... -d in_asm -D trace.log` writes every executed instruction, including the ROM stub and the first fetch at `0x8000_0000`.

## 8. Why `*` calls a function

RV32I has forty instructions and none of them multiply or divide; those belong to the M extension. When the target lacks M, clang lowers each 32-bit `*`, `/`, `%` to a call named after the routine GCC's `libgcc` has provided for decades: `__mulsi3`, `__divsi3`, `__udivsi3`, `__modsi3`, `__umodsi3` (`si` for a 32-bit "single integer", `3` for three operands). Normally the compiler's runtime library supplies them, but Homebrew's LLVM ships no RISC-V compiler-rt, so the link would fail with undefined symbols. [muldiv.c](../programs/rv32/rt/muldiv.c) supplies them: shift-and-add multiplication, restoring division with an explicit 33rd bit, and aliases from the `rv32_*` names to the libcall names. `llvm-objdump -d -r build/rv32/muldiv.o` shows no call relocations at all, which is the proof that the routines do not accidentally call themselves through a `*` or `%` of their own.

The signed routines are the exercise below. Their contract is the RISC-V M extension's, so that hardware we build later and these routines never disagree.

## 9. Exercises

1. Implement `rv32_div` and `rv32_rem` in `muldiv.c` using `udivmod()` on magnitudes. Predict, before running, what `-7 / 2`, `-7 % 2`, `7 / -2`, `INT32_MIN / -1`, and `x / 0` must return, then run `make test-rv32-rt` and finally `make test-rv32`. The passing line is `PASS 807d9fad`.
2. Change one expected constant in `selfcheck.c`, say check 12's `97406784u`. Predict the console line and the exit status (`make run-rv32-qemu; echo $?`), then restore it.
3. Rebuild with `-O0` (`make RV32_CFLAGS="..." ...` or edit the Makefile locally) and compare `fib`'s frame in the listing with the `-O2` version. How many bytes does each recursion level use?
4. Change `ORIGIN` in `link.ld` to `0x80001000`. Predict what the image checker says, and what QEMU would do if you bypassed it (the PC still starts at `0x8000_0000`, where there is now nothing).
5. Remove `-mno-relax`, rebuild, and diff the listings. Which `auipc`/`addi` pairs disappeared, and what did the linker need to know to remove them?

The firmware milestone ends here. The next planned artifact is the headless emulator (M2), which will load `selfcheck.elf` with the same parser the image checker uses and must reach the same console line and done word.
