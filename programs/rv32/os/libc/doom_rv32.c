/* doom_rv32: doomgeneric's platform hooks for the RV32 OS (issue #35, docs/rv32-doom.md).
 *
 * doomgeneric (third_party/doomgeneric, unmodified) is built with CMAP256, so its screen buffer
 * holds 320x200 palette indices and its palette is the global `colors[]`, flagged by
 * `palette_changed`. A frame is copied into the middle 200 rows of the 320x240 framebuffer and
 * presented; a changed palette goes to the display's palette through SYS_PALETTE first, and is read
 * back and compared, so a palette that does not reach the device stops the run. Keys come from the
 * kernel's input events.
 *
 * Doom's clock is virtual: DG_GetTicksMs returns a count of milliseconds that only DG_SleepMs
 * advances. Doom waits for its clock in two places, TryRunTics before a tic and the screen wipe
 * when the screen changes (the title, a new level, an intermission), and both sleep a millisecond
 * at a time while they wait. So every present, wipe frames included, comes one tic after the last,
 * and how many frames a wipe draws no longer depends on how much device time a frame took. That
 * differs between QEMU, the emulator and the RTL, so with mtime the same demo would draw different
 * frames on each. (A timedemo never waits in TryRunTics: it runs one tic per frame by itself. In
 * play, the first frame after a wipe may run the tics the wipe let pass.) In the window, presents
 * are paced (rv32win --fps 35), which sets the game's real speed, as Pong's is.
 *
 * `-frames N` (N > 0) ends the run after N frames. Every 35 frames, and at the end, it prints the
 * frame number and the hashes of the screen buffer and of the palette (the checkpoint hash of
 * docs/rv32.md: h = h*33 ^ word from 5381), which it computes itself, so QEMU, which has no
 * display, prints the same lines. `-fps` adds a line with the device ticks (the low word of mtime)
 * from the first frame's present to the last's, read straight after each present, so neither
 * start-up nor the reports are counted; it needs at least two frames. It depends on the backend,
 * so the cross-backend sessions leave it out.
 */
#include <errno.h>
#include <stdbool.h>
#include <stdint.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>

#include "board.h"
#include "doomgeneric.h"
#include "doomkeys.h"
#include "i_system.h"
#include "i_video.h"
#include "m_argv.h"
#include "ulib.h"

#define FB_COLUMNS 320u
#define FB_ROWS 240u
#define TOP ((FB_ROWS - DOOMGENERIC_RESY) / 2u) /* rows of black above and below the picture */
#define REPORT_EVERY 35u                        /* frames: one second of game time */

_Static_assert(DOOMGENERIC_RESX == FB_COLUMNS && DOOMGENERIC_RESY <= FB_ROWS, "the picture fits the framebuffer");

static uint8_t *framebuffer;  /* 0 without a display (QEMU) */
static uint32_t palette_words[OS_PALETTE_ENTRIES], device_palette[OS_PALETTE_ENTRIES];
static uint32_t virtual_ms = 1; /* from 1: i_timer.c takes a first reading of 0 as "not started" */
static uint32_t frames;
static uint32_t frame_limit;    /* 0: no -frames, so no limit (frames is never 0 when it is compared) */
static uint32_t first_ticks; /* mtime's low word at the first frame's present */
static bool report_ticks;

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
           (unsigned long)word_hash(palette_words, OS_PALETTE_ENTRIES));
}

void DG_Init(void)
{
    framebuffer = sys_display();
    if (M_CheckParm("-frames")) {
        int p = M_CheckParmWithArgs("-frames", 1);
        char *end = 0;
        errno = 0;
        unsigned long n = p ? strtoul(myargv[p + 1], &end, 10) : 0;
        if (!p || end == myargv[p + 1] || *end || n == 0 || errno == ERANGE || myargv[p + 1][0] == '-') {
            I_Error("doom: -frames needs a count of frames greater than 0");
        }
        frame_limit = (uint32_t)n;
    }
    report_ticks = M_CheckParm("-fps") != 0;
    if (framebuffer) {
        /* Another program may have drawn here: the bands above and below the picture start black
         * (index 0 is black in Doom's palettes, and the power-on palette's too). */
        memset(framebuffer, 0, FB_COLUMNS * FB_ROWS);
    }
}

/* Send a changed palette to the display and read it back: on a machine with a display, a palette
 * that does not arrive is an error, not a run that draws the wrong colours unnoticed. */
static void send_palette(void)
{
    for (uint32_t i = 0; i < OS_PALETTE_ENTRIES; i++) {
        palette_words[i] = (uint32_t)colors[i].r << 16 | (uint32_t)colors[i].g << 8 | colors[i].b;
    }
    if (!framebuffer) {
        return; /* QEMU: no display, no palette; the hash still covers Doom's own */
    }
    if (sys_set_palette(palette_words) == SYS_ERROR || sys_get_palette(device_palette) == SYS_ERROR ||
        memcmp(device_palette, palette_words, sizeof palette_words) != 0) {
        I_Error("doom: the display's palette does not hold Doom's");
    }
}

void DG_DrawFrame(void)
{
    if (palette_changed) {
        send_palette();
        palette_changed = false;
    }
    if (framebuffer) {
        memcpy(framebuffer + TOP * FB_COLUMNS, DG_ScreenBuffer, DOOMGENERIC_RESX * DOOMGENERIC_RESY);
        if (sys_present() == SYS_ERROR) {
            I_Error("doom: the display refused a present");
        }
    }
    frames++;
    bool last = frames == frame_limit;
    uint32_t now = frames == 1 || last ? sys_time() : 0; /* before any reporting work */
    if (frames == 1) {
        first_ticks = now;
    }
    if (frames % REPORT_EVERY == 0 || last) {
        report();
    }
    if (last) {
        if (report_ticks && frames >= 2) {
            printf("doom: frames 2 to %lu in %lu device ticks\n", (unsigned long)frames, (unsigned long)(now - first_ticks));
        } else if (report_ticks) {
            printf("doom: -fps needs two frames or more\n");
        }
        /* exit(), not I_Quit(): Doom's own exit handlers stay out of a measured run, so it saves
         * no config and a timedemo's end-of-demo report (an I_Error) never comes. */
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

/* Our key codes (board.h) to Doom's (doomkeys.h, and its defaults in m_controls.c); 0 for a key
 * Doom is not given (Q and R). */
static unsigned char doom_key(uint32_t code)
{
    switch (code) {
    case RV32_KEY_LEFT: return KEY_LEFTARROW;
    case RV32_KEY_RIGHT: return KEY_RIGHTARROW;
    case RV32_KEY_UP: return KEY_UPARROW;
    case RV32_KEY_DOWN: return KEY_DOWNARROW;
    case RV32_KEY_SPACE: return KEY_USE;          /* open doors, press switches */
    case RV32_KEY_ENTER: return KEY_ENTER;
    case RV32_KEY_ESCAPE: return KEY_ESCAPE;      /* the menu */
    case RV32_KEY_A: return KEY_STRAFE_L;
    case RV32_KEY_D: return KEY_STRAFE_R;
    case RV32_KEY_W: return KEY_UPARROW;
    case RV32_KEY_S: return KEY_DOWNARROW;
    case RV32_KEY_P: return KEY_PAUSE;
    case RV32_KEY_CTRL: return KEY_FIRE;
    case RV32_KEY_SHIFT: return KEY_RSHIFT;       /* run */
    case RV32_KEY_TAB: return KEY_TAB;            /* the automap */
    case RV32_KEY_Y: return 'y';
    case RV32_KEY_N: return 'n';
    default:
        break;
    }
    if (code >= RV32_KEY_DIGIT1 && code <= RV32_KEY_DIGIT7) {
        return (unsigned char)('1' + (code - RV32_KEY_DIGIT1)); /* the weapons */
    }
    return 0;
}

int DG_GetKey(int *pressed, unsigned char *key)
{
    for (;;) {
        uint32_t event = sys_event();
        if (!event) {
            return 0;
        }
        unsigned char k = doom_key(event & RV32_EVENT_CODE_MASK);
        if (k) {
            *pressed = (event >> RV32_EVENT_PRESS_SHIFT) & 1u;
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
