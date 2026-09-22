# M7: the first runtime and Tetris

## Behavior contract

One RV32I image boots to a three-entry menu. UP/DOWN cycles through Pong, Tetris and the G1 2D demo;
ENTER starts the selected game or demo. ESCAPE returns to the menu. Q ends the session
with `PASS <state checksum>`. P pauses and R restarts a game. At game over,
the result remains for 180 presented frames (R can restart), then the menu
returns. Standalone M6 Pong keeps its original controls and recording. The [G1 demo](rv32-gfx.md#guest-demo-and-commands) also uses SPACE to switch CPU/accelerator rendering.

Each iteration drains queued events, checks quit, reads KEYS, advances one
logical frame, draws, and presents. Events delivered by present N affect
iteration N+1. A screen transition consumes the rest of that event batch except Q, which always ends the session;
held game controls are blocked until released. No game reads the device timer.
The window paces presents at 60 fps; backend clock rates do not change rules.

| Tetris behavior | Rule |
| --- | --- |
| Board | 10 columns × 20 rows, all visible; no hidden rows |
| Piece IDs | I, O, T, S, Z, J, L (0 through 6) |
| Spawn | Rotation 0, bounding-box origin (3,0); occupied cells must fit |
| Rotation | UP, clockwise in a fixed 4×4 box for I, 3×3 for others; O unchanged; no kicks |
| Motion | LEFT/RIGHT immediate, first repeat after 12 frames, then every 4; opposing keys cancel; spawning resets repeat so a held direction moves immediately again |
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

## Build, play and replay

Use the same Homebrew LLVM/lld, Python, QEMU, Icarus, Verilator, Yosys, and
SDL3 prerequisites documented in the README. No additional dependencies.

```sh
make run-rv32-capstone                 # build, check, boot the menu, record at 60 fps
make test-rv32-capstone                # native rules/rendering and the pinned session, -O0 and -O2
make test-rv32-capstone-sanitize        # the same directed C checks under ASan and UBSan
make run-rv32-capstone-emu             # 68 checkpoint hashes and PASS ea60197e
make run-rv32-capstone-rtl             # same image and script on Icarus
make run-rv32-capstone-rtl-verilator   # same, with one stall per bus request
make frames-rv32-capstone             # PPMs under build/rv32/capstone-frames/
make disasm-rv32-capstone
```

The normal launch records `build/rv32/capstone.recorded.input` and
`build/rv32/capstone.recorded.checkpoints`. End with Q to obtain a reproducible
terminal checksum; closing the window reports `halt=stopped` instead. Replay:

```sh
build/rv32/rv32emu --image build/rv32/capstone.bin \
  --input build/rv32/capstone.recorded.input \
  --checkpoints build/rv32/capstone.replayed.checkpoints \
  --max-instructions 1000000000
cmp build/rv32/capstone.recorded.checkpoints build/rv32/capstone.replayed.checkpoints
```

Use the same firmware binary as the recording. The input format does not
encode a host window close: a closed session needs an instruction bound to
compare its completed frame prefix. Recordings from Q-terminated sessions
replay to completion directly. A long session may need a larger instruction
budget; the window itself has no instruction limit by default.

The committed `capstone.input` boots, serves and moves Pong, pauses/resumes,
returns to the menu, rotates/moves/drops Tetris pieces, soft-drops,
pauses/resumes, restarts, returns, then quits. Its 68 frame hashes are regression
records generated by the native C build, not independent rendering oracles.
The directed tests check the rules against separately specified expectations;
rotations are checked geometrically, line clears against constructed boards,
and incremental rendering against full redraws. Session history retains game
checksums when returning or restarting, so the final menu checksum still
checks the played games' state.

`tools/rv32_rtl.py --max-cycles N` sets an explicit positive RTL budget (up to
2147483647, the testbench's signed integer limit). Existing invocations retain
the 10-million-cycle default. The capstone requests 20 million, including
stalls, and a 300-second backend timeout. Limits and lost events remain errors.

## Reset to a game, then to a pixel

`start.S` sets SP to `0x80040000`, zeros `.bss`, and calls `main`. The capstone
entry point initializes the application and loops over the input queue.
`runtime_init` chooses the menu and initializes both game structs. ENTER
changes the screen and initializes the selected game; the next drawing pass
repaints its surface. Screen changes block currently held keys until release,
so a menu DOWN cannot become a Tetris soft drop. An automatic game-over return
happens after input draining and does not discard the next frame's new presses.

The application struct is 624 bytes, with no heap or recursion. The verified
image is 15,804 bytes; code, initialized data and `.bss` end at `0x8000402c`,
well below the stack bottom `0x8003c000`. A 200-byte cell cache lets Tetris scan
the board and draw only changed cells; there is no second full framebuffer in
RAM. Text and numbers redraw only when their values or game phase change.
The standalone Pong binary does not link the new text object.

In `build/rv32/emu/capstone.emu.trace`, frame 28 supplies LEFT. Its event and
held-key read, the x-coordinate store, and first erased pixel are:

```text
1361255 800004ac 00092583 x11=80000101 mem[20001000]->80000101/4
1361329 80000488 00892583 x11=00000002 mem[20001008]->00000002/4
1361491 80001c08 1c852223 mem[80003fd0]<-00000002/4
1361796 800033a8 00880023 mem[30001932]<-00000000/1
```

`0x80000101` is a LEFT press, and `0x2` is its held bit. `tetris_frame` changes
the piece origin from column 3 to 2 after collision checking. `tetris_draw`
compares composed board/piece cells with the drawing cache; the byte store at
`0x30001932` erases a cell whose old occupied position moved. The framebuffer
becomes visible only at the subsequent PRESENT store. These are instruction
and memory effects; emulator instruction counts are not RTL clock counts.
Addresses and step numbers describe this build and can move after edits.

## Exercises

1. Predict the rotated I piece at the left wall, then check `check_collisions`.
   Why can an origin of -2 be legal for one rotation but not another?
2. Change gravity from 30 to 15 frames. Identify which timing expectations and
   session checkpoints change before regenerating any expected data.
3. Trace the frame-24 ENTER through the menu transition. Explain why the same
   event cannot rotate or drop the first piece, and why time comes from frames.
4. Replace the seed with 2, derive the first bag, and compare the native and
   RTL results. Explain what the seven-bag guarantees and why a timer seed
   would invalidate deterministic replay.

## Acceptance evidence (2026-09-21)

- Native directed rules, rendering, transition and invalid-session checks pass
  at both `-O0` and `-O2`, and under AddressSanitizer/UndefinedBehaviorSanitizer;
  the 68-frame native session matches the pinned hashes.
- Emulator, Icarus and Verilator: `PASS ea60197e`, 68 identical checkpoints and
  2,220,509 identical retirement lines at M7, zero traps (the current G1 build
  has a larger startup/menu path; see [G1 acceptance](rv32-gfx.md#acceptance-evidence)). Icarus: 9,304,243 cycles;
  Verilator with one stall/request: 11,946,959 cycles, 2,642,716 transfers.
  The cycle relation is exact: `4 × 1,798,302 + 5 × 422,207 + stalls`.
- All existing RV32 suites, QEMU reference checks, strict lint and the existing
  aggregate synthesis checks passed; the new stalled capstone needed its
  explicitly documented 20-million-cycle budget. No RTL changed.
- Counter, ALU, SAP8 and SIMD4 regressions pass. The standalone Pong binary,
  200-frame checksum `8fef54bc`, and frame hashes are preserved.
- Six window tests pass, including the capstone under SDL's dummy driver and
  recording/replay equality. This automated evidence is distinct from play.
- The user confirmed playing both Pong and Tetris from the real menu. The
  first live recording (Pong) replayed all 1,200 checkpoints and `PASS ef82a348`
  exactly. The longer live session is kept locally under `build/rv32/`;
  the first 36,430 recorded checkpoints also replay exactly on the pre-review
  firmware (`6517eea`). That prefix replay adds Q at frame 36,432 only to an offline copy
  to bound execution; the original recording is untouched. Its terminal
  `PASS c245f086` belongs to that derived replay, not the original window session.

The first complete computer is achieved. F1 is next; no floating-point or
accelerator work is included in M7.
