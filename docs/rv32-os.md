# Track 2: a real OS, one step at a time

Track 2 turns the machine's polling runtime into an operating system in five
steps, each with its own evidence: interrupts (O1), a kernel with system calls
and separately linked programs (O2), storage (O3), preemptive multitasking
(O4) and protection (O5). The [plan](planning/track2-os.md) fixed the
contract; [docs/rv32.md](rv32.md#behavior-fixed-in-track-2) holds it now. This
record explains each step and what was run to accept it.

Every hardware change is made twice, in the emulator
([rv32emu_core.c](../tools/rv32emu_core.c)) and the RTL, and compared. Every
new device sits at QEMU `virt`'s address in `virt`'s register layout, so the
programs that exercise it also run on QEMU as an independent reference.

## O1: interrupts

### What changed

| Piece | Emulator | RTL |
| --- | --- | --- |
| `mstatus`, `mie`, `mip`, `mscratch` | `csr_read`/`csr_write` | four registers and the `csr_old` mux in [rv32.v](../rtl/rv32/rv32.v); decode admits the numbers |
| Interrupt entry | checked at the top of `step()` | taken in `FETCH` in place of a fetch not yet presented |
| `wfi` | retires at once | `WFI_WAIT` state until `mip & mie` (cycle ticks) |
| CLINT levels | `mip_now()` | `mtip`, `msip_level` outputs of [rv32_clint.v](../rtl/rv32/rv32_clint.v) |
| PLIC | `plic_load`/`plic_store` | [rv32_plic.v](../rtl/rv32/rv32_plic.v), a new bus slave |
| Input interrupt line | `COUNT` nonzero | `nonempty` output of [rv32_input.v](../rtl/rv32/rv32_input.v) |
| Step ticks | (always) | `step_ticks` input: CLINT and `cycle` count the core's retire/trap pulses |

### Taking an interrupt

The privileged specification lets an implementation take an interrupt at any
instruction boundary. Both backends choose the same one: the boundary before
the next fetch. The emulator checks at the top of `step()`: if `mstatus.MIE`
is set and `mip & mie` is nonzero, it takes the highest-priority one (MEI,
then MSI, then MTI) instead of executing the instruction at `pc`, and the
entry counts as a step.

The RTL does the same in `FETCH`, with one care: a memory request may not
change once it is presented (the [transaction
contract](rv32.md#memory-transaction-contract)). The core therefore decides
only in the first cycle of `FETCH` (`fetch_waiting` is clear): `irq_take`
gates `mem_valid` off in that cycle and runs the trap entry instead, which
costs one cycle. A fetch already waiting on a slow memory completes first.
The trap-entry task that exceptions used gained an `interrupt` input; it sets
`mcause`'s top bit, moves MIE to MPIE and clears MIE, and reports the entry on
the retirement port with a new `trap_interrupt` flag, from which the
testbench prints the trace line `N pc 00000000 interrupt 7`.

`wfi` is the one instruction whose behavior differs by backend. On the RTL
with cycle ticks, `EXECUTE` sends it to `WFI_WAIT`, where it stays until an
enabled level (`mip & mie`, whatever MIE says) appears, then retires; the
interrupt, if MIE is set, is taken at the next boundary with `mepc` after the
`wfi`. On the emulator time is instructions, so a waiting `wfi` would wait
forever: it retires at once. Both are allowed, and both run the same loop,
`while (!done) wfi;`.

### The PLIC

[rv32_plic.v](../rtl/rv32/rv32_plic.v) is a priority encoder and three small
register sets. Pending is combinational, `lines & WIRED & ~claimed`, because
QEMU's PLIC treats our kind of source as level-triggered and drops a request
whose line falls. The claim is a loop over 31 sources that keeps the first
strictly greater priority, so the lowest number wins a tie, starting from the
threshold, so only priorities above it count. A read of the claim register
marks the answer claimed at acceptance; writing the number back while it is
enabled clears the mark. `meip` is `best != 0`.

Only the input queue is wired (source 12, the first number `virt` leaves free
after its RTC at 11); O3 will wire virtio-blk as source 1, `virt`'s number for
its first virtio slot. The device tree now describes the wiring the way
QEMU's does: the hart has a `riscv,cpu-intc` (phandle 1), the CLINT lists
`interrupts-extended = <1 3 1 7>`, the PLIC `<1 11>` and `riscv,ndev = 31`,
and the input node `interrupt-parent = <2>; interrupts = <12>`. `fdt_cell`
([fdt.c](../programs/rv32/fdt.c)) reads such a cell, and the map checker now
requires our PLIC to equal `virt`'s.

### Device time and step ticks

A timer interrupt fires when `mtime` reaches `mtimecmp`, and `mtime` counts
cycles on the RTL and instructions on the emulator, so the interrupt lands on
a different instruction on each. Two comparisons result:

- **Results** (cycle ticks, the default): console, outcome, checkpoints and
  exception records must agree. Interrupt lines are left out of the records.
- **Traces** (step ticks, `--ticks steps`): the testbench's `+ticks=steps`
  drives the SoC's `step_ticks` input. The CLINT then advances on the core's
  registered `retire || trap` pulse instead of every cycle, and the core's
  `cycle` counter does too. The pulse is high in the first `FETCH` cycle
  after a step, and the count of that cycle already includes it
  (`mtime = elapsed + pulse`), so the interrupt check in that cycle sees the
  same `mtime` as the emulator's check before the next step, and every load
  or CSR read sees the steps before its instruction. `wfi` does not wait.
  Timer reads and interrupts then happen at the same instruction on both
  backends, and the whole trace, interrupt lines included, is compared.

The input interrupt needs no special mode: events arrive with the present
that reaches their frame on both backends, and the testbench's first push
lands before the next instruction's first `FETCH` cycle, where the
combinational pending line reaches `meip` in time.

### irqcheck

[irqcheck.c](../programs/rv32/irqcheck.c) finds the CLINT and PLIC in the
tree, installs [trap_entry.S](../programs/rv32/trap_entry.S) and checks the
registers, the gating (a pending timer interrupt is not taken while `mie` or
MIE is clear, the handler sees MIE clear and MPIE set, `mret` restores MIE), a
software interrupt, MSI before MTI, a `wfi` loop woken by a timer interrupt
2,000 ticks ahead, and the PLIC's registers. Where the tree lists our input
device it also masks the input with the threshold, then takes MEI before MSI,
claims source 12, drains the scripted events and completes. On QEMU:

```
irqcheck: registers ok
irqcheck: gating ok
irqcheck: software ok
irqcheck: priority ok
irqcheck: wfi ok
irqcheck: plic ok
irqcheck: keyboard absent
PASS 133cab46
```

On our backends the keyboard line is `irqcheck: keyboard source 12 events 2`.
The PASS word folds only what every platform shares.

### Evidence

Run on 2026-09-30 in a Linux container (x86-64): Ubuntu clang and lld 18.1.3,
QEMU 8.2.2, Icarus 13.0 and Verilator 5.040 built from their release tags,
Yosys 0.33.

- **irqcheck:** `PASS 133cab46` on QEMU `virt` (`-cpu rv32`, transcript equal
  to the pinned one), the emulator (56,553 steps, 7 interrupts), Icarus
  (231,595 cycles) and Verilator with a stall per request (297,542 cycles),
  results-identical with cycle ticks; the RTL's `wfi` waits, so it retires
  54,579 instructions against the emulator's 56,553. In step-tick mode on
  Verilator with seeded random stalls the traces are identical, 56,553 lines
  including the 7 interrupt lines.
- **Directed tests:** `test-rv32-irq`, 11 programs on the emulator and
  Verilator in step-tick mode, each compared trace for trace and against
  values written by hand: the CSRs' reset values and writable bits; a timer
  interrupt taken at the next boundary with `mepc`, `mstatus` in the handler
  and after `mret`; MSI before MTI; an input interrupt claimed, drained and
  completed; the PLIC's registers, faults and claim/complete protocol; `mtime`,
  `time` and `cycle` reading the steps before their instruction; `wfi`
  retiring once on the RTL with cycle ticks and spinning on the emulator; an
  interrupt into an unmapped handler as a double fault; and interrupts under
  seeded stalls.
- **Earlier results:** every earlier image keeps its PASS word and its
  trace; the architectural tests' `csrs mstatus` now retires on every backend
  as on QEMU, so the model's handler skips nothing and fails on any trap.

## Exercises (O1)

1. **Where does it land?** Run `make run-rv32-irq-rtl-verilator` and
   `run-rv32-irq-rtl-steps`, then find the first `interrupt 7` line in each
   RTL trace and in the emulator's. Why do the step-tick and emulator lines
   agree, and why does the cycle-tick one not?
2. **A fetch in flight.** In [rv32.v](../rtl/rv32/rv32.v), what would go wrong
   if `irq_take` ignored `fetch_waiting`? Run the directed tests with
   `--seed` stalls after removing it and read the testbench's complaint.
3. **Level or edge.** Make the PLIC latch pending instead of following the
   line. Which `irqcheck` step behaves differently if the handler drains the
   queue without claiming?
4. **Priority.** Give the input priority 1 and the threshold 1. Where does the
   specification say this masks the source, and which line of `rv32_plic.v`
   implements it?
