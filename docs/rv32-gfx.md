# G1: integer 2D accelerator

G1 adds CPU-commanded integer graphics, matched by RTL and an incremental emulator
device. This record specifies the contract and measured acceptance evidence.

The dedicated drawing engine shares RAM reads and owns the existing 320×240 RGB332
framebuffer while busy. SIMD4 remains unchanged. Commands are opaque fill (1), blit
(2), line (3), and filled triangle (4). There is one outstanding command, no queue,
interrupt, transparency, scaling, depth, or programmable stage.

## Registers

The non-executable 128-byte window at `0x20007000` accepts aligned word accesses.
Unlisted offsets/directions, parameter writes while busy, and START while busy are
side-effect-free CPU access faults. RESET is always legal.

| Offset | Access | Meaning |
| --- | --- | --- |
| 00 | W | COMMAND: exactly 1 START or 2 RESET |
| 04 | R | STATUS: IDLE=0, BUSY=1, DONE=2, FAULT=4 |
| 08 | R | Error: 0 none, 1 invalid command/parameters, 2 internal state |
| 0c | R | Busy device ticks, wrapping at 32 bits |
| 10 | R | Ticks waiting for memory acceptance |
| 14 | R | Accepted pixel reads |
| 18 | R | Accepted pixel writes |
| 40–7c | RW | Sixteen parameter words in the order below |

Parameters: OP, COLOR, X0, Y0, X1, Y1, X2, Y2, W, H, SRC, STRIDE, SW, SH, SX, SY.
Signed coordinates use full sign-extended 32-bit words in −1024…1023; dimensions
W/H are 0…2048. Color is 0…255 for every operation. Unused coordinate/dimension parameters are ignored. Fill/blit use X0/Y0
and W/H; line uses X0/Y0 and X1/Y1; triangle adds X2/Y2. START snapshots all words,
clears previous outcome/counters and validates before issuing any memory request.
Invalid parameters produce FAULT with ERROR=1; an unreachable internal-state
fallback uses ERROR=2. Empty operations produce DONE without transfers.

Blits additionally use signed SX/SY and source dimensions SW/SH (1…2048), with
SW ≤ STRIDE ≤ 65535. SRC is a byte address in configured RAM, or exactly
`0x30000000` with SW=320, SH=240, STRIDE=320 for framebuffer copies. The full
source extent `(SH-1)*STRIDE+SW` must fit RAM without address wrap. Source and
destination are clipped together. Overlap has snapshot semantics, implemented
by reverse traversal when destination follows source in the same framebuffer.

Lines use canonical lexicographic (x,y) endpoint order and inclusive integer
Bresenham: dx=abs(delta x), dy=abs(delta y), err=dx-dy; at each step retain the
old doubled error, advance x when e2≥−dy and y when e2≤dx. Clip generated pixels,
not endpoints. Triangle vertices are normalized to positive cross-product area.
Sample pixel centers; include an edge at zero only when dy<0 or (dy=0 and dx>0).
Degenerate triangles draw nothing. Integer bounds keep edge products within int32.

## Ownership and time

CPU framebuffer reads/writes and PRESENT fault while BUSY. Display metadata is
readable. During RAM blits, CPU writes intersecting the complete source extent
(including row padding) fault; CPU reads and instruction fetches continue. Other
RAM locations remain writable. A fair arbiter shares the single RAM port.
A graphics grant retains the port during backpressure: contended CPU RAM
accesses, including instruction fetches, wait without faulting until it releases.

Device RESET clears parameters, status and counters and cancels unaccepted
transfers. It preserves already accepted memory writes. Reset has priority over
same-edge engine acceptance. DONE follows the last accepted write, never merely
its issue. Parameter reads are allowed while busy. The emulator advances one
engine tick per executed instruction, including traps; RTL advances per clock.
Polling and counters therefore require results comparison, not retirement equality.
The emulator reports zero STALLS: only the RTL models RAM arbitration and injected
memory waits. The standalone native device bridge accepts holds for protocol tests.
Ownership checks use the BUSY value at the access, including SETUP. Immediately
following START with a forbidden access can therefore trap on one backend and
succeed on another for a very short, empty or invalid job. Portable guest code
polls until not BUSY before touching protected memory; cross-backend trap-order
comparisons deliberately keep tested jobs busy long enough on both backends.

`gpu_submit` returns false without mutation while BUSY. `gpu_wait` returns the
final status (IDLE, DONE or FAULT), or driver-only `GPU_TIMEOUT` (8). Its budget
counts polls, not ticks. FAULT leaves ERROR readable and permits a new submission.
Timeout saves status/reason/counters in `gpu_last_result()` before RESET; zero
budget takes that recovery path without polling. Diagnostic registers are read
sequentially while the engine can still progress, not as one atomic snapshot.
RESET clears all parameters,
so raw MMIO callers must rewrite them before START. `gpu_run` prints operation,
outcome, status, reason and counters on failure. Partially drawn output is never
a completed command after recovery.

## Guest demo and commands

`make run-rv32-capstone` opens the existing native window. Choose **2D DEMO**
with UP/DOWN and ENTER. SPACE selects CPU/device drawing, P pauses, R restarts,
ESC returns, and Q ends the session. The scene contains clipped rectangles,
shared-edge triangles, a clipped line, a RAM sprite, and an overlapping framebuffer
copy. All scene generation, reference rendering and checking run in guest C.
The host still only presents pixels and forwards input.

On accelerated frames the guest renders a RAM reference, submits the same scene,
waits, compares all 76,800 bytes, and adds labels only after the comparison.
Runtime state and the native reference stay device-free; the firmware boundary
provides the MMIO emitter. The framebuffer has exclusive device ownership only
while each command is busy, so CPU labels and PRESENT happen after completion.

```sh
make test-rv32-gfx test-rv32-gfx-verilator test-rv32-gfx-sanitize
make run-rv32-gfx-emu run-rv32-gfx-rtl run-rv32-gfx-rtl-verilator
make run-rv32-gfx-menu-emu run-rv32-gfx-menu-rtl run-rv32-gfx-menu-rtl-verilator
make bench-rv32-gfx waves-rv32-gfx
make lint-rv32-gfx synth-rv32-gfx lint-rv32-soc synth-rv32-soc
```

The bounded diagnostic checks every pixel after each operation and exercises
invalid commands, busy submission, zero-budget cancellation, nonzero budget
expiry with retained diagnostics, and relaunch. It prints
`PASS G1` and presents `frame 1 d8e316f6`. The eight-frame menu replay prints
`PASS 78d4a476`; software-native rendering independently supplies its expected
checkpoints. `--gpu-stall N` and `--gpu-seed N` add independent fixed or seeded
0–3-cycle graphics memory waits; CPU stalls use the existing options.

## From commands to gates

1. Seventeen CPU stores load sixteen parameter words and START. The decoder's
   128-byte select reaches a bank of 512 parameter flip-flops. The BUSY guard
   freezes that bank until completion; this supplies the command snapshot.
2. SETUP checks bounds and computes clipped bounds, winding, line increments,
   and blit direction. Comparators and multiplexers select the first coordinate.
   Signed products implement edge equations; bounded inputs keep them exact.
3. SCAN tests coverage. A fill, line or triangle proceeds to WRITE; a blit first
   enters READ and captures one source byte in an eight-bit register. WRITE holds
   address/data until acceptance, then ADVANCE updates coordinates or completes.
4. The framebuffer mux selects the CPU or graphics port. RAM reads share its
   existing word port with the CPU. A preference bit alternates contested grants;
   a second bit retains a graphics grant under backpressure. Source writes fault
   by testing each enabled CPU byte against the protected extent. The CPU can
   still fetch instructions, poll registers and access unrelated memory.

Address arithmetic is visible rather than hidden in host rendering: destination
is `0x30000000 + 320*y + x`; source is `SRC + STRIDE*(SY+y-Y0) + SX+x-X0`.
Byte enables select one lane of the existing 32-bit framebuffer word. No extra
framebuffer or RAM read port is synthesized. This first implementation uses
combinational integer products and byte transfers; incremental edge arithmetic
and packed spans are future optimization opportunities. Source extent validation
uses `SH*STRIDE + SW - STRIDE`, algebraically the same 64-bit result as
`(SH-1)*STRIDE + SW` including invalid SH=0. Both multiplicands now have only
32 live bits, avoiding the subtraction-induced wide multiplier without narrowing
the validation result.

Standalone synthesis: **52,164 generic cells, 1,139 flip-flops**, no latches.
The complete SoC with the existing 64-word synthesis RAM/framebuffer substitutions
has **164,861 generic cells, 22,981 flip-flops**. Those substitutions measure logic
cost; they cannot hold the real framebuffer and are not a runnable machine.
These are generic mapped counts, not FPGA utilization, physical timing or MHz.

## Measured costs

`make bench-rv32-gfx` builds the same four drawing jobs twice: CPU reference and
accelerator driver. Timer brackets include submission and completion polling;
parameter construction, scene validation, printing, and PRESENT are outside them.
The generated JSON separately records CPU instruction/register-store/FB traffic, engine
cycles, stalls and accepted reads/writes. Four independently calculated checkpoint
constants prevent a matching-but-wrong pair of backends from passing.

| Job | CPU drawing RTL cycles | Accelerator path RTL cycles | Engine cycles | Reads / writes |
| --- | ---: | ---: | ---: | ---: |
| Full 320×240 fill | 339,677 | 230,695 | 230,401 | 0 / 76,800 |
| 32×32 RAM blit | 294,490 | 4,638 | 4,352 | 1,024 / 1,024 |
| 51-pixel line | 4,796 | 439 | 154 | 0 / 51 |
| Triangle, 975 covered pixels | 92,520 | 5,250 | 4,976 | 0 / 975 |

RAM blit includes 255 arbitration wait cycles even with zero injected waits.
Adding two waits per CPU and graphics transfer gives accelerator-path costs of
384,498 / 8,660 / 735 / 7,404 cycles. The CPU baseline uses word-packed fills;
the device writes individual bytes, so its fill improvement is modest and
memory waits consume much of it. The simple software blit is a correctness
reference, not an optimized memcpy claim.

Emulator instruction intervals are different: CPU fill 80,044 versus device
230,463, since the emulator advances the device once per instruction. They are
not hardware speed measurements. No wall-clock emulator speedup is claimed.

## Inspected waves and exercises

The 10 ns test clock produces these observations in `build/gfx/`:

- `fill.vcd`: START at 355 ns, first write request at 375 ns for byte
  `0x30000141` (pixel 1,1), accepted at 385 ns. The third-to-fourth pixel address
  jumps from `0x30000143` to `0x30000281`, preserving the 320-byte row stride.
- The sixth fill byte commits at 535 ns. DONE appears at 545 ns, after the write,
  with six transfers and 19 busy ticks (`1 + 3*6`).
- `reset.vcd`: the third write is issued at 445 ns and held from 450 ns. RESET removes valid at
  460 ns before its accepting edge; status/counters clear at 465 ns. Two earlier
  writes survive. The next launch uses a different color and completes normally.
  Removing software-reset cancellation in a temporary RTL mutation changes the
  first checkpoint from `5ea7c2d4` to `e5b1ad8c`; the pixel check detects it.

Try predicting the first and last address of an overlapping blit, then reverse
its displacement. Split a rectangle along its diagonal and predict which triangle
owns the centers on that edge. Finally add two memory waits and calculate why
fill costs `1 + 5*pixels`, while triangle scan cycles include uncovered centers.

## Acceptance evidence

The directed/seeded byte-port corpus contains 211 total jobs: valid commands, invalid
descriptors, software/external resets and a final relaunch. Both simulators agree with the incremental C device
on framebuffer hashes and every counter. Valid commands also compare complete
C framebuffer bytes with an independent Python oracle and guest reference;
literal octant/tie cases and a two-triangle square partition anchor coverage.
The oracle independently counts accepted reads/writes; invalid jobs must leave
the entire framebuffer unchanged with zero transfers. The RTL harness reads all
registers and reset parameters through MMIO, and requires observed held reads,
held writes and cancellation of each. UBSan errors fail the sanitizer targets.
Malformed/empty fixtures are rejected. Whole-CPU tests exercise busy framebuffer
and PRESENT faults, protected source bytes, legal unrelated accesses, register
faults, and a trap that must advance the device.

The diagnostic passes on emulator, Icarus (15,507,039 clocks) and stalled
Verilator (20,225,414 clocks). The menu replay matches all eight checkpoints on the native reference,
emulator, actual SDL window, and independently stalled Verilator (38,269,264
clocks, 6,394,122 CPU instructions). Native menu tests run at -O0/-O2; sanitizers
cover reference anchors plus all 211 device/reference corpus jobs. Counts describe this revision
and workload, not architectural guarantees.

The full `make test-rv32` aggregate passes after the complete six-agent review fixes, covering
RV32I/F, QEMU, device/window, A2 and game regressions. The original game replay
retains `PASS ea60197e` and 68 checkpoints; nine menu checkpoints intentionally
change for the third entry, while the game state checksum is unchanged. It now
executes 2,356,102 instructions because code layout and startup storage changed.
The aggregate runs the G1 menu replay on Verilator and the complete pixel
diagnostic on both simulators; the optional `run-rv32-gfx-menu-rtl` Icarus target is available
separately. Counter, ALU, SAP8, standalone SIMD4 and FP32 checks also pass.
