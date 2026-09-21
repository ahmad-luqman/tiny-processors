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
    tetris_frame(&t,HELD(DOWN)); tetris_event(&t,PRESS(UP)); CHECK(tetris_checksum(&t)==h);
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
    runtime_frame(&r,0); CHECK(r.screen==RUNTIME_MENU);
    runtime_frame(&r,0); runtime_event(&r,PRESS(UP)); runtime_event(&r,PRESS(ENTER)); runtime_frame(&r,0);
    r.pong.phase=PONG_OVER;
    for (uint32_t i=0; i<30; i++) runtime_frame(&r,0);
    runtime_event(&r,PRESS(R)); CHECK(r.over_frames==0 && r.pong.phase==PONG_SERVE);
    r.pong.phase=PONG_OVER;
    for (uint32_t i=0; i<181; i++) runtime_frame(&r,0);
    CHECK(r.screen==RUNTIME_MENU);
    runtime_frame(&r,0); runtime_event(&r,PRESS(Q)); CHECK(r.quit);
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
    static const uint8_t a_rows[5]={2,5,7,5,5}, arrow[5]={4,2,1,2,4};
    for (uint32_t y=0; y<5; y++) {
        CHECK(gfx_char_row('A',y)==a_rows[y]); CHECK(gfx_char_row('>',y)==arrow[y]);
        for (uint32_t n=0; n<10; n++) CHECK(gfx_char_row('0'+n,y)==gfx_glyph_row(n,y));
    }
    CHECK(gfx_char_row('A',5)==0 && gfx_char_row('?',0)==0);
    gfx_clear(&a,0); gfx_clear(&b,0);
    gfx_draw_uint(&a,0,0,UINT32_MAX,2,255); gfx_draw_text(&b,0,0,"4294967295",2,255);
    CHECK(memcmp(incremental,reference,sizeof incremental)==0);
    /* Clipping cannot touch guard pixels outside the surface. */
    uint8_t guarded[102]; memset(guarded,0x55,sizeof guarded);
    struct gfx_surface small={guarded+1,10,10};
    gfx_draw_text(&small,-2,-2,"TETRIS",2,255);
    CHECK(guarded[0]==0x55 && guarded[101]==0x55);
    return 0;
}
