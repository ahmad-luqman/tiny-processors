/* Native directed oracles. CHECK returns its source line to Python. */
#include <string.h>
#include "runtime.h"
#include "board.h"
#define CHECK(x) do { if (!(x)) return __LINE__; } while (0)
#define PRESS(k) (RV32_EVENT_VALID | RV32_EVENT_PRESS | RV32_KEY_##k)
#define RELEASE(k) (RV32_EVENT_VALID | RV32_KEY_##k)
#define HELD(k) (1u << RV32_KEY_##k)
uint32_t native_size(void) { return sizeof(struct runtime); }
uint32_t native_quit(const struct runtime *r) { return r->quit; }
uint32_t native_screen(const struct runtime *r) { return r->screen; }
uint32_t native_tetris_score(const struct runtime *r) { return r->tetris.score; }
uint32_t native_tetris_lines(const struct runtime *r) { return r->tetris.lines; }
uint32_t native_digit_runs(const struct runtime *r) { return r->digit.runs; }
uint32_t native_digit_predicted(const struct runtime *r) { return r->digit.predicted; }
uint32_t native_digit_status(const struct runtime *r) { return r->digit.status; }

/* The native model classifies with the software path only. The firmware installs
 * a classifier that also runs the accelerator and compares the two. */
static uint32_t native_classify(const uint8_t canvas[DIGIT_PIXELS], int32_t logits[DIGIT_CLASSES])
{
    uint8_t x[DIGIT_INPUTS];
    digit_prepare(canvas, x);
    digit_infer_cpu(x, logits);
    return 0;
}

void native_attach_digit(struct runtime *r) { digit_ui_attach(&r->digit, native_classify); }

/* The native model renders the 3D screen with the C reference; the firmware
 * renders on the device, and the pinned checkpoints hold them to the same pixels. */
static uint16_t native_zbuf[320*240];
static uint32_t native_render(void *context, const struct g3d_job *job, const struct gfx_surface *s)
{
    (void)context;
    struct g3d_counts counts;
    gfx_clear(s, G3D_DEMO_BACKGROUND);
    for (uint32_t i = 0; i < 320*240; i++) native_zbuf[i] = 0xffff;
    return g3d_reference(s->pixels, native_zbuf, job, &counts);
}

void native_attach_g3d(struct runtime *r) { g3d_demo_attach(&r->g3d, native_render, 0); }
uint32_t native_g3d_shader(const struct runtime *r) { return r->g3d.shader; }
uint32_t native_g3d_frame(const struct runtime *r) { return r->g3d.frame; }
uint32_t native_g3d_status(const struct runtime *r) { return r->g3d.status; }
void native_g3d_constants(uint32_t frame, uint32_t *k) { g3d_demo_constants(frame, k); }

/* A classifier that fails the way a device failure would, so the status path is
 * exercised: nothing else in the suite ever returns non-zero. */
static uint32_t failing_classify(const uint8_t canvas[DIGIT_PIXELS], int32_t logits[DIGIT_CLASSES])
{
    (void)canvas;
    for (uint32_t c = 0; c < DIGIT_CLASSES; c++) logits[c] = 1234;   /* partially plausible */
    return 7;
}

int check_shapes(void)
{
    /* Independent row strings pin the spawn orientation and geometry. */
    const char *shapes[7] = {"....####........", ".##..##.........", ".#..###.........",
                            ".##.##..........", "##...##.........", "#...###.........", "..#.###........."};
    for (uint32_t p = 0; p < 7; p++) {
        uint32_t mask = 0;
        for (uint32_t i = 0; i < 16; i++) if (shapes[p][i] == '#') mask |= 1u << i;
        for (uint32_t r = 0; r < 4; r++) {
            CHECK(tetris_mask(p,r) == mask);
            uint32_t next = 0, n = p == 0 ? 4 : 3;
            for (uint32_t y = 0; y < n; y++) for (uint32_t x = 0; x < n; x++)
                if (mask & (1u << (y*4+x))) next |= 1u << (x*4 + n-1-y);
            if (p != 1) mask = next;
        }
    }
    CHECK(tetris_mask(7,0) == 0);
    return 0;
}

int check_collisions(void)
{
    struct tetris t;
    /* Independent occupied-coordinate bounds cover every piece/rotation and each wall. */
    for (uint32_t p = 0; p < 7; p++) for (uint32_t r = 0; r < 4; r++) {
        tetris_init(&t,1); t.piece=p; t.rotation=r;
        uint32_t mask=tetris_mask(p,r);
        for (int32_t y=-4; y<=20; y++) for (int32_t x=-4; x<=10; x++) {
            uint32_t expected=1;
            for (int32_t i=0; i<16; i++) if (mask & (1u<<i))
                if (x+i%4<0 || x+i%4>=10 || y+i/4<0 || y+i/4>=20) expected=0;
            CHECK(tetris_fits(&t,x,y,r)==expected);
            if (expected) {
                t.x=x; t.y=y;
                uint32_t nr=(r+1)&3, fits=tetris_fits(&t,x,y,nr);
                tetris_event(&t,PRESS(UP));
                CHECK(t.rotation==(fits ? nr:r)); t.rotation=r;
            }
        }
        t.x=3; t.y=5;
        for (int32_t i=0; i<16; i++) if (mask & (1u<<i)) {
            t.board[5+i/4][3+i%4]=1;
            CHECK(!tetris_fits(&t,3,5,r)); t.board[5+i/4][3+i%4]=0;
        }
    }
    return 0;
}

int check_clear_score(void)
{
    static const uint32_t points[5]={0,100,300,500,800};
    struct tetris t;
    for (uint32_t n=1; n<=4; n++) {
        tetris_init(&t,1);
        /* A vertical I fills a one-cell well in the last n rows. */
        t.piece=0; t.rotation=1; t.x=2; t.y=0;
        for (uint32_t y=20-n; y<20; y++) for (uint32_t x=0; x<10; x++) t.board[y][x]=(x==4 ? 0:2);
        t.board[10][0]=7; /* survivor must move down exactly n rows */
        tetris_event(&t,PRESS(SPACE));
        CHECK(t.lines==n && t.score==points[n]);
        CHECK(t.board[10+n][0]==7 && t.board[10][0]==0);
        for (uint32_t y=0; y<n; y++) for (uint32_t x=0; x<10; x++) CHECK(t.board[y][x]==0);
    }
    tetris_init(&t,1); t.piece=0; t.rotation=1; t.x=2; t.y=0;
    for (uint32_t x=0; x<10; x++) t.board[17][x]=t.board[19][x]=(x==4 ? 0:2);
    t.board[10][0]=7;
    tetris_event(&t,PRESS(SPACE));
    CHECK(t.lines==2 && t.score==300 && t.board[12][0]==7);
    CHECK(t.board[18][4]==1 && t.board[19][4]==1); /* the I's surviving cells compact too */
    tetris_init(&t,1); t.piece=0; t.rotation=0; t.x=3; t.y=18;
    t.gravity=29; tetris_frame(&t,0);
    for (uint32_t x=3; x<7; x++) CHECK(t.board[19][x]==1);
    CHECK(t.score==0 && t.y==0);
    tetris_init(&t,1); t.score=UINT32_MAX-10; t.lines=UINT32_MAX;
    t.piece=0; t.rotation=0; t.x=3; t.y=18;
    for (uint32_t x=0; x<10; x++) t.board[19][x]=(x>=3 && x<7 ? 0:2);
    tetris_event(&t,PRESS(SPACE));
    CHECK(t.score==UINT32_MAX && t.lines==UINT32_MAX);
    return 0;
}

int check_timing(void)
{
    struct tetris t;
    tetris_init(&t,1);
    for (uint32_t i=0; i<29; i++) tetris_frame(&t,0);
    CHECK(t.y==0); tetris_frame(&t,0); CHECK(t.y==1);
    tetris_frame(&t,HELD(DOWN)); tetris_frame(&t,HELD(DOWN)); CHECK(t.y==1);
    tetris_frame(&t,HELD(DOWN)); CHECK(t.y==2);
    tetris_init(&t,1); tetris_frame(&t,HELD(LEFT)); CHECK(t.x==2);
    for (uint32_t i=0; i<11; i++) tetris_frame(&t,HELD(LEFT));
    CHECK(t.x==2); tetris_frame(&t,HELD(LEFT)); CHECK(t.x==1);
    for (uint32_t i=0; i<4; i++) tetris_frame(&t,HELD(LEFT)); CHECK(t.x==0);
    tetris_frame(&t,HELD(LEFT)|HELD(RIGHT)); CHECK(t.x==0);
    tetris_frame(&t,HELD(RIGHT)); CHECK(t.x==1);
    tetris_event(&t,PRESS(P)); uint32_t h=tetris_checksum(&t);
    for (uint32_t i=0; i<50; i++) tetris_frame(&t,HELD(RIGHT)|HELD(DOWN));
    tetris_event(&t,PRESS(UP)); tetris_event(&t,PRESS(SPACE));
    CHECK(tetris_checksum(&t)==h);
    tetris_event(&t,RELEASE(P)); CHECK(t.phase==TETRIS_PAUSED);
    tetris_event(&t,PRESS(P)); CHECK(t.phase==TETRIS_PLAY);
    return 0;
}

int check_random_restart(void)
{
    struct tetris t, other;
    tetris_init(&t,0); tetris_init(&other,1);
    CHECK(tetris_checksum(&t)==tetris_checksum(&other));
    uint32_t initial=tetris_checksum(&t);
    /* An independent host implementation pins 100 consecutive bags. */
    uint32_t rng=1;
    for (uint32_t b=0; b<100; b++) {
        uint32_t bag[7]={0,1,2,3,4,5,6};
        for (uint32_t i=6; i>0; i--) {
            rng ^= rng<<13; rng ^= rng>>17; rng ^= rng<<5;
            uint32_t j=rng%(i+1), v=bag[i]; bag[i]=bag[j]; bag[j]=v;
        }
        uint32_t seen=0;
        for (uint32_t i=0; i<7; i++) {
            CHECK(t.piece==bag[i]); seen |= 1u<<t.piece;
            tetris_event(&t,PRESS(SPACE));
            memset(t.board,0,sizeof t.board);
        }
        CHECK(seen==127);
    }
    tetris_event(&t,PRESS(R)); CHECK(tetris_checksum(&t)==initial);
    tetris_init(&other,2); CHECK(tetris_checksum(&t)!=tetris_checksum(&other));
    /* Spawn is blocked only after the old piece locks. */
    for (uint32_t y=0; y<2; y++) for (uint32_t x=0; x<10; x++) t.board[y][x]=1;
    /* Leave a hole so the row does not clear. */
    t.board[0][0]=t.board[1][0]=0;
    t.y=10; tetris_event(&t,PRESS(SPACE)); CHECK(t.phase==TETRIS_OVER);
    uint32_t h=tetris_checksum(&t);
    tetris_frame(&t,HELD(DOWN)); tetris_event(&t,PRESS(UP)); tetris_event(&t,PRESS(P));
    CHECK(tetris_checksum(&t)==h && t.phase==TETRIS_OVER);
    tetris_event(&t,PRESS(R)); CHECK(tetris_checksum(&t)==initial);
    return 0;
}

int check_transitions(void)
{
    struct runtime r;
    runtime_init(&r); CHECK(r.screen==RUNTIME_MENU && r.selected==0);
    runtime_event(&r,PRESS(ENTER)); runtime_event(&r,PRESS(SPACE));
    runtime_frame(&r,HELD(ENTER)|HELD(SPACE)|HELD(W));
    CHECK(r.screen==RUNTIME_PONG && r.pong.phase==PONG_SERVE && r.pong.left_y==108*256);
    runtime_event(&r,PRESS(SPACE)); CHECK(r.pong.phase==PONG_SERVE);
    runtime_event(&r,RELEASE(SPACE)); runtime_event(&r,PRESS(SPACE)); CHECK(r.pong.phase==PONG_PLAY);
    runtime_frame(&r,0); runtime_event(&r,PRESS(ESCAPE)); runtime_frame(&r,0);
    CHECK(r.screen==RUNTIME_MENU);
    runtime_event(&r,PRESS(DOWN)); runtime_event(&r,PRESS(ENTER)); runtime_frame(&r,HELD(DOWN));
    CHECK(r.screen==RUNTIME_TETRIS && r.tetris.gravity==1);
    for (uint32_t i=0; i<5; i++) runtime_frame(&r,HELD(DOWN)); CHECK(r.tetris.y==0);
    runtime_event(&r,RELEASE(DOWN)); runtime_frame(&r,0);
    runtime_event(&r,PRESS(R)); CHECK(r.tetris.frame==0);
    r.tetris.phase=TETRIS_OVER;
    for (uint32_t i=0; i<180; i++) { runtime_frame(&r,0); CHECK(r.screen==RUNTIME_TETRIS); }
    runtime_frame(&r,HELD(DOWN)); CHECK(r.screen==RUNTIME_MENU);
    runtime_event(&r,PRESS(DOWN)); CHECK(r.selected==1); /* held across automatic return */
    runtime_event(&r,RELEASE(DOWN)); runtime_event(&r,PRESS(UP));
    runtime_event(&r,PRESS(ENTER)); runtime_frame(&r,0);
    CHECK(r.screen==RUNTIME_PONG);
    r.pong.phase=PONG_OVER;
    for (uint32_t i=0; i<30; i++) runtime_frame(&r,0);
    runtime_event(&r,PRESS(R)); CHECK(r.over_frames==0 && r.pong.phase==PONG_SERVE);
    r.pong.phase=PONG_OVER;
    for (uint32_t i=0; i<181; i++) runtime_frame(&r,0);
    CHECK(r.screen==RUNTIME_MENU);
    runtime_event(&r,PRESS(Q)); CHECK(r.quit);
    for (uint32_t game=0; game<2; game++) {
        runtime_init(&r); r.selected=game; runtime_event(&r,PRESS(ENTER)); runtime_frame(&r,0);
        if (!game) runtime_event(&r,PRESS(SPACE));
        runtime_event(&r,PRESS(P));
        CHECK(game ? r.tetris.phase==TETRIS_PAUSED : r.pong.phase==PONG_PAUSED);
        runtime_event(&r,PRESS(R));
        CHECK(game ? r.tetris.phase==TETRIS_PLAY : r.pong.phase==PONG_SERVE);
        if (!game) runtime_event(&r,PRESS(SPACE));
        runtime_event(&r,PRESS(P)); runtime_event(&r,PRESS(ESCAPE));
        CHECK(r.screen==RUNTIME_MENU);
    }
    return 0;
}

int check_render(void)
{
    static uint8_t incremental[320*240], reference[320*240];
    struct gfx_surface a={incremental,320,240}, b={reference,320,240};
    struct tetris t;
    tetris_init(&t,1);
    for (uint32_t i=0; i<700; i++) {
        if (i%17==0) tetris_event(&t,PRESS(UP));
        if (i%23==0) tetris_event(&t,PRESS(SPACE));
        if (i%31==0) tetris_event(&t,PRESS(P));
        if (i%113==0) tetris_event(&t,PRESS(R));
        tetris_frame(&t,(i%40<20 ? HELD(LEFT):HELD(RIGHT))|HELD(DOWN));
        tetris_draw(&t,&a);
        struct tetris copy=t; copy.dirty=1; tetris_draw(&copy,&b);
        CHECK(memcmp(incremental,reference,sizeof incremental)==0);
    }
    /* Actually clear non-adjacent rows, then hide a blocked piece at OVER. */
    tetris_init(&t,1); t.piece=0; t.rotation=1; t.x=2;
    for (uint32_t x=0; x<10; x++) t.board[17][x]=t.board[19][x]=(x==4 ? 0:2);
    tetris_draw(&t,&a);
    tetris_event(&t,PRESS(SPACE)); CHECK(t.lines==2 && t.score==300);
    tetris_draw(&t,&a);
    struct tetris copy=t; copy.dirty=1; tetris_draw(&copy,&b);
    CHECK(memcmp(incremental,reference,sizeof incremental)==0);
    t.phase=TETRIS_OVER; tetris_draw(&t,&a);
    copy=t; copy.dirty=1; tetris_draw(&copy,&b);
    CHECK(memcmp(incremental,reference,sizeof incremental)==0);
    static const uint8_t a_rows[5]={2,5,7,5,5}, arrow[5]={4,2,1,2,4};
    for (uint32_t y=0; y<5; y++) {
        CHECK(gfx_char_row('A',y)==a_rows[y]); CHECK(gfx_char_row('>',y)==arrow[y]);
        for (uint32_t n=0; n<10; n++) CHECK(gfx_char_row('0'+n,y)==gfx_glyph_row(n,y));
    }
    CHECK(gfx_char_row('A',5)==0 && gfx_char_row('?',0)==0);
    gfx_clear(&a,0); gfx_clear(&b,0);
    gfx_draw_uint(&a,0,0,UINT32_MAX,2,255); gfx_draw_text(&b,0,0,"4294967295",2,255);
    CHECK(memcmp(incremental,reference,sizeof incremental)==0);
    gfx_clear(&a,0); gfx_clear(&b,0);
    gfx_draw_uint(&a,0,0,0,2,255); gfx_draw_text(&b,0,0,"0",2,255);
    CHECK(memcmp(incremental,reference,sizeof incremental)==0);
    /* Clipping cannot touch guard pixels outside the surface. */
    uint8_t guarded[102]; memset(guarded,0x55,sizeof guarded);
    struct gfx_surface small={guarded+1,10,10};
    gfx_draw_text(&small,-2,-2,"TETRIS",2,255);
    CHECK(guarded[0]==0x55 && guarded[101]==0x55);
    return 0;
}


int check_quit_batches(void)
{
    struct runtime r;
    for (uint32_t game=0; game<2; game++) {
        runtime_init(&r); r.selected=game;
        runtime_event(&r,PRESS(ENTER)); runtime_event(&r,PRESS(Q));
        CHECK(r.quit); /* ENTER and Q between the same presents */
        runtime_init(&r); r.selected=game; runtime_event(&r,PRESS(ENTER)); runtime_frame(&r,0);
        runtime_event(&r,PRESS(ESCAPE)); runtime_event(&r,PRESS(Q));
        CHECK(r.screen==RUNTIME_MENU && r.quit); /* ESC and Q in one batch */
        runtime_init(&r); r.selected=game; runtime_event(&r,PRESS(ENTER)); runtime_frame(&r,0);
        runtime_event(&r,PRESS(Q)); CHECK(r.quit); /* quit directly inside either game */
    }
    return 0;
}

int check_runtime_render(void)
{
    static uint8_t incremental[320*240], reference[320*240];
    struct gfx_surface a={incremental,320,240}, b={reference,320,240};
    struct runtime r;
    runtime_init(&r);
    uint32_t phases=0;
    for (uint32_t frame=0; frame<250; frame++) {
        switch (frame) {
        case 1: case 23: case 226: runtime_event(&r,PRESS(ENTER)); break;
        case 2: case 17: case 227: runtime_event(&r,PRESS(SPACE)); break;
        case 8: case 12: case 26: case 230: runtime_event(&r,PRESS(P)); break;
        case 15: r.pong.phase=PONG_OVER; break;
        case 16: case 28:
            runtime_event(&r,PRESS(R));
            CHECK(frame==16 ? r.pong.phase==PONG_SERVE : r.tetris.phase==TETRIS_PLAY);
            break;
        case 20: case 232:
            runtime_event(&r,PRESS(ESCAPE)); CHECK(r.screen==RUNTIME_MENU); break;
        case 22: runtime_event(&r,PRESS(DOWN)); break;
        case 40: r.tetris.phase=TETRIS_OVER; break;
        case 222: CHECK(r.screen==RUNTIME_MENU); break;
        case 225: runtime_event(&r,PRESS(UP)); break;
        }
        runtime_frame(&r,0);
        if (r.screen==RUNTIME_PONG) phases |= 1u<<r.pong.phase;
        if (r.screen==RUNTIME_TETRIS) phases |= 1u<<(4+r.tetris.phase);
        runtime_draw(&r,&a);
        struct runtime copy=r;
        copy.dirty=copy.tetris.dirty=copy.pong.needs_full_redraw=1;
        runtime_draw(&copy,&b);
        CHECK(memcmp(incremental,reference,sizeof incremental)==0);
    }
    CHECK(phases==127); /* serve/play/pause/over for Pong and play/pause/over for Tetris */
    return 0;
}

/* The digit screen: cursor bounds, the brush, clearing, and the classifier
 * callback, none of which touch a device. */
int check_digit_ui(void)
{
    struct runtime r;
    runtime_init(&r);
    native_attach_digit(&r);
    CHECK(r.digit.cursor_x == DIGIT_SIDE / 2 && r.digit.cursor_y == DIGIT_SIDE / 2);
    CHECK(r.digit.runs == 0 && r.digit.classified == 0);

    /* The cursor stops at every edge instead of wrapping or running off. A held
     * key moves once and then every DIGIT_UI_REPEAT+1 frames, so crossing the
     * canvas takes that many frames per cell. */
    const uint32_t sweep = DIGIT_SIDE * (DIGIT_UI_REPEAT + 1) + 8;
    for (uint32_t i = 0; i < sweep; i++) digit_ui_frame(&r.digit, HELD(LEFT));
    CHECK(r.digit.cursor_x == 0);
    for (uint32_t i = 0; i < sweep; i++) digit_ui_frame(&r.digit, HELD(UP));
    CHECK(r.digit.cursor_y == 0);
    for (uint32_t i = 0; i < sweep; i++) digit_ui_frame(&r.digit, HELD(RIGHT) | HELD(DOWN));
    CHECK(r.digit.cursor_x == DIGIT_SIDE - 1 && r.digit.cursor_y == DIGIT_SIDE - 1);

    /* A held direction repeats on a fixed cadence rather than every frame. */
    digit_ui_init(&r.digit);
    native_attach_digit(&r);
    uint32_t start = r.digit.cursor_x;
    digit_ui_frame(&r.digit, HELD(LEFT));
    CHECK(r.digit.cursor_x == start - 1);          /* the first frame moves at once */
    for (uint32_t i = 0; i < DIGIT_UI_REPEAT; i++) digit_ui_frame(&r.digit, HELD(LEFT));
    CHECK(r.digit.cursor_x == start - 1);          /* the cadence counts down first */
    digit_ui_frame(&r.digit, HELD(LEFT));
    CHECK(r.digit.cursor_x == start - 2);          /* so a move lands every REPEAT+1 frames */
    digit_ui_frame(&r.digit, 0);
    digit_ui_frame(&r.digit, HELD(LEFT));
    CHECK(r.digit.cursor_x == start - 3);          /* releasing restarts the cadence */

    /* SPACE paints a 2x2 block clipped at the edge. */
    digit_ui_init(&r.digit);
    native_attach_digit(&r);
    r.digit.cursor_x = r.digit.cursor_y = 5;
    digit_ui_frame(&r.digit, HELD(SPACE));
    uint32_t ink = 0;
    for (uint32_t i = 0; i < DIGIT_PIXELS; i++) if (r.digit.canvas[i]) ink++;
    CHECK(ink == 4);
    CHECK(r.digit.canvas[5 * DIGIT_SIDE + 5] && r.digit.canvas[6 * DIGIT_SIDE + 6]);
    r.digit.cursor_x = r.digit.cursor_y = DIGIT_SIDE - 1;
    digit_ui_frame(&r.digit, HELD(SPACE));
    ink = 0;
    for (uint32_t i = 0; i < DIGIT_PIXELS; i++) if (r.digit.canvas[i]) ink++;
    CHECK(ink == 5);                               /* only the in-range corner cell */

    /* R clears the canvas and the result but keeps the classifier installed. */
    digit_ui_event(&r.digit, RV32_KEY_ENTER);
    CHECK(r.digit.runs == 1 && r.digit.classified == 1 && r.digit.status == 0);
    digit_ui_event(&r.digit, RV32_KEY_R);
    CHECK(r.digit.predicted == 0 && r.digit.margin == 0);   /* the clear resets the result */
    for (uint32_t i = 0; i < DIGIT_PIXELS; i++) CHECK(r.digit.canvas[i] == 0);
    CHECK(r.digit.runs == 0 && r.digit.classified == 0);
    digit_ui_event(&r.digit, RV32_KEY_ENTER);
    CHECK(r.digit.runs == 1);                      /* still attached after a clear */

    /* A blank canvas still classifies: every logit is the bias alone. */
    int32_t blank[DIGIT_CLASSES];
    uint8_t zeros[DIGIT_INPUTS];
    for (uint32_t i = 0; i < DIGIT_INPUTS; i++) zeros[i] = 0;
    digit_infer_cpu(zeros, blank);
    for (uint32_t c = 0; c < DIGIT_CLASSES; c++) CHECK(r.digit.logits[c] == blank[c]);

    /* runtime_init leaves no classifier attached, so ENTER must do nothing rather
     * than jump through whatever the stack happened to hold. This is asserted, not
     * arranged: the test does not clear the field first. */
    struct runtime bare;
    runtime_init(&bare);
    CHECK(bare.digit.classify == 0);
    digit_ui_event(&bare.digit, RV32_KEY_ENTER);
    CHECK(bare.digit.runs == 0 && bare.digit.classified == 0);

    /* A failing classifier must not leave a prediction behind: the screen shows an
     * error, and the checksum must not claim a digit that was never computed. */
    struct runtime failed;
    runtime_init(&failed);
    native_attach_digit(&failed);
    failed.digit.cursor_x = failed.digit.cursor_y = 9;
    digit_ui_frame(&failed.digit, HELD(SPACE));
    digit_ui_event(&failed.digit, RV32_KEY_ENTER);
    uint32_t good_checksum = digit_ui_checksum(&failed.digit);
    CHECK(failed.digit.status == 0 && failed.digit.classified == 1);
    digit_ui_attach(&failed.digit, failing_classify);
    digit_ui_event(&failed.digit, RV32_KEY_ENTER);
    CHECK(failed.digit.status == 7 && failed.digit.classified == 1);
    CHECK(failed.digit.predicted == 0 && failed.digit.margin == 0);
    for (uint32_t c = 0; c < DIGIT_CLASSES; c++) CHECK(failed.digit.logits[c] == 0);
    CHECK(digit_ui_checksum(&failed.digit) != good_checksum);

    /* Drawing is a second copy of the brush clip and is never reached by the
     * directed checks otherwise. Paint at the bottom-right corner, where an
     * off-by-one reads past the canvas, and draw both the full and incremental
     * paths. */
    static uint8_t pixels[320*240];
    struct gfx_surface surface = {pixels, 320, 240};
    struct runtime drawn;
    runtime_init(&drawn);
    native_attach_digit(&drawn);
    drawn.digit.cursor_x = drawn.digit.cursor_y = DIGIT_SIDE - 1;
    digit_ui_frame(&drawn.digit, HELD(SPACE));
    digit_ui_draw(&drawn.digit, &surface);      /* dirty: full repaint */
    digit_ui_draw(&drawn.digit, &surface);      /* clean: incremental brush path */
    digit_ui_frame(&drawn.digit, HELD(LEFT) | HELD(SPACE));
    digit_ui_draw(&drawn.digit, &surface);
    digit_ui_event(&drawn.digit, RV32_KEY_ENTER);
    digit_ui_draw(&drawn.digit, &surface);      /* the result panel and the bars */
    digit_ui_attach(&drawn.digit, failing_classify);
    digit_ui_event(&drawn.digit, RV32_KEY_ENTER);
    digit_ui_draw(&drawn.digit, &surface);      /* the DEVICE ERROR panel */
    CHECK(drawn.digit.cursor_x == DIGIT_SIDE - 2 && drawn.digit.cursor_y == DIGIT_SIDE - 1);
    return 0;
}

/* G2: SPACE cycles the three shaders, P freezes the frame counter, R restarts,
 * the attached renderer draws each frame, and a failing renderer is reported. */
static uint32_t render_calls;
static uint32_t counting_render(void *context, const struct g3d_job *job, const struct gfx_surface *s)
{
    (void)context;
    render_calls++;
    struct g3d_counts counts;
    for (uint32_t i = 0; i < 320*240; i++) native_zbuf[i] = 0xffff;
    gfx_clear(s, G3D_DEMO_BACKGROUND);
    return g3d_reference(s->pixels, native_zbuf, job, &counts);
}
static uint32_t failing_render(void *context, const struct g3d_job *job, const struct gfx_surface *s)
{
    (void)context; (void)job; (void)s;
    return G3D_E_LIMIT;
}

int check_g3d_screen(void)
{
    static uint8_t pixels[320*240];
    struct gfx_surface surface = {pixels, 320, 240};
    struct runtime r;
    runtime_init(&r);
    g3d_demo_attach(&r.g3d, counting_render, 0);
    r.selected = 3;
    runtime_event(&r, PRESS(ENTER));
    CHECK(r.screen == RUNTIME_3D);
    runtime_event(&r, RELEASE(ENTER));
    runtime_frame(&r, 0);
    runtime_draw(&r, &surface);
    CHECK(render_calls == 1 && r.g3d.status == 0 && r.g3d.frame == 1);
    /* The cube is drawn: most of the screen is background, but not all of it. */
    uint32_t cube = 0;
    for (uint32_t i = 0; i < 320*240; i++) cube += pixels[i] != G3D_DEMO_BACKGROUND;
    CHECK(cube > 5000 && cube < 40000);
    for (uint32_t shader = 1; shader <= G3D_SHADERS; shader++) {
        runtime_event(&r, PRESS(SPACE));
        CHECK(r.g3d.shader == shader % G3D_SHADERS);
        runtime_event(&r, RELEASE(SPACE));
    }
    runtime_event(&r, PRESS(SPACE));             /* toon, so R has a shader to reset */
    runtime_event(&r, RELEASE(SPACE));
    CHECK(r.g3d.shader == G3D_SHADER_TOON);
    runtime_event(&r, PRESS(P));
    runtime_event(&r, RELEASE(P));
    runtime_frame(&r, 0);
    runtime_frame(&r, 0);
    CHECK(r.g3d.paused && r.g3d.frame == 1);
    runtime_event(&r, PRESS(R));
    CHECK(!r.g3d.paused && r.g3d.frame == 0 && r.g3d.shader == 0);
    runtime_event(&r, RELEASE(R));
    /* Constants: a rotation keeps the object-space light at unit length (within Q16.16 rounding). */
    uint32_t k[G3D_CONSTS];
    for (uint32_t frame = 0; frame < 128; frame += 9) {
        g3d_demo_constants(frame, k);
        int64_t len = (int64_t)(int32_t)k[16]*(int32_t)k[16] + (int64_t)(int32_t)k[17]*(int32_t)k[17] +
                      (int64_t)(int32_t)k[18]*(int32_t)k[18];
        CHECK(len > ((int64_t)65536*65536*99)/100 && len < ((int64_t)65536*65536*101)/100);
        CHECK(k[15] == 196608u && k[20] == frame);
    }
    g3d_demo_attach(&r.g3d, failing_render, 0);
    runtime_draw(&r, &surface);
    CHECK(r.g3d.status == G3D_E_LIMIT);
    uint32_t before = runtime_checksum(&r);
    r.g3d.status = 0;
    CHECK(runtime_checksum(&r) != before);   /* a device failure is part of the checked state */
    return 0;
}

/* The menu has five entries since G2, so selection wraps over five with compares. */
int check_digit_menu(void)
{
    struct runtime r;
    runtime_init(&r);
    native_attach_digit(&r);
    CHECK(r.selected == 0);
    runtime_event(&r, PRESS(UP));                  /* wraps back to the last entry */
    CHECK(r.selected == RUNTIME_ENTRIES - 1);
    runtime_event(&r, RELEASE(UP));
    runtime_event(&r, PRESS(DOWN));                /* and forward to the first */
    CHECK(r.selected == 0);
    runtime_event(&r, RELEASE(DOWN));
    for (uint32_t i = 1; i < RUNTIME_ENTRIES; i++) {
        runtime_event(&r, PRESS(DOWN));
        CHECK(r.selected == i);
        runtime_event(&r, RELEASE(DOWN));
    }
    /* Entry order is menu order: the third entry is the digit screen. */
    r.selected = 2;
    runtime_event(&r, PRESS(ENTER));
    CHECK(r.screen == RUNTIME_DIGIT);
    CHECK(native_screen(&r) == RUNTIME_DIGIT);
    runtime_event(&r, RELEASE(ENTER));
    runtime_frame(&r, 0);
    runtime_event(&r, PRESS(ESCAPE));
    CHECK(r.screen == RUNTIME_MENU);
    /* Returning to the menu blocks every key and sets the transition flag, so the
     * next frame has to run before another selection is accepted. */
    runtime_event(&r, RELEASE(ESCAPE));
    runtime_frame(&r, 0);
    /* The last entry is still the 2D demo, which the graphics replay selects by
     * pressing UP from the first entry; G2 inserted the 3D screen before it. */
    r.selected = 4;
    runtime_event(&r, PRESS(ENTER));
    CHECK(r.screen == RUNTIME_GPU);

    /* The literal count, so raising RUNTIME_ENTRIES cannot pass this check by
     * changing both sides of it. */
    CHECK(RUNTIME_ENTRIES == 5);
    CHECK(RUNTIME_GPU == RUNTIME_3D + 1 && RUNTIME_3D == RUNTIME_DIGIT + 1 && RUNTIME_DIGIT == RUNTIME_TETRIS + 1);

    /* Re-entering the digit screen clears the canvas but keeps the classifier. */
    struct runtime again;
    runtime_init(&again);
    native_attach_digit(&again);
    again.selected = 2;
    runtime_event(&again, PRESS(ENTER));
    CHECK(again.screen == RUNTIME_DIGIT);
    runtime_event(&again, RELEASE(ENTER));
    runtime_frame(&again, 0);
    again.digit.cursor_x = again.digit.cursor_y = 4;
    digit_ui_frame(&again.digit, HELD(SPACE));
    digit_ui_event(&again.digit, RV32_KEY_ENTER);
    CHECK(again.digit.runs == 1);
    runtime_event(&again, PRESS(ESCAPE));
    CHECK(again.screen == RUNTIME_MENU);
    runtime_event(&again, RELEASE(ESCAPE));
    runtime_frame(&again, 0);
    again.selected = 2;
    runtime_event(&again, PRESS(ENTER));
    CHECK(again.screen == RUNTIME_DIGIT);
    for (uint32_t i = 0; i < DIGIT_PIXELS; i++) CHECK(again.digit.canvas[i] == 0);
    CHECK(again.digit.runs == 0 && again.digit.classified == 0);
    CHECK(again.digit.classify != 0);       /* the clear must not detach it */
    digit_ui_event(&again.digit, RV32_KEY_ENTER);
    CHECK(again.digit.runs == 1);
    return 0;
}

#ifdef RV32_NATIVE_MAIN
#include <stdio.h>
int main(void)
{
    int (*checks[])(void)={check_shapes,check_collisions,check_clear_score,check_timing,
                          check_random_restart,check_transitions,check_quit_batches,check_render,check_runtime_render,check_digit_ui,check_digit_menu,check_g3d_screen};
    for (unsigned i=0; i<sizeof checks/sizeof checks[0]; i++) {
        int line=checks[i]();
        if (line) { fprintf(stderr,"native check %u failed at line %d\n",i,line); return 1; }
    }
    puts("all native checks passed under ASan/UBSan");
    return 0;
}
#endif
