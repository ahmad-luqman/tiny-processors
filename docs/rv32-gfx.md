# G1: integer 2D accelerator

Work in progress on G1. This contract precedes implementation; acceptance evidence
is recorded below only after verification.

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
| 04 | R | STATUS: BUSY=1, DONE=2, FAULT=4 |
| 08 | R | Error: 0 none, 1 invalid command/parameters |
| 0c | R | Busy device ticks, wrapping at 32 bits |
| 10 | R | Ticks waiting for memory acceptance |
| 14 | R | Accepted pixel reads |
| 18 | R | Accepted pixel writes |
| 40–7c | RW | Sixteen parameter words in the order below |

Parameters: OP, COLOR, X0, Y0, X1, Y1, X2, Y2, W, H, SRC, STRIDE, SW, SH, SX, SY.
Signed coordinates use full sign-extended 32-bit words in −1024…1023; dimensions
W/H are 0…2048. Color is 0…255. Unused parameters are ignored. Fill/blit use X0/Y0
and W/H; line uses X0/Y0 and X1/Y1; triangle adds X2/Y2. START snapshots all words,
clears previous outcome/counters and validates before issuing any memory request.
Invalid parameters produce FAULT; empty operations produce DONE without transfers.

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

Device RESET clears parameters, status and counters and cancels unaccepted
transfers. It preserves already accepted memory writes. Reset has priority over
same-edge engine acceptance. DONE follows the last accepted write, never merely
its issue. Parameter reads are allowed while busy. The emulator advances one
engine tick per executed instruction, including traps; RTL advances per clock.
Polling and counters therefore require results comparison, not retirement equality.

The driver returns failure for BUSY submission, command fault, or timeout. Timeout
resets the engine; zero budget resets immediately. Guest software must not treat
partially drawn output as a completed command after recovery.
