/* doom_rv32: doomgeneric's platform hooks for the RV32 OS (issue #35, docs/rv32-doom.md).
 *
 * doomgeneric (third_party/doomgeneric, unmodified) is built with CMAP256, so its screen buffer
 * holds 320x200 palette indices and its palette is the global `colors[]`, flagged by
 * `palette_changed`. A frame is copied into the middle 200 rows of the 320x240 framebuffer and
 * presented; a changed palette goes to the display's palette through SYS_PALETTE first. Keys come
 * from the kernel's input events.
 *
 * Doom's clock is virtual: DG_GetTicksMs returns a count of milliseconds that only DG_SleepMs
 * advances. Doom waits for its clock in exactly two places, TryRunTics before a tic and the screen
 * wipe between frames, and both sleep a millisecond at a time while they wait, so the game still
 * runs one tic per frame, and how many frames a wipe draws no longer depends on how much device
 * time a frame took. That differs between QEMU, the emulator and the RTL, so with mtime the same
 * demo would draw different frames on each. In the window, presents are paced (rv32win --fps 35),
 * which sets the game's real speed, as Pong's is.
 *
 * `-frames N` ends the run after N frames. Every 35 frames, and at the end, it prints the frame
 * number and the hashes of the screen buffer and of the palette (the checkpoint hash of
 * docs/rv32.md: h = h*33 ^ word from 5381), which it computes itself, so QEMU, which has no
 * display, prints the same lines. `-fps` adds the frame rate in mtime at the end; that line depends
 * on the backend, so the pinned sessions leave it out.
 */
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <time.h>

#include "doomgeneric.h"
#include "doomkeys.h"
#include "i_video.h"
#include "m_argv.h"
#include "ulib.h"

#define FB_COLUMNS 320u
#define FB_ROWS 240u
#define TOP ((FB_ROWS - DOOMGENERIC_RESY) / 2u) /* rows of black above and below the picture */
#define REPORT_EVERY 35u                        /* frames: one second of game time */

_Static_assert(DOOMGENERIC_RESX == FB_COLUMNS && DOOMGENERIC_RESY <= FB_ROWS, "the picture fits the framebuffer");

static uint8_t *framebuffer;  /* 0 without a display (QEMU) */
static uint32_t palette_words[256];
static uint32_t virtual_ms = 1; /* from 1: i_timer.c takes a first reading of 0 as "not started" */
static uint32_t frames, frame_limit, start_clock;
static int report_fps;

static uint32_t word_hash(const uint32_t *words, uint32_t count)
{
    uint32_t h = 5381u;
    for (uint32_t i = 0; i < count; i++) {
        h = ((h << 5) + h) ^ words[i];
    }
    return h;
}

static void report(void)
{
    printf("doom: frame %lu screen %08lx palette %08lx\n", (unsigned long)frames,
           (unsigned long)word_hash((const uint32_t *)DG_ScreenBuffer, DOOMGENERIC_RESX * DOOMGENERIC_RESY / 4u),
           (unsigned long)word_hash(palette_words, 256));
}

void DG_Init(void)
{
    framebuffer = sys_display();
    int p = M_CheckParmWithArgs("-frames", 1);
    if (p) {
        frame_limit = (uint32_t)strtoul(myargv[p + 1], 0, 10);
    }
    report_fps = M_CheckParm("-fps") != 0;
    start_clock = (uint32_t)clock();
}

void DG_DrawFrame(void)
{
    if (palette_changed) {
        for (uint32_t i = 0; i < 256; i++) {
            palette_words[i] = (uint32_t)colors[i].r << 16 | (uint32_t)colors[i].g << 8 | colors[i].b;
        }
        sys_set_palette(palette_words); /* an error without a display, which changes nothing */
        palette_changed = false;
    }
    if (framebuffer) {
        memcpy(framebuffer + TOP * FB_COLUMNS, DG_ScreenBuffer, DOOMGENERIC_RESX * DOOMGENERIC_RESY);
        sys_present();
    }
    frames++;
    if (frames % REPORT_EVERY == 0 || frames == frame_limit) {
        report();
    }
    if (frames == frame_limit) {
        if (report_fps) {
            uint32_t ms = (uint32_t)(((uint64_t)((uint32_t)clock() - start_clock) * 1000u) / CLOCKS_PER_SEC);
            printf("doom: %lu frames in %lu ms of device time\n", (unsigned long)frames, (unsigned long)ms);
        }
        exit(0);
    }
}

void DG_SleepMs(uint32_t ms)
{
    virtual_ms += ms;
}

uint32_t DG_GetTicksMs(void)
{
    return virtual_ms;
}

/* Our key codes (board.h) to Doom's; 0 for a key Doom is not given. */
static unsigned char doom_key(uint32_t code)
{
    switch (code) {
    case 1: return KEY_LEFTARROW;   /* LEFT */
    case 2: return KEY_RIGHTARROW;  /* RIGHT */
    case 3: return KEY_UPARROW;     /* UP */
    case 4: return KEY_DOWNARROW;   /* DOWN */
    case 5: return KEY_USE;         /* SPACE: open doors, press switches */
    case 6: return KEY_ENTER;       /* ENTER */
    case 7: return KEY_ESCAPE;      /* ESCAPE: the menu */
    case 8: return KEY_STRAFE_L;    /* A */
    case 9: return KEY_STRAFE_R;    /* D */
    case 10: return KEY_UPARROW;    /* W */
    case 11: return KEY_DOWNARROW;  /* S */
    case 12: return KEY_PAUSE;      /* P */
    case 15: return KEY_FIRE;       /* CTRL */
    case 16: return KEY_RSHIFT;     /* SHIFT: run */
    case 17: return KEY_TAB;        /* TAB: the automap */
    case 18: return 'y';            /* Y */
    case 19: return 'n';            /* N */
    default:
        return code >= 20 && code <= 26 ? (unsigned char)('1' + code - 20) : 0; /* DIGIT1..DIGIT7: weapons */
    }
}

int DG_GetKey(int *pressed, unsigned char *key)
{
    for (;;) {
        uint32_t event = sys_event();
        if (!event) {
            return 0;
        }
        unsigned char k = doom_key(event & 31u);
        if (k) {
            *pressed = (event >> 8) & 1u;
            *key = k;
            return 1;
        }
    }
}

void DG_SetWindowTitle(const char *title)
{
    (void)title;
}

int main(int argc, char **argv)
{
    doomgeneric_Create(argc, argv);
    for (;;) {
        doomgeneric_Tick();
    }
}
