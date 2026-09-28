/* S1: the three accelerators in one image. Each case starts one engine, then
 * drives another (or breaks the contract on purpose) while the first is still
 * BUSY, and checks both outcomes exactly against the oracles the milestone
 * diagnostics already use. The overlap policy under test (docs/rv32-soc.md,
 * "Accelerator overlap"): G1 and G2 exclude each other because they share one
 * engine memory port; SIMD4 has private memories and runs alongside either.
 *
 * Every forbidden access is expected to trap, and the trap handler records it
 * and skips the instruction. Before each forbidden access the owning engine's
 * STATUS is read as BUSY, so the trap cannot depend on how far the device got
 * on a particular backend. Exit codes start at 100; lower ones belong to the
 * milestone diagnostics and 80-86 to the capstone.
 */
#include "gpu.h"
#include "g3d.h"
#include "simd4.h"
#include "digit_hw.h"
#include "board.h"
#include "mmio.h"
#include "console.h"
#include "g3d_scenes.h"
#include "digit_check.h"

#define ZBASE 0x80040000u
#define FB_WORDS (320u*240u/4u)
#define Z_WORDS (320u*240u*2u/4u)
#define CAUSE_LOAD_FAULT 5u
#define CAUSE_STORE_FAULT 7u

/* The G1 reference frame. Word aligned so it can be hashed as words. */
static uint32_t expected_words[FB_WORDS];
/* SIMD4 programs for the long and the faulting launch, as in digitcheck. */
static uint32_t simd_program[256];
static uint16_t simd_data[256] = {0x5151};
static uint32_t present_word = 1;

#define TRAPS 8
static volatile uint32_t trap_count;
static volatile uint32_t trap_cause[TRAPS], trap_tval[TRAPS];

uint32_t diag_trap(uint32_t cause, uint32_t tval, uint32_t epc)
{
    if (trap_count < TRAPS) {
        trap_cause[trap_count] = cause;
        trap_tval[trap_count] = tval;
    }
    trap_count++;
    return epc + 4;
}

extern void diag_trap_entry(void);

static int fail(unsigned code)
{
    rv32_puts("FAIL ");
    rv32_put_udec(code);
    rv32_putc('\n');
    return (int)code;
}

static void passed(const char *name)
{
    rv32_puts("case ");
    rv32_puts(name);
    rv32_puts(" ok\n");
}

/* The recorded traps since the last call must be exactly these, in order. */
static int traps_were(uint32_t count, const uint32_t *causes, const uint32_t *tvals)
{
    int ok = trap_count == count;
    for (uint32_t i = 0; ok && i < count; i++)
        ok = trap_cause[i] == causes[i] && trap_tval[i] == tvals[i];
    if (!ok) {
        rv32_puts("traps ");
        rv32_put_udec(trap_count);
        for (uint32_t i = 0; i < trap_count && i < TRAPS; i++) {
            rv32_puts(" ");
            rv32_put_udec(trap_cause[i]);
            rv32_puts("@");
            rv32_put_hex32(trap_tval[i]);
        }
        rv32_putc('\n');
    }
    trap_count = 0;
    return ok;
}

static int no_traps(void) { return traps_were(0, 0, 0); }

static uint32_t fb_hash(void) { return g3d_hash((const volatile uint32_t *)RV32_FB_BASE, FB_WORDS, 5381u); }

static uint32_t g1_status(void) { return mmio_read32(GPU_BASE + GPU_STATUS); }
static uint32_t g2_status(void) { return mmio_read32(G3D_BASE + G3D_STATUS); }
static uint32_t simd_busy(void) { return mmio_read32(RV32_SIMD4_BASE + RV32_SIMD4_STATUS) & RV32_SIMD4_BUSY; }

/* ---- G1: a full-screen fill, long enough that the CPU can act while it runs. */

static struct gpu_command fill;
static uint32_t fill_hash;

static void prepare_fill(uint8_t color)
{
    gpu_command_init(&fill, GPU_FILL, color);
    fill.p[GP_W] = 320;
    fill.p[GP_H] = 240;
    gpu_reference((uint8_t *)expected_words, 0, &fill);
    fill_hash = g3d_hash((const volatile uint32_t *)expected_words, FB_WORDS, 5381u);
}

static int fill_finished(void)
{
    return gpu_wait(1000000) == GPU_DONE && fb_hash() == fill_hash;
}

/* ---- G2: a scene from the oracle table, checked field by field as g3dcheck does. */

static int clear_z(void)
{
    return g3d_run(G3D_CLEAR_Z, 0, 0, ZBASE, 0, 200000);
}

static int start_scene(const struct g3d_scene *s)
{
    return g3d_load_program(s->program, s->program_words) && g3d_load(G3D_CONST, s->consts, G3D_CONSTS) &&
           g3d_load(G3D_VERTEX, s->inputs, s->vcount * G3D_SLOTS) &&
           g3d_load(G3D_TRIANGLE, s->triangles, s->tcount) &&
           g3d_submit(G3D_START, s->vcount, s->tcount, s->zbase, s->limit);
}

/* Blank framebuffer and depth buffer. On the emulator this alone costs more CPU
 * instructions than a long SIMD4 launch has device ticks, so cases that need
 * SIMD4 to outlast a scene clear first and launch SIMD4 afterwards. */
static int blank_scene(void)
{
    volatile uint32_t *fb = (volatile uint32_t *)RV32_FB_BASE;
    for (uint32_t i = 0; i < FB_WORDS; i++) fb[i] = 0;
    return clear_z();
}

/* Blank, then START; the caller acts while it runs. */
static int begin_scene(const struct g3d_scene *s)
{
    return blank_scene() && start_scene(s) && g2_status() == G3D_BUSY;
}

/* The job's outcome and counters; cheap, so a caller can check what else is
 * still running the moment G2 stops. */
static int scene_done(const struct g3d_scene *s)
{
    uint32_t status = g3d_wait(s->cycles + 100000u);
    const struct g3d_result *r = g3d_last_result();
    return status == s->status && r->error == s->error && r->instructions == s->instructions &&
           r->transfers == s->transfers && r->pixels == s->pixels && r->zfail == s->zfail &&
           r->culled == s->culled && r->divides == s->divides && r->cycles - r->stalls == s->cycles;
}

/* The two images. Hashing them costs more CPU time than the scene itself. */
static int scene_image(const struct g3d_scene *s)
{
    return fb_hash() == s->fb_hash && g3d_hash((const volatile uint32_t *)ZBASE, Z_WORDS, 5381u) == s->z_hash;
}

static int scene_finished(const struct g3d_scene *s) { return scene_done(s) && scene_image(s); }

/* ---- SIMD4: a full digit inference, or a raw launch that runs long or faults. */

static int infer_matches(void)
{
    uint8_t x[DIGIT_INPUTS];
    int32_t logits[DIGIT_CLASSES];
    digit_prepare(digit_check_canvas[0], x);
    if (digit_hw_infer(x, logits) != DIGIT_OK) return 0;
    for (uint32_t c = 0; c < DIGIT_CLASSES; c++)
        if (logits[c] != digit_check_logits[0][c]) return 0;
    return 1;
}

/* SETLOOP 65535 then LOOP to itself: about 131,000 device cycles, longer than
 * scene 0, so it is still running when G2 finishes on every backend. */
static int start_long_simd(void)
{
    for (uint32_t i = 0; i < 256; i++) simd_program[i] = 0;
    simd_program[0] = 0x07ffffffu;
    simd_program[1] = 0x08000001u;
    digit_hw_forget();
    return simd4_load(simd_program, simd_data) && simd4_start(0) && simd_busy();
}

static int start_faulting_simd(void)
{
    for (uint32_t i = 0; i < 256; i++) simd_program[i] = 0;
    simd_program[0] = 0xff000000u;           /* illegal opcode */
    digit_hw_forget();
    return simd4_load(simd_program, simd_data) && simd4_start(0);
}

int main(void)
{
    __asm__ volatile("csrw mtvec, %0" : : "r"(diag_trap_entry));
    const struct g3d_scene *scene = &g3d_scenes[0];
    gpu_reset();
    g3d_reset();
    simd4_reset();

    /* 1. Each engine alone, in sequence, from one image. */
    prepare_fill(0x25);
    if (!gpu_submit(&fill) || !fill_finished()) return fail(100);
    if (!begin_scene(scene) || !scene_finished(scene)) return fail(101);
    if (digit_hw_init() != DIGIT_OK || !infer_matches()) return fail(102);
    if (!no_traps()) return fail(103);
    mmio_write32(RV32_DISPLAY_BASE, present_word++);
    passed("sequential");

    /* 2. G1 START while G2 runs faults, leaves G1 idle and G2's frame exact. */
    prepare_fill(0x03);           /* the reference costs more than a scene: draw it first */
    if (!begin_scene(scene)) return fail(104);
    uint32_t g1_before = g1_status();                 /* DONE, from case 1 */
    if (!gpu_submit(&fill) || g1_status() != g1_before) return fail(105);
    if (!scene_finished(scene)) return fail(106);
    {
        const uint32_t causes[] = {CAUSE_STORE_FAULT}, tvals[] = {GPU_BASE + GPU_COMMAND};
        if (!traps_were(1, causes, tvals)) return fail(107);
    }
    passed("g1-refused-while-g2");

    /* 3. G2 START and CLEAR_Z while G1 runs fault and leave G2's status alone. */
    prepare_fill(0x1c);
    if (!gpu_submit(&fill) || g1_status() != GPU_BUSY) return fail(108);
    uint32_t before = g2_status();
    if (!start_scene(scene) || g2_status() != before) return fail(109);
    if (!g3d_submit(G3D_CLEAR_Z, 0, 0, ZBASE, 0) || g2_status() != before) return fail(110);
    if (!fill_finished()) return fail(111);
    {
        const uint32_t causes[] = {CAUSE_STORE_FAULT, CAUSE_STORE_FAULT};
        const uint32_t tvals[] = {G3D_BASE + G3D_COMMAND, G3D_BASE + G3D_COMMAND};
        if (!traps_were(2, causes, tvals)) return fail(112);
    }
    passed("g2-refused-while-g1");

    /* 4. While G2 runs, the framebuffer, PRESENT and the depth buffer belong to it. */
    if (!begin_scene(scene)) return fail(113);
    mmio_write32(RV32_FB_BASE + 64, 0xffffffffu);
    (void)mmio_read32(RV32_FB_BASE + 128);
    mmio_write32(RV32_DISPLAY_BASE, 1);
    mmio_write32(ZBASE + 8, 0);
    if (g2_status() != G3D_BUSY) return fail(114);   /* all four happened while it ran */
    if (!scene_finished(scene)) return fail(115);
    {
        const uint32_t causes[] = {CAUSE_STORE_FAULT, CAUSE_LOAD_FAULT, CAUSE_STORE_FAULT, CAUSE_STORE_FAULT};
        const uint32_t tvals[] = {RV32_FB_BASE + 64, RV32_FB_BASE + 128, RV32_DISPLAY_BASE, ZBASE + 8};
        if (!traps_were(4, causes, tvals)) return fail(116);
    }
    passed("g2-owns-display");

    /* 5. SIMD4 runs a whole inference while G2 draws; both results exact. */
    if (!begin_scene(scene)) return fail(117);
    if (!infer_matches()) return fail(118);
    if (!scene_finished(scene) || !no_traps()) return fail(119);
    passed("simd4-alongside-g2");

    /* 6. SIMD4 runs a whole inference while G1 fills; both results exact. */
    prepare_fill(0x92);
    if (!gpu_submit(&fill) || g1_status() != GPU_BUSY) return fail(120);
    if (!infer_matches()) return fail(121);
    if (!fill_finished() || !no_traps()) return fail(122);
    passed("simd4-alongside-g1");

    /* 7. A busy SIMD4 does not own the display: the CPU may draw and present. And
     *    G2 runs a whole scene start to finish inside one SIMD4 launch. */
    if (!start_long_simd()) return fail(123);
    mmio_write32(RV32_FB_BASE, 0x01020304u);
    if (mmio_read32(RV32_FB_BASE) != 0x01020304u || !simd_busy()) return fail(124);
    mmio_write32(RV32_DISPLAY_BASE, present_word++);
    if (simd4_wait(0) != SIMD4_TIMEOUT) return fail(125);
    if (!blank_scene() || !start_long_simd() || !start_scene(scene) || !scene_done(scene)) return fail(126);
    if (!simd_busy()) return fail(127);              /* the overlap covered the whole scene */
    if (simd4_wait(0) != SIMD4_TIMEOUT || simd_busy() || !scene_image(scene)) return fail(128);
    if (!no_traps()) return fail(129);
    passed("g2-inside-simd4");

    /* 8. Resetting one engine leaves the others' work alone: G1 RESET and SIMD4
     *    RESET during a G2 scene, G2 RESET during a G1 fill and a SIMD4 launch. */
    if (!begin_scene(scene) || !start_long_simd()) return fail(130);
    gpu_reset();
    simd4_reset();
    if (simd_busy() || g2_status() != G3D_BUSY) return fail(131);
    if (!scene_finished(scene)) return fail(132);
    prepare_fill(0x49);
    if (!start_long_simd() || !gpu_submit(&fill) || g1_status() != GPU_BUSY) return fail(133);
    g3d_reset();
    if (!simd_busy() || g1_status() != GPU_BUSY) return fail(134);
    if (!fill_finished()) return fail(135);
    if (simd4_wait(0) != SIMD4_TIMEOUT) return fail(136);
    if (!no_traps()) return fail(137);
    passed("reset-isolation");

    /* 9. A fault in one engine leaves the others' work alone: a SIMD4 illegal
     *    opcode during a G2 scene, a G2 parameter fault during a SIMD4 launch. */
    if (!begin_scene(scene) || !start_faulting_simd()) return fail(138);
    if (simd4_wait(1000) != SIMD4_FAULT || !scene_finished(scene)) return fail(139);
    if (!start_long_simd()) return fail(140);
    if (!g3d_submit(G3D_START, G3D_VMAX + 1u, 1, ZBASE, 4096) || g3d_wait(100) != G3D_FAULT ||
        g3d_last_result()->error != G3D_E_PARAM) return fail(141);
    if (!simd_busy() || simd4_wait(0) != SIMD4_TIMEOUT) return fail(142);
    if (!no_traps()) return fail(143);
    passed("fault-isolation");

    /* 10. RESET hands the shared port over: cancel a busy G2 and G1 starts at
     *     once, cancel a busy G1 and G2 starts at once, both without a trap and
     *     both exact. G1 fills the whole screen over G2's partial frame; G1's partial
     *     fill is black, so G2 still starts from a blank screen. */
    prepare_fill(0x6d);
    if (!begin_scene(scene)) return fail(144);
    g3d_reset();
    if (g2_status() != G3D_IDLE || !gpu_submit(&fill) || g1_status() != GPU_BUSY) return fail(145);
    if (!fill_finished()) return fail(146);
    prepare_fill(0);                  /* black, so its partial frame is still a blank one */
    if (!blank_scene() || !gpu_submit(&fill) || g1_status() != GPU_BUSY) return fail(147);
    gpu_reset();
    if (g1_status() != GPU_IDLE || !start_scene(scene) || g2_status() != G3D_BUSY) return fail(148);
    if (!scene_finished(scene) || !no_traps()) return fail(149);
    passed("reset-hands-over-port");

    /* 11. After all of that, each engine still produces its exact result. */
    if (!infer_matches()) return fail(150);
    if (!begin_scene(scene) || !scene_finished(scene)) return fail(151);
    mmio_write32(RV32_DISPLAY_BASE, present_word++);
    if (!no_traps()) return fail(152);
    passed("recovered");
    rv32_puts("PASS S1\n");
    return 0;
}
