# A2: RV32-commanded SIMD4

The RV32 CPU loads private accelerator memories through its bus, launches the
four-lane A1 engine, polls for completion, and reads the result. There is no DMA,
interrupt, shared-RAM arbitration or coherent cache. The standalone engine's
instruction and arithmetic contract in [simd4.md](simd4.md) is unchanged.

## Device contract

All accesses are aligned 32-bit words and no window is executable. Misalignment
traps before decode; other invalid accesses are load/store access faults without
side effects. Unlisted offsets and directions are invalid.

| Address | Direction | Meaning |
| --- | --- | --- |
| `0x20004000` | write | COMMAND: exactly 1 START, exactly 2 RESET |
| `0x20004004` | read | STATUS: bit 0 BUSY, bit 1 DONE, bit 2 FAULT; other bits zero |
| `0x20004008` | read/write | ENTRY: program word index 0–255; larger writes fault |
| `0x2000400c` | read | CYCLES: busy device ticks, including final halt/fault |
| `0x20004010` | read | STALLS: ticks waiting on the engine's data port |
| `0x20004014` | read | TRANSFERS: accepted engine reads and writes |
| `0x20004018` | read | INSTRUCTIONS: successful engine retirements, including HLT |
| `0x20005000`–`0x200053ff` | read/write | 256 program words, four bytes per word |
| `0x20006000`–`0x200063ff` | read/write | 256 data slots, four bytes per 16-bit word |

Data-slot reads zero-extend; writes discard the upper 16 bits. This CPU slot
addressing does not change the SIMD4 engine's eight-bit word addresses.

START captures ENTRY and clears previous DONE/FAULT, counters, registers,
accumulators, loop and execution state. It is accepted only while idle. While
busy, CPU reads/writes of either memory and writes to ENTRY or START fault;
reads of registers and RESET remain available. A request on the completion edge
still observes busy and is rejected. The next request can access the buffers.
Invalid CPU accesses do not change device status. Engine illegal/invalid-loop
instructions finish with DONE and FAULT; successful HLT sets DONE. Both bits
remain until START or RESET. Counters wrap modulo 2^32.

Global reset and COMMAND RESET clear ENTRY and execution/status/counters. Reset
has priority over any engine transfer on that edge, cancels an unaccepted
request, and never rolls back accepted stores. Neither reset clears program or
data memory. Power-up contents are unspecified; firmware initializes every
location it uses. The harness starts memories at zero to match emulator storage.

Side effects occur only at acceptance: CPU `valid && ready && !error`, engine
`mem_valid && mem_ready`. While stalled, request fields stay stable. Each lane's
store is a separate transfer, so a reset/fault may leave partial output.

## Device time and comparison

RTL advances on each clock. The emulator advances one FETCH/EXECUTE/MEMORY phase
at the end of each executed CPU instruction, including traps, with one lane
transfer per MEMORY tick. A successful START or RESET suppresses advancement
for that CPU instruction. CPU loads observe state before that tick. Emulator
memory has zero waits; RTL test inputs can delay CPU and engine memory
independently. Performance counters measure device ticks, not CPU instructions.

Completion polling produces different CPU traces across backends. Result-level
comparison requires observed timer reads or accelerator register accesses;
console, completion and ordered trap records must still match. Existing
trace-identical firmware keeps strict retirement comparisons.
