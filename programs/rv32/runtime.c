#include "runtime.h"
#include "board.h"

static uint32_t game_checksum(const struct runtime *r)
{
    if (r->screen == RUNTIME_PONG) return pong_checksum(&r->pong);
    if (r->screen == RUNTIME_TETRIS) return tetris_checksum(&r->tetris);
    if (r->screen == RUNTIME_GPU) return (r->demo.frame*16777619u)^r->demo.accelerated^(r->demo.paused<<8);
    if (r->screen == RUNTIME_DIGIT) return digit_ui_checksum(&r->digit);
    if (r->screen == RUNTIME_3D) return g3d_demo_checksum(&r->g3d);
    return 0;
}

static void change_screen(struct runtime *r, uint32_t screen)
{
    r->history = (r->history ^ game_checksum(r)) * 16777619u;
    r->screen = screen;
    r->blocked = UINT32_MAX;
    r->transition = r->dirty = 1;
    r->over_frames = 0;
    if (screen == RUNTIME_GPU) gpu_demo_init(&r->demo);
    if (screen == RUNTIME_3D) g3d_demo_init(&r->g3d);
    /* Entering the digit screen always starts from a blank canvas; it is
     * deliberately not preserved across a visit to the menu. Clear rather than
     * init, so the classifier the host attached at boot stays attached. */
    if (screen == RUNTIME_DIGIT) digit_ui_clear(&r->digit);
    if (screen == RUNTIME_PONG) pong_init(&r->pong);
    if (screen == RUNTIME_TETRIS) tetris_init(&r->tetris, 1);
}

void runtime_init(struct runtime *r)
{
    gpu_demo_init(&r->demo);
    g3d_demo_attach(&r->g3d, 0, 0);
    g3d_demo_init(&r->g3d);
    digit_ui_init(&r->digit);
    pong_init(&r->pong);
    tetris_init(&r->tetris, 1);
    r->screen = RUNTIME_MENU;
    r->selected = r->quit = r->blocked = r->transition = r->over_frames = r->frames = 0;
    r->dirty = 1;
    r->history = 2166136261u;
}

void runtime_event(struct runtime *r, uint32_t event)
{
    if (!(event & RV32_EVENT_VALID)) return;
    uint32_t code = event & RV32_EVENT_CODE_MASK, bit = 1u << code;
    if (!(event & RV32_EVENT_PRESS)) { r->blocked &= ~bit; return; }
    /* Q ends the session even in a screen-transition batch. */
    if (code == RV32_KEY_Q) { r->quit = 1; return; }
    if (r->transition || (r->blocked & bit) || r->quit) return;
    if (code == RV32_KEY_ESCAPE && r->screen != RUNTIME_MENU) {
        change_screen(r, RUNTIME_MENU);
    } else if (r->screen == RUNTIME_MENU) {
        /* Five entries: explicit compares wrap in both directions (a mask was
         * right only for four, and a modulo would call the software divide). */
        if (code == RV32_KEY_DOWN) { r->selected = r->selected + 1 == RUNTIME_ENTRIES ? 0 : r->selected + 1; r->dirty = 1; }
        else if (code == RV32_KEY_UP) { r->selected = r->selected ? r->selected - 1 : RUNTIME_ENTRIES - 1; r->dirty = 1; }
        else if (code == RV32_KEY_ENTER) change_screen(r, RUNTIME_PONG+r->selected);
    } else {
        if (code == RV32_KEY_R) {
            r->history = (r->history ^ game_checksum(r)) * 16777619u;
            r->over_frames = 0;
        }
        if (r->screen == RUNTIME_PONG) {
            pong_event(&r->pong, event);
            /* Remove our PAUSED overlay, which Pong's dirty rectangles do not track. */
            if (code == RV32_KEY_P) r->pong.needs_full_redraw = 1;
        }
        else if(r->screen==RUNTIME_TETRIS) tetris_event(&r->tetris, event);
        else if(r->screen==RUNTIME_DIGIT) digit_ui_event(&r->digit,code);
        else if(r->screen==RUNTIME_GPU) gpu_demo_event(&r->demo,code);
        else if(r->screen==RUNTIME_3D) g3d_demo_event(&r->g3d,code);
    }
}

void runtime_frame(struct runtime *r, uint32_t keys)
{
    uint32_t held = keys;
    r->frames++;
    r->blocked &= keys;
    keys &= ~r->blocked;
    r->transition = 0;
    uint32_t over = 0;
    if (r->screen == RUNTIME_PONG) {
        pong_frame(&r->pong, keys);
        over = r->pong.phase == PONG_OVER;
    } else if (r->screen == RUNTIME_TETRIS) {
        tetris_frame(&r->tetris, keys);
        over = r->tetris.phase == TETRIS_OVER;
    }
    if(r->screen==RUNTIME_GPU && !r->demo.paused)r->demo.frame++;
    if(r->screen==RUNTIME_3D && !r->g3d.paused)r->g3d.frame++;
    if(r->screen==RUNTIME_DIGIT) digit_ui_frame(&r->digit, keys);
    if (over) {
        if (++r->over_frames > 180) {
            change_screen(r, RUNTIME_MENU);
            /* This transition follows event draining: there is no old batch
             * left to swallow. Only keys actually held stay blocked. */
            r->blocked = held;
            r->transition = 0;
        }
    } else r->over_frames = 0;
}

void runtime_draw(struct runtime *r, const struct gfx_surface *s)
{
    if (r->screen == RUNTIME_PONG) {
        pong_draw(&r->pong, s);
        if (r->pong.phase == PONG_OVER || r->pong.phase == PONG_PAUSED) {
            gfx_fill_rect(s, 40, 108, 240, 24, 0);
            gfx_draw_text(s, 48, 116, r->pong.phase == PONG_OVER ? "GAME OVER - R RESTART" :
                          "PAUSED - P RESUME", 2, 0xff);
        }
    } else if (r->screen == RUNTIME_TETRIS) tetris_draw(&r->tetris, s);
    else if(r->screen==RUNTIME_DIGIT)digit_ui_draw(&r->digit,s);
    else if(r->screen==RUNTIME_GPU)gpu_demo_draw(&r->demo,s);
    else if(r->screen==RUNTIME_3D)g3d_demo_draw(&r->g3d,s);
    else if (r->dirty) {
        gfx_clear(s, 0);
        gfx_draw_text(s, 40, 26, "TINY COMPUTER", 4, 0x1f);
        /* Five entries 22 pixels apart from y=64 keep the last (15 pixels tall at
         * scale 3) clear of the help line at 180. Every menu frame hash changes
         * with this layout; the state checksum changes only for a session whose
         * final selection moved. */
        static const char *const entries[RUNTIME_ENTRIES] = {"PONG", "TETRIS", "DIGIT", "3D DEMO", "2D DEMO"};
        static const char *const help[RUNTIME_ENTRIES] = {"W S AND UP DOWN MOVE SPACE SERVE",
            "LEFT RIGHT MOVE UP ROTATE SPACE DROP", "ARROWS MOVE SPACE DRAW ENTER READ",
            "SPACE SHADER P PAUSE R RESTART", "SPACE CPU GPU P PAUSE R RESTART"};
        for (uint32_t i = 0; i < RUNTIME_ENTRIES; i++) gfx_draw_text(s, 100, 64+(int32_t)i*22, entries[i], 3, 0xff);
        gfx_draw_text(s, 70, 64+(int32_t)r->selected*22, ">", 3, 0xfc);
        gfx_draw_text(s, 36, 196, "UP DOWN SELECT ENTER PLAY", 2, 0xff);
        gfx_draw_text(s, 36, 218, "Q QUIT", 2, 0x92);
        gfx_draw_text(s, 36, 180, help[r->selected], 1, 0x92);
    }
    r->dirty = 0;
}

uint32_t runtime_checksum(const struct runtime *r)
{
    uint32_t h = r->history;
#define HASH(v) h = (h ^ (uint32_t)(v)) * 16777619u
    HASH(r->screen); HASH(r->selected); HASH(r->frames); HASH(r->over_frames);
    HASH(r->blocked); HASH(r->quit); HASH(game_checksum(r));
#undef HASH
    return h;
}
