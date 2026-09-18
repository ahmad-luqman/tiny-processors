# SIMD4: vector-add compute milestone

## Plan

1. Define shared control, lane registers, integer/address rules, and the memory handshake.
2. Implement parameterized SIMD RTL and compare every retired instruction and memory transfer with a Python interpreter. Verify launch, completion, faults, backpressure, and reset; commit a working core.
3. Run the same vector-add workload with one, two, and four lanes and different memory delays. Save cycle/stall reports, waveforms, and explanations; preserve and regression-test the earlier projects.
4. Complete the vector-add MVP and pause. Multiplication and matrix kernels are follow-on work.

## Machine and launch contract

`simd4` defaults to four lanes; supported `LANES` values are 1, 2, and 4. Each lane has four independent 16-bit registers (`r0`–`r3`). All lanes share an eight-bit PC, a 32-bit instruction register, and a 16-bit loop counter. There are no per-lane branches, flags, active masks, or divergent control flow.

The program interface reads one of 256 **32-bit words** combinationally. The separate data interface addresses 256 **16-bit words** through one shared port. Addresses and PC wrap modulo 256; ADD/ADDI wrap modulo 65536. Signed and unsigned addition have identical low 16 result bits. There is no multiply instruction or multiply-result truncation in this MVP.

On a rising edge with `start=1` and `busy=0`, the core captures `entry_pc`, clears all lane registers, loop/trace/performance state, and starts fetching. `start` while busy is ignored. `done` and `fault` are sticky until a new accepted launch or reset. HLT sets done; illegal opcodes and invalid loop operations set done and fault. Pulse start for one cycle: holding it high can launch again on the first idle edge after completion.

Reset is synchronous, active high, and has priority over launch/execution. It clears registers, counters, status, and pending work. Memory requests are gated off while reset is asserted. Completed stores remain in memory; reset cancels an unaccepted request, without rollback. The core does not initialize or clear external memories.

## Instruction words

Fields: `[31:24] opcode`, `[23:22] rd`, `[21:20] ra`, `[19:18] rb`, `[17:16] unused`, `[15:0] immediate`. Unused fields are ignored; program builders emit zeros. All operations apply to every lane unless they affect shared control.

| Opcode | Instruction | Effect |
| --- | --- | --- |
| `00` | HLT | Finish successfully |
| `01` | LDI rd, imm | Broadcast an immediate into each lane's rd |
| `02` | LANE rd | rd gets physical lane ID: 0 through LANES−1 |
| `03` | ADD rd, ra, rb | Per lane, rd = ra + rb, modulo 65536 |
| `04` | ADDI rd, ra, imm | Per lane, rd = ra + immediate, modulo 65536 |
| `05` | LOAD rd, ra, offset | rd = memory[(ra + offset) mod 256] |
| `06` | STORE rd, ra, offset | memory[(ra + offset) mod 256] = rd |
| `07` | SETLOOP count | Set the shared loop counter; zero faults |
| `08` | LOOP target | If counter > 1, decrement and jump to target's low byte; if 1, clear and fall through; if 0, fault |

Opcodes `09`–`ff` fault. Faulting instructions do not retire or write lane registers/memory. Their fetch has already incremented PC. Registers `rd`, `ra`, and `rb` may alias; arithmetic captures results from the pre-edge source values. Stores visit lanes in ascending order, so if addresses alias the highest-numbered lane writes last.

## Memory handshake and execution phases

States are IDLE (0), FETCH (1), EXECUTE (2), and MEMORY (3). Launch enters FETCH. FETCH captures the instruction and its address, increments PC, then enters EXECUTE. Ordinary instructions commit on the next edge. LOAD/STORE instead enter MEMORY, starting at lane zero. Each accepted transfer advances to the next lane; the last transfer retires the instruction and returns to FETCH.

`mem_valid` identifies a request. `mem_write`, `mem_address`, and `mem_write_data` describe it. A transfer occurs only on a rising edge where `mem_valid && mem_ready`; reads sample `mem_read_data`, and writes commit at that edge. The responder must not apply side effects without a transfer. Read data must be valid at the accepting edge. Only one request is outstanding: this is a combined request/completion handshake, not a separate pipelined response channel.

While valid and not ready, the core holds address, direction, write data, and lane selection stable. LOAD fills one lane at a time, but no subsequent instruction executes until all lanes have loaded. STORE may therefore be partially complete when reset aborts a kernel. No instruction-level memory atomicity is promised.

`retired` pulses after a successful instruction completes, including HLT; `retire_pc` and `retire_instruction` identify it. `register_state` exposes all lane registers for inspection, lane 0/r0 in the least significant 16 bits. Snapshots are checked after nonblocking assignments settle.

Performance counters are 32-bit, reset on each accepted launch, and wrap modulo 2^32:

- `cycles`: every busy edge, including the final halt/fault edge, excluding launch and idle edges.
- `stalls`: MEMORY edges with valid request and ready low.
- `memory_transfers`: accepted reads plus writes.
- `instructions`: successfully retired instructions, including HLT.

With no resets, `cycles = 2 × attempted_instructions + memory_transfers + stalls`. A faulting instruction counts as an attempt but does not increment the retired-instruction counter. The 10 ns testbench clock is a simulation convenience, not a measured hardware frequency.

## Run and verify

```sh
make test-simd4            # Python model tests; Icarus for 1, 2, and 4 lanes
make test-simd4-verilator  # Same suite through compiled simulation, plus waves
make lint-simd4            # Strict RTL lint for all three configurations
make synth-simd4           # Four-lane core netlist, statistics, and latch check
make bench-simd4           # Fixed-size vector with lane-count and ready-delay sweep
make waves-simd4           # Tests, short waveforms, and Surfer link
```

Python uses only the standard library. The [kernel builder](../programs/simd4/vector_add.py) spells out nine instruction words with readable comments. The [Python interpreter](../tools/simd4_model.py) produces expected register/PC/loop snapshots, ordered memory transfers, and the complete final memory. The [runner](../tools/simd4_run.py) generates fixtures and launches the [HDL testbench](../tests/simd4_tb.sv). It separately checks the vector result with a direct Python list calculation.

`test-simd4` and `test-simd4-verilator` each pass 312 cases and 329 completed launches, with 12 additional reset-aborted prefixes. Coverage includes every illegal opcode, invalid loop counts, arithmetic/address/PC wraparound, register aliases, colliding stores, shared loops, ignored starts while busy, sticky done/fault, relaunch without reset, variable backpressure, partial-load reset, partial-store reset, and deterministic mixed instruction sequences. The testbench compares every retirement and accepted transfer, all final memory words, stalled request stability, and performance counters. Six Python tests pin encodings and independently check reference semantics and input validation.

Kernel lengths must be 1–64 and divisible by the chosen lane count: every lane is active. Each lane processes `lane_id`, `lane_id + LANES`, and so on. A and B start at data word addresses `00` and `40`; C starts at `80`. No tail masks are implemented. Generated images, traces, logs, and JSON reports live under `build/simd4/icarus/` and `build/simd4/verilator/`.
