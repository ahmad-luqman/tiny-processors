#include "runtime.h"
#include "board.h"

static uint32_t game_checksum(const struct runtime *r)
{
    if (r->screen == RUNTIME_PONG) return pong_checksum(&r->pong);
    if (r->screen == RUNTIME_TETRIS) return tetris_checksum(&r->tetris);
    if (r->screen == RUNTIME_GPU) return (r->demo.frame*16777619u)^r->demo.accelerated^(r->demo.paused<<8);
    if (r->screen == RUNTIME_DIGIT) return digit_ui_checksum(&r->digit);
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
        /* Four entries, so the wrap is a mask; UP is -1 modulo four, which is +3.
         * With three entries it was +2, and leaving that would send UP from the
         * first entry to the third instead of the last. */
        if (code == RV32_KEY_UP || code == RV32_KEY_DOWN) { r->selected = (code==RV32_KEY_DOWN ? r->selected+1 : r->selected+RUNTIME_ENTRIES-1) & (RUNTIME_ENTRIES-1); r->dirty = 1; }
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
    else if (r->dirty) {
        gfx_clear(s, 0);
        gfx_draw_text(s, 40, 26, "TINY COMPUTER", 4, 0x1f);
        /* Four entries need tighter spacing than three: 30 pixels apart from y=70
         * keeps the last one clear of the help line. Every menu frame hash changes
         * with this layout. The state checksum does not change for a session whose
         * final selection is unchanged, which is why the capstone replay kept its
         * PASS word; the graphics replay wraps upward from entry 0 and so now ends
         * on selection 3 instead of 2, and its word did change. */
        gfx_draw_text(s, 100, 70, "PONG", 3, 0xff);
        gfx_draw_text(s, 100, 100, "TETRIS", 3, 0xff);
        gfx_draw_text(s, 100, 130, "DIGIT", 3, 0xff);
        gfx_draw_text(s, 100, 160, "2D DEMO", 3, 0xff);
        gfx_draw_text(s, 70, 70+(int32_t)r->selected*30, ">", 3, 0xfc);
        gfx_draw_text(s, 36, 196, "UP DOWN SELECT ENTER PLAY", 2, 0xff);
        gfx_draw_text(s, 36, 218, "Q QUIT", 2, 0x92);
        gfx_draw_text(s, 36, 180, r->selected==3 ? "SPACE CPU GPU P PAUSE R RESTART" : r->selected==2 ? "ARROWS MOVE SPACE DRAW ENTER READ" : r->selected ? "LEFT RIGHT MOVE UP ROTATE SPACE DROP" : "W S AND UP DOWN MOVE SPACE SERVE", 1, 0x92);
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
