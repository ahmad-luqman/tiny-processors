# M7: the first runtime and Tetris

## Behavior contract

One RV32I image boots to a two-entry menu. UP/DOWN selects Pong or Tetris;
ENTER starts a fresh game. ESCAPE returns to the menu. Q ends the session
with `PASS <state checksum>`. P pauses and R restarts a game. At game over,
the result remains for 180 presented frames (R can restart), then the menu
returns. Standalone M6 Pong keeps its original controls and recording.

Each iteration drains queued events, checks quit, reads KEYS, advances one
logical frame, draws, and presents. Events delivered by present N affect
iteration N+1. A screen transition consumes the rest of that event batch;
held controls are blocked until released. No game reads the device timer.
The window paces presents at 60 fps; backend clock rates do not change rules.

| Tetris behavior | Rule |
| --- | --- |
| Board | 10 columns × 20 rows, all visible; no hidden rows |
| Piece IDs | I, O, T, S, Z, J, L (0 through 6) |
| Spawn | Rotation 0, bounding-box origin (3,0); occupied cells must fit |
| Rotation | UP, clockwise in a fixed 4×4 box for I, 3×3 for others; O unchanged; no kicks |
| Motion | LEFT/RIGHT immediate, first repeat after 12 frames, then every 4; opposing keys cancel |
| Gravity | One downward move every 30 active frames; DOWN changes interval to 3 |
| Drop/lock | SPACE drops to the lowest legal row and locks; a blocked gravity move locks immediately |
| Clear | Remove all full rows simultaneously; compact survivors downward, zero the top |
| Score | 100/300/500/800 for 1/2/3/4 lines; no drop bonus; score and line counts saturate at UINT32_MAX |
| Sequence | xorshift32 (13,17,5), Fisher–Yates seven-bag using remainder; default seed 1, zero maps to 1 |
| Restart | Empty board, zero counters/score, restore initial seed and sequence |
| Pause | Freeze movement, gravity and repeat counters; R and ESCAPE still work |
| Game over | Next piece cannot spawn; no further gameplay until restart/menu |

Spawn shapes (`.` empty), followed by clockwise rotation `(x,y) -> (n-1-y,x)`.
States are stored as four explicit masks and checked against this construction.
O uses the same mask for every state.

| Piece | Spawn rows | Box |
| --- | --- | --- |
| I | `..../####/..../....` | 4×4 |
| O | `.##/.##/...` | 3×3 |
| T | `.#./###/...` | 3×3 |
| S | `.##/##./...` | 3×3 |
| Z | `##./.##/...` | 3×3 |
| J | `#../###/...` | 3×3 |
| L | `..#/###/...` | 3×3 |

Events act in queue order; hard drop and restart reset gravity/repeat counters.
A newly spawned piece can advance its first frame after event processing.
Held horizontal movement is sampled once per frame. Rotation and hard drop
are press events, without host keyboard repeat. Releases only update held
state. The next piece is previewed. No hold, ghost, lock delay or levels.

## Runtime boundary

The application controller and both games operate on ordinary C structs and
`gfx_surface` buffers. Only the capstone entry point accesses MMIO. Static
storage holds both games, board and drawing cache; there is no heap. The
existing linker retains a 16 KiB, 16-byte-aligned stack in the 256 KiB RAM
slice and asserts that data does not overlap it. Display memory stays at the
existing framebuffer address. No ISA, device or input-script format changes.

The uppercase 3×5 letters and punctuation are original bitmap drawings for
this project, encoded as five rows of three bits. Existing digits are reused.
This is our first OS/runtime: boot/menu, polling input, frame timing, drawing,
and static memory services. It has no scheduler, filesystem, loader,
protection, interrupts or host services inside the guest.
