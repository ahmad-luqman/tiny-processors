/* rv32win: the RV32 machine in a native window (docs/rv32.md, docs/rv32-window.md).
 *
 * Runs the same core as rv32emu and shows every present in an SDL3 window,
 * scaled by an integer, with host keys turned into the contract's key events.
 * Time enters only through pacing: presents are throttled to --fps, the timer
 * stays an instruction counter, so a session recorded with --record replays
 * identically on rv32emu and on the RTL (trace for trace when the guest never
 * reads the timer, at the results level otherwise). Console bytes go to stdout and
 * nothing else does; diagnostics and the final `rv32win: halt=...` line go
 * to stderr with the headless exit status.
 *
 * Build: cc -std=c11 -O2 -Wall -Wextra -Werror $(pkg-config --cflags sdl3) -o rv32win rv32win.c rv32emu_core.c $(pkg-config --libs sdl3)
 * SDL 3.4.16 from Homebrew (zlib license, https://libsdl.org/).
 */
#define _POSIX_C_SOURCE 200809L
#include "rv32emu_core.h"

#include <SDL3/SDL.h>
#include <SDL3/SDL_main.h>
#include <stdlib.h>
#include <string.h>

#define SLICE 250000u        /* instructions between event pumps when the guest does not present */
#define MAX_SCALE 8u

/* Host keys and the contract's key codes (programs/rv32/board.h);
 * tests/test_rv32_tools.py pins this copy to the others. */
static const struct { SDL_Scancode scancode; int code; } KEYMAP[] = {
    {SDL_SCANCODE_LEFT, 1}, {SDL_SCANCODE_RIGHT, 2}, {SDL_SCANCODE_UP, 3}, {SDL_SCANCODE_DOWN, 4},
    {SDL_SCANCODE_SPACE, 5}, {SDL_SCANCODE_RETURN, 6}, {SDL_SCANCODE_ESCAPE, 7}, {SDL_SCANCODE_A, 8},
    {SDL_SCANCODE_D, 9}, {SDL_SCANCODE_W, 10}, {SDL_SCANCODE_S, 11}, {SDL_SCANCODE_P, 12},
    {SDL_SCANCODE_Q, 13}, {SDL_SCANCODE_R, 14},
};

static int scancode_to_key(SDL_Scancode scancode)
{
    for (size_t i = 0; i < sizeof KEYMAP / sizeof KEYMAP[0]; i++) {
        if (KEYMAP[i].scancode == scancode) {
            return KEYMAP[i].code;
        }
    }
    return -1;
}

static void usage(void)
{
    fputs("usage: rv32win --image FILE [--base ADDR] [--pc ADDR] [--scale N] [--fps N] [--input FILE]\n"
          "               [--record FILE] [--checkpoints FILE] [--max-instructions N] [--allow-lost-events]\n"
          "Runs FILE as rv32emu does, with no instruction limit unless --max-instructions, and shows\n"
          "each present in a window scaled by N (1..8, default 3),\n"
          "at most N presents per second (default 60; 0 runs unthrottled). Keys become the contract's\n"
          "events at the next present: arrows, space, return, escape, A, D, W, S, P, Q, R. A scripted\n"
          "--input replays as on rv32emu; --record writes every event, typed or scripted, as a script\n"
          "that replays this session. Closing the window stops the run with halt=stopped.\n",
          stderr);
    exit(EXIT_EMULATOR_ERROR);
}

static int fail_sdl(const char *what)
{
    fprintf(stderr, "rv32win: %s: %s\n", what, SDL_GetError());
    SDL_Quit(); /* harmless before SDL_Init; the process is about to exit */
    return EXIT_EMULATOR_ERROR;
}

/* Copy the framebuffer into the texture through the RGB332 table. */
static bool upload_frame(SDL_Texture *texture, const uint8_t *fb, const uint32_t *lut)
{
    void *pixels;
    int pitch;
    if (!SDL_LockTexture(texture, NULL, &pixels, &pitch)) {
        return false;
    }
    for (uint32_t y = 0; y < FB_ROWS; y++) {
        uint32_t *row = (uint32_t *)((uint8_t *)pixels + (size_t)y * (size_t)pitch);
        const uint8_t *source = fb + y * FB_COLUMNS;
        for (uint32_t x = 0; x < FB_COLUMNS; x++) {
            row[x] = lut[source[x]];
        }
    }
    SDL_UnlockTexture(texture);
    return true;
}

int main(int argc, char **argv)
{
    const char *image_path = NULL, *input_path = NULL, *record_path = NULL, *checkpoints_path = NULL;
    uint32_t base = RAM_BASE, start = 0, scale = 3, fps = 60;
    bool start_given = false, allow_lost_events = false;
    machine m;
    emu_prog = "rv32win";
    emu_init(&m);
    m.limit = UINT64_MAX; /* a session is as long as the player wants; --max-instructions bounds it */
    for (int i = 1; i < argc; i++) {
        const char *arg = argv[i];
        if (!strcmp(arg, "--allow-lost-events")) {
            allow_lost_events = true;
            continue;
        }
        const char *value = i + 1 < argc ? argv[++i] : NULL;
        if (!value) {
            usage();
        }
        if (!strcmp(arg, "--image")) {
            image_path = value;
        } else if (!strcmp(arg, "--base")) {
            base = emu_parse_u32(value, "base address");
        } else if (!strcmp(arg, "--pc")) {
            start = emu_parse_u32(value, "start pc");
            start_given = true;
        } else if (!strcmp(arg, "--scale")) {
            scale = (uint32_t)emu_parse_u64(value, MAX_SCALE, "scale");
            if (scale == 0) {
                fprintf(stderr, "rv32win: bad scale: %s\n", value);
                return EXIT_EMULATOR_ERROR;
            }
        } else if (!strcmp(arg, "--fps")) {
            fps = (uint32_t)emu_parse_u64(value, 1000, "frames per second");
        } else if (!strcmp(arg, "--input")) {
            input_path = value;
        } else if (!strcmp(arg, "--record")) {
            record_path = value;
        } else if (!strcmp(arg, "--checkpoints")) {
            checkpoints_path = value;
        } else if (!strcmp(arg, "--max-instructions")) {
            m.limit = emu_parse_u64(value, UINT64_MAX, "instruction limit");
        } else {
            usage();
        }
    }
    if (!image_path) {
        usage();
    }
    size_t loaded = 0;
    if (!emu_alloc(&m) || !emu_load_image(&m, image_path, base, &loaded)) {
        return EXIT_EMULATOR_ERROR;
    }
    m.pc = start_given ? start : RAM_BASE;
    /* Refuse every aliased pair, parse the script, and open SDL before any output file is
     * created, so a refused run truncates nothing. */
    emu_require_distinct(record_path, "record file", image_path, "image");
    emu_require_distinct(record_path, "record file", input_path, "input script");
    emu_require_distinct(checkpoints_path, "checkpoints file", image_path, "image");
    emu_require_distinct(checkpoints_path, "checkpoints file", input_path, "input script");
    emu_require_distinct(checkpoints_path, "checkpoints file", record_path, "record file");
    if (input_path) {
        emu_read_input_script(&m, input_path); /* exits on a bad script */
    }
    if (!SDL_Init(SDL_INIT_VIDEO)) {
        return fail_sdl("cannot initialise SDL");
    }
    SDL_Window *window;
    SDL_Renderer *renderer;
    if (!SDL_CreateWindowAndRenderer("rv32win", (int)(FB_COLUMNS * scale), (int)(FB_ROWS * scale),
                                     SDL_WINDOW_RESIZABLE | SDL_WINDOW_HIGH_PIXEL_DENSITY, &window, &renderer)) {
        return fail_sdl("cannot open a window");
    }
    /* The guest's 320x240 is scaled by the largest integer that fits the window; the rest is the
     * clear colour, black. */
    SDL_Texture *texture = SDL_CreateTexture(renderer, SDL_PIXELFORMAT_XRGB8888, SDL_TEXTUREACCESS_STREAMING,
                                             (int)FB_COLUMNS, (int)FB_ROWS);
    if (!texture || !SDL_SetRenderDrawColor(renderer, 0, 0, 0, 255) ||
        !SDL_SetRenderLogicalPresentation(renderer, (int)FB_COLUMNS, (int)FB_ROWS, SDL_LOGICAL_PRESENTATION_INTEGER_SCALE) ||
        !SDL_SetTextureScaleMode(texture, SDL_SCALEMODE_NEAREST) /* pixels stay square blocks */) {
        return fail_sdl("cannot set up the frame texture");
    }
    uint32_t lut[256];
    for (unsigned p = 0; p < 256; p++) {
        uint8_t rgb[3];
        emu_rgb332((uint8_t)p, rgb);
        lut[p] = 0xff000000u | ((uint32_t)rgb[0] << 16) | ((uint32_t)rgb[1] << 8) | rgb[2];
    }
    if (record_path) { /* opened before frame 0's events are delivered, so they are recorded too */
        m.record = emu_open_output(record_path, "record file");
        if (!m.record) {
            return EXIT_EMULATOR_ERROR;
        }
    }
    if (checkpoints_path) {
        m.checkpoints = emu_open_output(checkpoints_path, "checkpoints file");
        if (!m.checkpoints) {
            return EXIT_EMULATOR_ERROR;
        }
    }
    emu_require_distinct_streams(m.record, record_path, "record file", m.checkpoints, checkpoints_path, "checkpoints file");
    if (input_path) {
        emu_deliver_events(&m); /* frame 0's events are queued before the first instruction */
    }

    uint32_t *pending = NULL; /* host keys polled since the last present; grows as needed */
    size_t pending_count = 0, pending_capacity = 0;
    bool closing = false, render_ok = true;
    uint64_t period = fps ? 1000000000ull / fps : 0, next_frame = SDL_GetTicksNS();
    for (;;) {
        SDL_Event e;
        while (SDL_PollEvent(&e)) {
            if (e.type == SDL_EVENT_QUIT || e.type == SDL_EVENT_WINDOW_CLOSE_REQUESTED) {
                closing = true;
            } else if (e.type == SDL_EVENT_KEY_DOWN || e.type == SDL_EVENT_KEY_UP) {
                int code = scancode_to_key(e.key.scancode);
                if (e.key.repeat || code < 0) { /* held keys are one press; unmapped keys do not exist */
                    continue;
                }
                uint32_t event = EVENT_VALID | (e.type == SDL_EVENT_KEY_DOWN ? EVENT_PRESS : 0u) | (uint32_t)code;
                if (pending_count == pending_capacity) {
                    pending_capacity = pending_capacity ? 2 * pending_capacity : 64;
                    pending = realloc(pending, pending_capacity * sizeof *pending);
                    if (!pending) {
                        fprintf(stderr, "rv32win: cannot allocate the key list\n");
                        return EXIT_EMULATOR_ERROR;
                    }
                }
                pending[pending_count++] = event; /* every key reaches the queue and the record */
            }
        }
        if (closing) {
            m.halt = HALT_STOPPED;
            break;
        }
        emu_stop stop = emu_run_until(&m, SLICE);
        if (stop == EMU_STOP_HALTED) {
            break;
        }
        if (stop == EMU_STOP_BUDGET) {
            continue; /* no present yet: pump events again so the window stays alive */
        }
        /* A present. The frame's scripted events were queued by the core; the host's keys polled
         * since the last present join the same frame, so a recording of them is a script. More
         * than the queue holds is dropped there, recorded and counted like a scripted burst. */
        for (size_t i = 0; i < pending_count; i++) {
            emu_queue_event(&m, m.frames, pending[i]);
        }
        pending_count = 0;
        if (!upload_frame(texture, m.fb, lut) || !SDL_RenderClear(renderer) ||
            !SDL_RenderTexture(renderer, texture, NULL, NULL) || !SDL_RenderPresent(renderer)) {
            fprintf(stderr, "rv32win: cannot draw the frame: %s\n", SDL_GetError());
            render_ok = false;
            closing = true; /* a window that no longer shows the game is not worth playing in */
        }
        if (period) {
            next_frame += period;
            uint64_t now = SDL_GetTicksNS();
            if (next_frame > now) {
                SDL_DelayPrecise(next_frame - now);
            } else if (now - next_frame > period) {
                next_frame = now; /* fell behind (a slow guest frame): do not try to catch up */
            }
        }
    }
    bool outputs_ok = emu_finish_outputs(&m, NULL, checkpoints_path, record_path) && render_ok;
    int status = emu_report_halt(&m, loaded);
    SDL_DestroyTexture(texture);
    SDL_DestroyRenderer(renderer);
    SDL_DestroyWindow(window);
    SDL_Quit();
    emu_free(&m);
    free(pending);
    return emu_exit_status(&m, status, outputs_ok, allow_lost_events);
}
