# F2: RV32F architectural contract

F2 integrates the F1 unit into the existing machine. The ISA is RV32IF with
Zicsr and the existing four trap CSRs/MRET, not a full privileged platform.
F is always enabled; FS, context switching, D, compressed instructions and
accelerators are outside this milestone. Existing RV32I firmware remains valid.

## State and instructions

There are 32 independent 32-bit floating registers; **f0 is writable**.
Reset clears all floating registers and fcsr. FLW/FSW use integer base addresses,
signed 12-bit offsets, and the existing word alignment/access/device rules.
Loads, stores, FMV.X.W, FMV.W.X and FSGNJ/N/X preserve NaN payload bits.
FCLASS writes the ten standard classification bits without setting flags.

Arithmetic includes FADD/FSUB/FMUL/FDIV/FSQRT.S, FMADD/FMSUB/FNMSUB/FNMADD.S,
FMIN/FMAX.S, FEQ/FLT/FLE.S and FCVT.W[U].S/FCVT.S.W[U]. The F1 numeric
contract applies, including canonical arithmetic NaNs, subnormals, tininess
after rounding, one-rounding FMA and saturated invalid integer conversions.

CSR addresses 0x001, 0x002, 0x003 alias fflags[4:0], frm[2:0], fcsr[7:0].
Other bits read zero and ignore writes. All six Zicsr forms operate atomically;
CSRRW[I] with rd=x0 suppresses reads; CSRRS/RC with rs1=x0 and immediate
set/clear with uimm=0 suppress writes. Nonzero register indices containing zero
still perform writes. Existing trap CSR semantics remain unchanged.

An instruction with an rm field accepts modes 0..4, or 7 resolving frm.
Reserved static modes 5/6 or resolved dynamic modes 5..7 trap as illegal
instructions before issuing work. Writing a reserved frm is legal. Instructions
without an rm field ignore frm, and use RNE for the standalone FPU interface.
Accrued IEEE flags are ORed into fflags at retirement, even when an integer
destination is x0. IEEE exceptions do not trap. Illegal instructions and failed
loads commit no floating result or flags; existing traps report the fault.

## Execution and observation

The CPU has one instruction and one FPU request outstanding. Operands and
resolved rounding are held through acceptance; the CPU waits for the response,
then commits the result and flags in WRITEBACK. Reset cancels both units and
prevents stale completion. No younger instruction executes during this wait.

The retirement port adds floating write enable/index/value, floating CSR write
enable and the resulting fcsr byte. Trace effects are ordered integer write,
floating write (` fN=XXXXXXXX`), floating CSR update (` fcsr=XX`), memory effect.
A floating CSR write is recorded even if the value is unchanged; arithmetic
records an update when its per-operation flags are nonzero. Integer-only trace
lines remain byte-identical. State dumps include f0..f31 and fcsr.

Float firmware uses `-march=rv32if_zicsr -mabi=ilp32` with separate build
objects. Arguments/results cross C call boundaries in integer ABI registers;
the compiler moves them into floating registers for arithmetic. No incompatible
hard-float ABI objects or host libraries are linked into guest firmware.

The emulator uses pinned Berkeley SoftFloat as host arithmetic, with independent
ISA decode and per-machine accrued state. It sets rounding, tininess and clears
temporary flags for every operation. Its synchronous API is single-threaded.
Literal expected results and F1's separate oracle protocol anchor differential
tests; sharing SoftFloat is not an independent second arithmetic oracle.

References: [F 2.2](https://docs.riscv.org/reference/isa/v20260120/unpriv/f-st-ext.html),
[Zicsr 2.0](https://docs.riscv.org/reference/isa/v20260120/unpriv/zicsr.html),
[F1 numeric/handshake contract](fp32.md).
