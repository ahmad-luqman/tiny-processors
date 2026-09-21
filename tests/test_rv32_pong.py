"""Pong's rules and drawing on the host (programs/rv32/pong_game.c, gfx.c through tools/rv32_pong_native.py).

The game never touches a device, so it compiles for the Mac and runs here at
-O0 and -O2 against hand-computed expectations: glyph shapes, clipping, the
wall and paddle bounces, scoring, the serve, pause, restart, quit. Then the
session of programs/rv32/pong.input is run natively and must give the
checkpoints of pong.expected and the PASS word the Makefile pins; the
emulator and the RTL replay the same session against the same file.
"""

from pathlib import Path
import re
import sys
import unittest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from tools.rv32_devices import EVENT_PRESS, EVENT_VALID, KEYS, frame_hash, parse_input_script  # noqa: E402
from tools.rv32_pong_native import EXPECTED, INPUT, Pong, build, make_surface, run_script  # noqa: E402

FP = 256
WIDTH, HEIGHT = 320, 240
BALL, PADDLE_W, PADDLE_H = 4, 4, 24
LEFT_X, RIGHT_X = 8, 308
GLYPHS = {  # the digits as drawn on graph paper, independently of the C table
    0: ["###", "#.#", "#.#", "#.#", "###"], 1: [".#.", "##.", ".#.", ".#.", "###"],
    2: ["###", "..#", "###", "#..", "###"], 3: ["###", "..#", "###", "..#", "###"],
    4: ["#.#", "#.#", "###", "..#", "..#"], 5: ["###", "#..", "###", "..#", "###"],
    6: ["###", "#..", "###", "#.#", "###"], 7: ["###", "..#", "..#", "..#", "..#"],
    8: ["###", "#.#", "###", "#.#", "###"], 9: ["###", "#.#", "###", "..#", "###"],
}


def press(name):
    return EVENT_VALID | EVENT_PRESS | KEYS[name]


def release(name):
    return EVENT_VALID | KEYS[name]


def held(*names):
    return sum(1 << KEYS[name] for name in names)


class PongTest(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.libraries = {level: build(level) for level in ("O0", "O2")}

    def each(self, check):
        """Run `check(lib)` on the -O0 and the -O2 build, as its own subtest."""
        for level, lib in self.libraries.items():
            with self.subTest(level=level):
                check(lib)

    def test_glyphs_match_the_drawings(self):
        def check(lib):
            for digit, rows in GLYPHS.items():
                for row, text in enumerate(rows):
                    self.assertEqual(lib.gfx_glyph_row(digit, row), int(text.replace("#", "1").replace(".", "0"), 2), (digit, row))
            self.assertEqual((lib.gfx_glyph_row(10, 0), lib.gfx_glyph_row(0, 5)), (0, 0))
            buffer, surface = make_surface(16, 12)
            lib.gfx_draw_digit(surface, 1, 1, 7, 1, 9)
            drawn = ["".join("#" if buffer[y * 16 + x] else "." for x in range(1, 4)) for y in range(1, 6)]
            self.assertEqual(drawn, GLYPHS[7])
            self.assertEqual(sum(buffer), 9 * 7, "seven cells, nothing else")
            lib.gfx_clear(surface, 0)
            lib.gfx_draw_digit(surface, 0, 0, 1, 2, 1)  # scale 2: every cell is a 2x2 block
            for y in range(5):
                for x in range(3):
                    block = {buffer[(2 * y + dy) * 16 + 2 * x + dx] for dy in (0, 1) for dx in (0, 1)}
                    self.assertEqual(block, {1 if GLYPHS[1][y][x] == "#" else 0}, (x, y))
            lib.gfx_clear(surface, 0)
            lib.gfx_draw_number(surface, 0, 0, 42, 1, 1)  # tens at x=0, ones at x=4
            self.assertEqual("".join("#" if buffer[x] else "." for x in range(8)), "#.#.###.")
            lib.gfx_clear(surface, 0)
            lib.gfx_draw_number(surface, 0, 0, 105, 1, 1)  # 105 shows 05
            self.assertEqual("".join("#" if buffer[x] else "." for x in range(8)), "###.###.")
            for value, tens, ones in ((0, 0, 0), (99, 9, 9), (100, 0, 0), (9, 0, 9)):
                lib.gfx_clear(surface, 0)
                lib.gfx_draw_number(surface, 0, 0, value, 1, 1)
                rows = ["".join("#" if buffer[y * 16 + x] else "." for x in range(7)) for y in range(5)]
                self.assertEqual(rows, [f"{GLYPHS[tens][y]}.{GLYPHS[ones][y]}" for y in range(5)], value)
        self.each(check)

    def test_fill_rect_clips_and_clear_covers_everything(self):
        def check(lib):
            buffer, surface = make_surface(20, 10)
            lib.gfx_fill_rect(surface, -3, -2, 5, 4, 7)  # top-left corner: 2x2 survives
            self.assertEqual([buffer[y * 20 + x] for y in range(3) for x in range(3)], [7, 7, 0, 7, 7, 0, 0, 0, 0])
            lib.gfx_fill_rect(surface, 18, 9, 10, 10, 3)  # bottom-right corner: 2x1 survives
            self.assertEqual(list(buffer[9 * 20 + 17:9 * 20 + 20]), [0, 3, 3])
            lib.gfx_fill_rect(surface, 25, 0, 4, 4, 5)  # fully outside
            lib.gfx_fill_rect(surface, 5, 5, 0, 4, 5)  # empty
            lib.gfx_fill_rect(surface, 5, 5, 4, -1, 5)
            self.assertEqual(sum(buffer), 4 * 7 + 2 * 3)
            lib.gfx_fill_rect(surface, 1, 2, 17, 3, 9)  # unaligned start and end: bytes, words, bytes
            for y in range(10):
                for x in range(20):
                    inside = 1 <= x < 18 and 2 <= y < 5
                    if inside:
                        self.assertEqual(buffer[y * 20 + x], 9, (x, y))
            self.assertEqual(sum(buffer), 4 * 7 + 2 * 3 + 17 * 3 * 9, "nothing outside the three rows was touched")
            lib.gfx_clear(surface, 0xE3)
            self.assertEqual(set(buffer), {0xE3})
        self.each(check)

    def test_serve_walls_and_scoring(self):
        def check(lib):
            game = Pong(lib)
            self.assertEqual((game.phase, game.scores, game.ball, game.paddles), ("serve", (0, 0), (158, 118), (108, 108)))
            game.frame(0)
            self.assertEqual(game.ball, (158, 118), "the ball waits for the serve")
            game.event(release("SPACE"))
            self.assertEqual(game.phase, "serve", "a release is not a serve")
            game.event(press("SPACE"))
            self.assertEqual((game.phase, game.velocity), ("play", (4 * FP, 1 * FP)), "first serve: right and down")
            game.frame(0)
            self.assertEqual(game.ball, (162, 119))
            # The right paddle stays put, so the ball passes it at x = 304 + and the left player scores.
            for _ in range(40):
                game.frame(0)
            self.assertEqual((game.phase, game.scores), ("serve", (1, 0)))
            self.assertEqual(game.ball, (158, 118), "back to the centre")
            game.event(press("SPACE"))
            self.assertEqual(game.velocity, (4 * FP, -1 * FP), "second serve: toward the loser, and up")
            # The top wall reflects: a ball rising one pixel a frame with no sideways motion reaches 0 and comes back.
            game.set_ball(100 * FP, 118 * FP, 0, -1 * FP)
            for _ in range(118):
                game.frame(0)
            self.assertEqual(game.ball[1], 0)
            game.frame(0)
            self.assertEqual((game.ball[1], game.velocity[1]), (1, 1 * FP))
            # A ball placed just above the bottom wall, moving down fast, reflects with the overshoot.
            game.set_ball(100 * FP, 233 * FP, 0, 5 * FP)
            game.frame(0)
            self.assertEqual((game.ball[1], game.velocity[1]), (234, -5 * FP), "233 + 5 = 238 is 2 past 236")
            # Past the left edge entirely: the right player scores; a win ends the game.
            game.set_ball(-3 * FP, 100 * FP, -4 * FP, 0)
            game.frame(0)
            self.assertEqual((game.scores, game.phase), ((1, 1), "serve"))
            for _ in range(7):
                game.set_ball(WIDTH * FP, 100 * FP, 4 * FP, 0)
                game.frame(0)
            self.assertEqual((game.scores, game.phase), ((8, 1), "serve"))
            game.set_ball(WIDTH * FP, 100 * FP, 4 * FP, 0)
            game.frame(0)
            self.assertEqual((game.scores, game.phase), ((9, 1), "over"))
            right = Pong(lib)
            for _ in range(9):
                right.set_ball(-4 * FP, 100 * FP, -4 * FP, 0)
                right.frame(0)
            self.assertEqual((right.scores, right.phase), ((0, 9), "over"), "the right player can win too")
            game.event(press("SPACE"))
            game.frame(held("W", "UP"))
            self.assertEqual((game.phase, game.paddles), ("over", (108, 108)), "nothing moves after the win")
            game.event(press("R"))
            self.assertEqual((game.phase, game.scores, game.ball), ("serve", (0, 0), (158, 118)))
        self.each(check)

    def test_paddles_move_clamp_and_return_the_ball(self):
        def check(lib):
            game = Pong(lib)
            game.frame(held("W", "DOWN"))
            self.assertEqual(game.paddles, (105, 111))
            game.frame(held("S", "UP"))
            self.assertEqual(game.paddles, (108, 108))
            for _ in range(50):
                game.frame(held("W", "DOWN"))
            self.assertEqual(game.paddles, (0, HEIGHT - PADDLE_H), "clamped at the walls")
            game.frame(held("W", "S", "UP", "DOWN"))
            self.assertEqual(game.paddles, (3, HEIGHT - PADDLE_H), "up first (clamped) then down: the left one nets +3, the right one 0")
            # Bounce rule (b): vx mirrors, vy comes from where the ball struck the paddle.
            game = Pong(lib)  # paddles at 108..132, centres at 120
            game.set_ball((RIGHT_X - BALL - 2) * FP, 118 * FP, 4 * FP, 0)  # ball centre 120: dead centre
            game.frame(0)
            self.assertEqual((game.ball, game.velocity), ((RIGHT_X - BALL, 118), (-4 * FP, 0)), "returned flat")
            game.set_ball((RIGHT_X - BALL - 2) * FP, 128 * FP, 4 * FP, 0)  # ball centre 130: 10 below
            game.frame(0)
            self.assertEqual(game.velocity, (-4 * FP, (10 * FP) >> 2), "angled down by the offset over 4")
            game.set_ball((LEFT_X + PADDLE_W + 2) * FP, 106 * FP, -4 * FP, 0)  # 106..110 meets the top of 108..132; centre 108: 12 above
            game.frame(0)
            self.assertEqual((game.ball, game.velocity), ((LEFT_X + PADDLE_W, 106), (4 * FP, (-12 * FP) >> 2)))
            # One fixed-point unit of overlap at the paddle's top: the offset is -13 px and 3/4, and the
            # arithmetic shift floors (a division would round toward zero and give -895).
            game.set_ball((RIGHT_X - BALL - 2) * FP, 108 * FP - BALL * FP + 1, 4 * FP, 0)
            game.frame(0)
            self.assertEqual(game.velocity, (-4 * FP, -896))
            # Just missing: the ball's bottom at the paddle's top does not touch.
            game.set_ball((RIGHT_X - BALL - 2) * FP, (108 - BALL) * FP, 4 * FP, 0)
            game.frame(0)
            self.assertEqual(game.velocity, (4 * FP, 0), "no contact")
            game.set_ball((RIGHT_X - BALL - 2) * FP, (108 - BALL + 1) * FP, 4 * FP, 0)
            game.frame(0)
            self.assertEqual(game.velocity[0], -4 * FP, "one pixel of overlap returns it")
            # A ball moving away from a paddle it overlaps is not returned again.
            game.set_ball((RIGHT_X - BALL) * FP, 118 * FP, -4 * FP, 0)
            game.frame(0)
            self.assertEqual(game.ball, (RIGHT_X - BALL - 4, 118))
        self.each(check)

    def test_pause_quit_and_unknown_events(self):
        def check(lib):
            game = Pong(lib)
            game.event(press("SPACE"))
            game.event(press("P"))
            game.frame(held("W"))
            self.assertEqual((game.phase, game.ball, game.paddles), ("paused", (158, 118), (108, 108)), "nothing moves")
            game.event(press("P"))
            game.frame(0)
            self.assertEqual((game.phase, game.ball), ("play", (162, 119)))
            game = Pong(lib)
            game.event(press("P"))
            self.assertEqual(game.phase, "serve", "pause only toggles play and paused")
            for word in (press("A"), press("LEFT"), release("Q"), 0, EVENT_PRESS | KEYS["Q"], press("ESCAPE") & ~EVENT_VALID):
                game.event(word)
            self.assertFalse(game.quit, "unlisted keys, releases, and invalid words do nothing")
            game.event(press("Q"))
            self.assertTrue(game.quit)
            game = Pong(lib)
            game.event(press("ESCAPE"))
            self.assertTrue(game.quit)
        self.each(check)

    def test_drawing_is_the_same_incrementally_and_from_scratch(self):
        """Dirty-rectangle drawing must leave the surface exactly as one full redraw of the same state
        would: the reference replays the same frames on a game that never drew, whose first draw is a
        full one, and the two surfaces must match at every frame. The first session scores a point and
        crosses the net; the second starts the ball inside the score band, crosses both digit boxes,
        and restarts with R, so every restore branch of pong_draw runs."""
        session = parse_input_script("frame 0 down SPACE\nframe 3 down DOWN\nframe 30 up DOWN\nframe 40 down W\n"
                                     "frame 116 down SPACE\nframe 130 down Q\n")
        scores = parse_input_script("frame 60 down R\nframe 62 down SPACE\n")
        for level, lib in self.libraries.items():
            with self.subTest(level=level, session="rally"):
                game = self.compare_incremental_with_full(lib, session, 130)
                self.assertEqual(game.scores, (1, 0), "the script scored a point, so the digits were redrawn")
            with self.subTest(level=level, session="scores"):
                game = self.compare_incremental_with_full(lib, scores, 90, place=(100 * FP, 10 * FP, 4 * FP, 0))
                self.assertEqual((game.scores, game.phase), ((0, 0), "play"), "R reset the game after the ball crossed the digits")

    def compare_incremental_with_full(self, lib, events, frames, place=None):
        """Replay `events` for `frames` frames drawing incrementally, against a game that never drew and
        redraws from scratch at every frame over a garbage-filled surface; with `place` the ball starts
        there, in play. Returns the incremental game."""
        incremental = Pong(lib)
        if place is not None:
            incremental.set_ball(*place)
        keys, frames_keys = 0, []
        for frame in range(1, frames):
            for at, word in events:
                if at == frame - 1:
                    keys = (keys | (1 << (word & 31))) if word & EVENT_PRESS else (keys & ~(1 << (word & 31)))
                    incremental.event(word)
            frames_keys.append(keys)
            incremental.frame(keys)
            incremental.draw()
            fresh = Pong(lib)
            if place is not None:
                fresh.set_ball(*place)
            for n, k in enumerate(frames_keys, 1):
                for at, word in events:
                    if at == n - 1:
                        fresh.event(word)
                fresh.frame(k)
            lib.gfx_clear(fresh.surface, 0x55)  # a full redraw does not depend on what was there
            fresh.draw()
            self.assertEqual(incremental.pixels(), fresh.pixels(), f"frame {frame}")
            self.assertEqual(incremental.scores, fresh.scores)
        return incremental

    def test_session_matches_expected_file_and_makefile(self):
        events = parse_input_script(INPUT.read_text())
        expected = [line for line in EXPECTED.read_text().splitlines() if line and not line.startswith("#")]
        pinned = re.search(r"^RV32_PONG_HEX := ([0-9a-f]{8})$", (ROOT / "Makefile").read_text(), re.M).group(1)
        def check(lib):
            checkpoints, checksum, frames = run_script(lib, events)
            self.assertEqual(frames, 200)
            self.assertEqual(checkpoints, expected)
            self.assertEqual(f"{checksum:08x}", pinned)
            again, _, _ = run_script(lib, events)
            self.assertEqual(again, checkpoints, "deterministic")
        self.each(check)
        # The story of the script, frame by frame: a return, a point, a return, a pause, a wall bounce.
        trace = []
        run_script(self.libraries["O2"], events, trace=trace)
        by_frame = {frame: (phase, scores, ball, velocity, paddles) for frame, phase, scores, ball, velocity, paddles in trace}
        self.assertEqual(by_frame[37][3], (-4 * FP, -2 * FP), "returned by the right paddle at frame 37")
        self.assertEqual(by_frame[115][:2], ("serve", (0, 1)), "the left player missed")
        self.assertEqual(by_frame[157][3][0], 4 * FP, "returned by the left paddle")
        self.assertEqual(by_frame[161][0], "paused")
        self.assertEqual(by_frame[170][0], "paused")
        self.assertEqual(by_frame[171][0], "play")
        self.assertEqual((by_frame[199][3][1], by_frame[200][3][1]), (-640, 640), "the top wall at frame 200")
        # A script that quits with events still to come, or never quits, is refused.
        with self.assertRaisesRegex(ValueError, "come after the quit"):
            run_script(self.libraries["O2"], events + [(201, press("A"))])
        with self.assertRaisesRegex(ValueError, "no quit within 300"):
            run_script(self.libraries["O2"], events[:-1], max_frames=300)
        with self.assertRaisesRegex(ValueError, "would overflow at frame 0"):
            run_script(self.libraries["O2"], [(0, press("A"))] * 17 + [(1, press("Q"))])


if __name__ == "__main__":
    unittest.main()
