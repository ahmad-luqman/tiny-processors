# RV32 machine: the native window, software drawing, and Pong

M6 made the [RV32 machine](rv32.md) something you can play. [tools/rv32win.c](../tools/rv32win.c) shows the emulator's framebuffer in a Mac window and turns keys into the contract's input events; [programs/rv32/gfx.c](../programs/rv32/gfx.c) draws rectangles and digits on any byte surface; [programs/rv32/pong_game.c](../programs/rv32/pong_game.c) is Pong with no device in sight, so the same C runs on the host under test, on the emulator in the window, and on the RTL as a bounded replay. A session of 200 frames scripted in [pong.input](../programs/rv32/pong.input) gives the same 200 checkpoints and the same `PASS 8fef54bc` on the native build, the emulator, Icarus, and Verilator, and the RTL trace equals the emulator's for all 478,797 instructions, because the game never reads the timer.

## Implementation plan (M6)

1. Choose the window library with the smallest program that scales a 320×240 RGB332 buffer and reports keys; decide the host-time timer question and the palette question ([decisions](#decisions-fixed-in-m6)).
2. Split the emulator into a core library and the headless main, byte-identical in behavior, with a run loop the window can pace.
3. The window: presents into a texture, host keys queued at presents, a recording that replays.
4. Drawing routines and Pong in freestanding C with fixed-point arithmetic, tested natively at `-O0` and `-O2`; a scripted session replayed on the emulator against pinned checkpoints.
5. The same session on the RTL, both simulators, with cycles and wall time recorded; the session through a real window.
6. Lint (no RTL changed, so synthesis is unchanged), this record, the roadmap.

All six steps are complete; see [Milestone result](#milestone-result).

## Decisions fixed in M6

| Question | Decision | Why |
| --- | --- | --- |
| Window and input library | SDL 3.4.16 from Homebrew (zlib license, `brew install sdl3`, found with `pkg-config sdl3`) | Already on the machine, one dependency, integer scaling and nearest-neighbour texture built in. The spike (a texture filled from an RGB332 pattern, `SDL_SetRenderLogicalPresentation` with `SDL_LOGICAL_PRESENTATION_INTEGER_SCALE`, key events printed) opened a Cocoa window with the Metal renderer at 960×720 points and 1920×1440 pixels and showed 60 frames in 1.000 s. The dummy video driver runs the same binary without a window, which is how it is tested |
| Interactive pacing and the host-time timer | The window throttles presents to `--fps` (default 60); the timer stays an instruction counter in every mode; **no host-time mode was added** | Time enters the machine only through pacing, which the guest cannot observe, so a recorded session replays exactly on the headless emulator and on the RTL. A game that derived state from wall-clock time would give different checkpoints on every backend; a game that reads the timer at all loses the trace-identical comparison ([Device time](rv32.md#device-time)). Pong paces itself on presents: one step per frame |
| Palette | RGB332 stays the fixed mapping; the window at `0x2000_3000` stays reserved and unmapped | Pong needs four colours. The checkpoint hash covers pixel indices, so a palette added later never invalidates the 200 pinned checkpoints; the four fault cases (a load and a store, on each backend) that pin the window are untouched |
| Host key arrival | Keys typed since the last present are queued as events of the new frame, at the same point scripted events arrive; `--record` writes every offered event as a script line | A hand-played session is a legal input script. Record, replay headless, compare checkpoints: manual play and deterministic replay are the same test |
| Pong rules | Two players on one keyboard: W/S and UP/DOWN, SPACE serves, P pauses, R restarts, Q or ESCAPE quits with a PASS word; first to 9; no randomness | Deterministic and small. The serve's vertical direction alternates; the next serve goes toward the player who lost the point |

## The emulator as a library

[tools/rv32emu_core.c](../tools/rv32emu_core.c) (about a thousand lines) holds what was `rv32emu.c`: the memory map and devices, the instruction step, the input-script parser, the halt report. [tools/rv32emu.c](../tools/rv32emu.c) is about 150 lines of option parsing around it; every existing option and exit status is as before, the usage text is corrected, and `--record` is new. The API in [rv32emu_core.h](../tools/rv32emu_core.h) exposes only what a host needs:

- `emu_run_until(m, budget)` runs until the machine halts, a present raised the frame count, or `budget` instructions have executed, and says which. The present is reported *after* the presenting store's step, and no instruction runs between that return and the host's next call, so an event the host queues on return is indistinguishable to the guest from one the script delivered inside the store. There is no callback into the core and no SDL in it; the headless emulator's loop calls it with an unlimited budget and does nothing at a present.
- `emu_queue_event(m, frame, event)` is the one door into the input queue, for scripted and typed events alike. With `--record` it writes the event as a script line *before* the queue decides, so a replay of the record reproduces the run including its drops.
- A new halt reason, `stopped` (`halt=stopped ... error=host-stopped`, status 2), is the host's doing: the window was closed. The headless emulator never produces it.

The Makefile rule and `build_emulator()` in [rv32_run_emu.py](../tools/rv32_run_emu.py) compile both files; the aliasing guard now covers `--frames` and `--record` as well, and `usage()` no longer claims `--pc` defaults to the base (it defaults to `0x8000_0000`, as the code always did).

## The window

```
build/rv32/rv32win --image FILE [--base ADDR] [--pc ADDR] [--scale N] [--fps N] [--input FILE]
                   [--record FILE] [--checkpoints FILE] [--max-instructions N] [--allow-lost-events]
```

The loop in [rv32win.c](../tools/rv32win.c) (about 250 lines) is: pump SDL events; run the core for at most 250,000 instructions; if that ended at a present, queue the pending keys as events of the new frame, copy the framebuffer through a 256-entry RGB332 table into a 320×240 streaming texture, render it scaled by the largest integer that fits the window (`--scale` sets the initial size, 3 by default), and sleep to the `--fps` deadline; otherwise pump again. A guest that never presents therefore cannot freeze the window, and one that presents faster than the rate is slowed to it. Key repeats and unmapped keys are ignored; the map is LEFT, RIGHT, UP, DOWN, SPACE, RETURN (the contract's ENTER), ESCAPE, A, D, W, S, P, Q, R, pinned to `board.h` by the same test that pins the emulator and the testbench. Console bytes go to stdout and nothing else does; the halt line goes to stderr as `rv32win: halt=...`; exit statuses are the headless emulator's. Closing the window stops the run with `halt=stopped`. The default instruction limit is unlimited, since the headless default of 100 million instructions is about a quarter of an hour of Pong.

`--input` replays a script through the window, which is how the diagnostic and a recorded game can be watched. The tests run the window under `SDL_VIDEO_DRIVER=dummy` with the software renderer: the diagnostic replayed through it gives the headless checkpoints, console, and pass, and its recording parses to the same events as `diag.input` and replays on `rv32emu` to the same checkpoints; a recording starts with the script's frame-0 events and survives a refused run; a guest that never presents ends at its instruction limit; and a SIGTERM, which SDL turns into the quit event a closed window sends, stops a present-forever guest at 60 frames a second with `halt=stopped` and its recording and checkpoints complete. Keys the guest's 16-event queue cannot hold are dropped there, recorded and counted like a scripted burst, so a passing run that lost typed keys is rejected too.

## Software drawing

[gfx.c](../programs/rv32/gfx.c) (about a hundred lines) draws on a `struct gfx_surface`, any array of one-byte pixels with a width and height: the guest passes the framebuffer window, the host tests pass a buffer of their own. `gfx_fill_rect` (clipped) fills each row, and `gfx_clear` the whole buffer as one span, with bytes up to the first word boundary, then words, then bytes; four pixels per store is what makes a full clear cost 94,480 instructions on the guest rather than four times that. One multiply per call computes the first row's address and the rest is an add per row, because a guest multiply is a shift-and-add loop in [rt/muldiv.c](../programs/rv32/rt/muldiv.c). The ten digits are a 3×5 glyph table drawn for this project (each row three bits, bit 2 the left column), scaled by an integer; `gfx_draw_number` shows two digits and splits the value by repeated subtraction rather than `/` and `%`, each of which would be a 32-step division routine.

## Pong

[pong_game.c](../programs/rv32/pong_game.c) (under three hundred lines) is the rules and the drawing; [pong.c](../programs/rv32/pong.c) (forty) is the glue. Positions are fixed point with eight fraction bits; the ball crosses the field at 4 pixels a frame, a paddle moves 3, a hit angles the ball by where it struck. Every product is by a constant or inside the checksum's FNV multiply, so nothing needs a 64-bit helper the guest does not have; the state is written field by field, because a struct assignment would become a `memcpy` the guest does not have either (the linker would refuse the undefined symbol, which is late).

The glue loop is the contract for every replay: iteration `k` pops every queued event, quits with `PASS <checksum>` if the game asked to, reads `KEYS`, simulates one frame, draws what changed, and presents frame `k`. The scripted events of frame `k` arrive with that present and are popped in iteration `k + 1`, so a Q scheduled at frame 200 ends the run with exactly 200 checkpoints. [tools/rv32_pong_native.py](../tools/rv32_pong_native.py) runs the same loop on the host build and writes [pong.expected](../programs/rv32/pong.expected).

The drawing is by dirty rectangles: the first frame clears the field and draws the net and both scores; every later frame erases the ball and any paddle that moved, redraws the net dashes and score digits where the ball's old place cut them, and draws the new positions; a score is redrawn only when it changed. An ordinary frame costs between 1,300 and 1,800 instructions (1,735 in the opening), a frame with a score redraw about 10,000, against 94,480 for the full clear. A host test replays a session with dirty rectangles beside a game that redraws from scratch every frame over a garbage-filled surface, and the two surfaces must be identical at every frame.

**The bounce rule** is the one place with design latitude, and it is marked as such in the source. Three candidates: (a) mirror `vx`, keep `vy`; (b) mirror `vx` and take `vy` from where the ball struck, so a paddle's edge angles the ball and its centre returns it flat; (c) rule b plus a little more speed at every hit. Rule b is implemented and pinned. Changing it is an exercise below: the native tests say what the new rule does, and `rv32_pong_native.py --write` regenerates the expected file.

## Walkthrough: a key press to a pixel

`make run-rv32-pong-emu` leaves the trace of the session in `build/rv32/emu/pong.emu.trace`. The script presses DOWN at frame 5. The fifth present is the store at step 101,422; the sixth iteration starts on the next line:

```
101423 800000f0 00092583 x11=80000104 mem[20001000]->80000104/4
101437 800000b4 00092583 x11=00000000 mem[20001000]->00000000/4
101445 800000cc 00892583 x11=00000010 mem[20001008]->00000010/4
```

The first `lw` pops the event: `0x80000104` is valid, press, code 4, DOWN. The second pop returns 0: the queue is empty. The third reads `KEYS` and gets bit 4, because the key is held. Then `pong_frame` (at `0x80000300`; `llvm-nm` on the ELF gives the addresses, and `game` is at `0x800017ac`):

```
101449 80000300 02c52683 x13=00000005 mem[800017d8]->00000005/4
101454 80000314 02d52623 mem[800017d8]<-00000006/4
101464 800003d8 01452583 x11=00006c00 mem[800017c0]->00006c00/4
101472 800003fc 00b52a23 mem[800017c0]<-00006f00/4
101475 80000408 00052683 x13=0000b200 mem[800017ac]->0000b200/4
101481 80000420 00d52023 mem[800017ac]<-0000b600/4
```

`game + 0x2c` is the frame counter, 5 becoming 6. `game + 0x14` is the right paddle's top edge: `0x6c00` is 108 pixels in fixed point, and `0x6f00` is 111, three pixels down, the clamp having found nothing to do. `game + 0` is the ball's x: 178 becoming 182. Then `pong_draw` erases the ball where it was, at (178, 123), which is byte `123 × 320 + 178 = 0x9a72` of the framebuffer:

```
101651 80000fb0 00880023 mem[30009a72]<-00000000/1
101659 80000fb0 00880023 mem[30009a73]<-00000000/1
101670 80000fec 00880023 mem[30009a74]<-00000000/1
101674 80000fec 00880023 mem[30009a75]<-00000000/1
```

Two byte stores at one PC reach the word boundary, two at another finish the row: `fill_span`'s head and tail loops with no word between them, because the ball is four pixels wide and started two bytes into a word. On the RTL these same lines appear in the same order with the same values; only the cycle count is new. Compare `mem[800017c0]<-00006f00` in the two traces and you have followed one key from the script (or the keyboard: the window queues it at the same point) to a flip-flop in the register file, a word in RAM, and a byte in the framebuffer.

For the timer's wrap, which Pong never touches, the [diagnostic](rv32-soc.md#the-diagnostic) loads `0xFFFFFF00` and polls until the count passes zero; the same instructions read a different number of ticks on each backend, which is why that image is compared at the results level and this one trace for trace.

## Run and verify

```sh
make build-rv32-win           # SDL3 check, then build/rv32/rv32win
make run-rv32-pong            # play in the window; the session is recorded to build/rv32/pong.recorded.input
make test-rv32-pong           # 7 host tests of the game and the drawing at -O0 and -O2, and the session against pong.expected
make test-rv32-win            # 5 tests of the window under SDL's dummy driver (SDL3 is a prerequisite, like LLVM)
make run-rv32-pong-emu        # the session on the emulator: 200 checkpoints and PASS 8fef54bc
make frames-rv32-pong         # the same, writing build/rv32/pong-frames/frame-NNNN.ppm
make run-rv32-pong-rtl        # the session on Icarus, trace-identical to the emulator
make run-rv32-pong-rtl-verilator  # the same on Verilator with one stall per request
make disasm-rv32-pong         # the listing
```

| Run | Instructions | Cycles | Wall time |
| --- | ---: | ---: | ---: |
| Emulator, 200 frames | 478,797 | — | under 0.1 s |
| Icarus, unstalled | 478,797 | 1,998,070 | 21.7 s for the target |
| Verilator, one stall per request | 478,797 | 2,559,749 | 2.1 s for the target |

The cycle formula holds on both: `4 × 395,915 + 5 × 82,882 + stalls`. A 120-frame measurement taken before the script was written put Icarus at about 100,000 cycles a second including the testbench's 19,200-word hash at every present, and Verilator at 2.7 million; the second number had never been recorded. To replay a hand-played session, pass its recording to any backend: `build/rv32/rv32emu --image build/rv32/pong.bin --input build/rv32/pong.recorded.input --checkpoints /tmp/ck`, or the runner with `--input` for the RTL. Every `M5` target is unchanged: the self-check and the diagnostic give the same numbers as before on all three backends.

## Exercises

1. **Change the bounce rule.** Implement candidate (a) or (c) in `bounce()` and run `make test-rv32-pong`: `test_paddles_move_clamp_and_return_the_ball` names exactly which expectations moved. Then regenerate `pong.expected` with `python3 tools/rv32_pong_native.py --write`, pin the new PASS word in the Makefile, and confirm both RTL replays still agree with the emulator. How many of the 200 checkpoints changed, and from which frame?
2. **Cost of a clear.** Change `fill_span` to store bytes only and measure the first frame in the trace (the store to `0x20002000` marks each present). Then try eight pixels per two stores. What does the RTL's 5-cycle store make of each version?
3. **A CPU paddle.** Give the right paddle a rule in `pong_frame` (follow the ball's centre at a capped speed) behind a flag in `struct pong`, keep it deterministic, and add a native test. Which scripted frames of `pong.input` still make sense?
4. **Show the timer.** Read `TICKS` once per frame in `pong.c` and draw it with `gfx_draw_number`. The runner now refuses `--compare trace` for the image: read the message, switch the Pong targets to results mode, and note which comparison you gave up.
5. **A palette.** Map the window at `0x2000_3000` as 256 words on both backends, make the window's lookup table read it, and prove with `run-rv32-pong-emu` that the 200 checkpoints did not change.

## Milestone result

Completed on 2026-09-21 on branch `m6-rv32-window`. From a clean `build/`, `make test-rv32` passes:

- `make test-rv32-tools` 19, `make test-rv32-rt` 6, `make test-rv32-pong` 7, `make test-rv32-emu` 33, `make test-rv32-win` 5, `make test-rv32-rtl` 38 on Icarus and 38 on Verilator.
- `make run-rv32-qemu`, `run-rv32-emu`, `diff-rv32-qemu`, `run-rv32-rtl`, `run-rv32-rtl-verilator`: the self-check unchanged, `PASS 807d9fad`, 32,610 identical lines, 138,495 cycles.
- `make run-rv32-diag-emu`, `run-rv32-diag-rtl`, `run-rv32-diag-rtl-verilator`: `PASS 8bd87e9a` and the two checkpoints unchanged.
- `make run-rv32-pong-emu`, `run-rv32-pong-rtl`, `run-rv32-pong-rtl-verilator`: `PASS 8fef54bc`, 200 identical checkpoints, 478,797 identical trace lines, the cycles in the table above.
- `make lint-rv32`, `lint-rv32-soc` clean; no RTL file changed, so `synth-rv32` (8,175 cells) and `synth-rv32-soc` (23,664 cells) are as in M5.
- The scripted session replayed through a real Cocoa window at 60 frames a second gives the same 200 checkpoints, and the diagnostic ran through the window to `PASS 8bd87e9a`. The roadmap's remaining acceptance step, a session played by hand with `make run-rv32-pong` (end it with Q) whose recording replays headless to the window's checkpoints, was not part of the automated evidence and is the first thing to do with the merged branch.
- Counter, ALU, SAP8, and SIMD4 targets unchanged and passing.

Limitations that remain: no palette; no host-time timer mode, by decision; no interrupts, so waiting is polling; the window is Mac-tested only, though nothing in it is Mac-specific; the pixel checkpoints of the session come from the same C compiled natively, while the rules are checked against hand-computed expectations. M7 adds the boot menu, the runtime services both games share, and Tetris.
