#include "digit_ui.h"
#include "board.h"

#define INK 0xffu
#define BLANK 0u
#define CURSOR 0xe0u
#define FRAME_COLOR 0x49u
#define LABEL 0x92u
#define RESULT 0x1fu

void digit_ui_clear(struct digit_ui *ui)
{
    for (uint32_t i = 0; i < DIGIT_PIXELS; i++) ui->canvas[i] = 0;
    for (uint32_t c = 0; c < DIGIT_CLASSES; c++) ui->logits[c] = 0;
    ui->cursor_x = ui->cursor_y = DIGIT_SIDE / 2;
    ui->drawn_x = ui->cursor_x;
    ui->drawn_y = ui->cursor_y;
    ui->repeat = ui->painting = ui->classified = 0;
    ui->predicted = ui->margin = ui->status = ui->runs = 0;
    ui->dirty = 1;
}

void digit_ui_init(struct digit_ui *ui)
{
    digit_ui_clear(ui);
    ui->classify = 0;
}

void digit_ui_attach(struct digit_ui *ui, digit_classify classify) { ui->classify = classify; }

/* A 2x2 brush: a one-cell stroke pools away to almost nothing, and the training
 * digits are several pixels wide. */
static void paint(struct digit_ui *ui)
{
    for (uint32_t dy = 0; dy < 2; dy++) {
        uint32_t y = ui->cursor_y + dy;
        if (y >= DIGIT_SIDE) continue;
        for (uint32_t dx = 0; dx < 2; dx++) {
            uint32_t x = ui->cursor_x + dx;
            if (x >= DIGIT_SIDE) continue;
            ui->canvas[y * DIGIT_SIDE + x] = INK;
        }
    }
}

void digit_ui_event(struct digit_ui *ui, uint32_t code)
{
    if (code == RV32_KEY_R) {
        digit_ui_clear(ui);
        return;
    }
    if (code != RV32_KEY_ENTER || !ui->classify) return;
    ui->status = ui->classify(ui->canvas, ui->logits);
    ui->runs++;
    ui->classified = 1;
    if (ui->status) {
        /* A failed classification leaves `logits` partially written, so give
         * failure one representation instead of checksumming a mix of this run
         * and the last one. */
        for (uint32_t c = 0; c < DIGIT_CLASSES; c++) ui->logits[c] = 0;
        ui->predicted = ui->margin = 0;
    } else {
        ui->predicted = digit_argmax(ui->logits, &ui->margin);
    }
    ui->dirty = 1;
}

void digit_ui_frame(struct digit_ui *ui, uint32_t keys)
{
    uint32_t moving = keys & ((1u << RV32_KEY_LEFT) | (1u << RV32_KEY_RIGHT) |
                              (1u << RV32_KEY_UP) | (1u << RV32_KEY_DOWN));
    if (!moving) {
        ui->repeat = 0;
    } else if (ui->repeat) {
        ui->repeat--;
    } else {
        if ((keys & (1u << RV32_KEY_LEFT)) && ui->cursor_x) ui->cursor_x--;
        if ((keys & (1u << RV32_KEY_RIGHT)) && ui->cursor_x + 1 < DIGIT_SIDE) ui->cursor_x++;
        if ((keys & (1u << RV32_KEY_UP)) && ui->cursor_y) ui->cursor_y--;
        if ((keys & (1u << RV32_KEY_DOWN)) && ui->cursor_y + 1 < DIGIT_SIDE) ui->cursor_y++;
        ui->repeat = DIGIT_UI_REPEAT;
    }
    ui->painting = (keys >> RV32_KEY_SPACE) & 1u;
    if (ui->painting) paint(ui);
}

static void cell_rect(const struct gfx_surface *s, uint32_t x, uint32_t y, uint8_t color)
{
    gfx_fill_rect(s, (int32_t)(DIGIT_UI_LEFT + x * DIGIT_UI_CELL),
                  (int32_t)(DIGIT_UI_TOP + y * DIGIT_UI_CELL),
                  (int32_t)DIGIT_UI_CELL, (int32_t)DIGIT_UI_CELL, color);
}

/* Bars scaled by a shift rather than a division: the guest has no divide
 * instruction, and a shift that brings the range under the bar width is enough
 * to show which classes the model preferred. */
static void draw_scores(const struct digit_ui *ui, const struct gfx_surface *s)
{
    int32_t low = ui->logits[0], high = ui->logits[0];
    for (uint32_t c = 1; c < DIGIT_CLASSES; c++) {
        if (ui->logits[c] < low) low = ui->logits[c];
        if (ui->logits[c] > high) high = ui->logits[c];
    }
    /* Unsigned subtraction, for the reason digit_model.h gives for the margin: the
     * difference of two valid int32 logits can overflow a signed subtraction, and the
     * native build runs UBSan without recovery. */
    uint32_t span = (uint32_t)high - (uint32_t)low, shift = 0;
    while ((span >> shift) > 88u && shift < 31u) shift++;
    for (uint32_t c = 0; c < DIGIT_CLASSES; c++) {
        int32_t y = (int32_t)(150u + c * 8u);
        uint32_t length = ((uint32_t)ui->logits[c] - (uint32_t)low) >> shift;
        gfx_draw_digit(s, (int32_t)DIGIT_UI_PANEL, y, c, 1, LABEL);
        gfx_fill_rect(s, (int32_t)DIGIT_UI_PANEL + 6, y, 90, 5, 0);
        gfx_fill_rect(s, (int32_t)DIGIT_UI_PANEL + 6, y, (int32_t)length, 5,
                      c == ui->predicted ? RESULT : FRAME_COLOR);
    }
}

void digit_ui_draw(struct digit_ui *ui, const struct gfx_surface *s)
{
    if (ui->dirty) {
        gfx_clear(s, 0);
        gfx_draw_text(s, 6, 6, "DRAW A DIGIT", 3, RESULT);
        for (uint32_t y = 0; y < DIGIT_SIDE; y++)
            for (uint32_t x = 0; x < DIGIT_SIDE; x++)
                cell_rect(s, x, y, ui->canvas[y * DIGIT_SIDE + x] ? INK : BLANK);
        gfx_fill_rect(s, (int32_t)DIGIT_UI_LEFT - 2, (int32_t)DIGIT_UI_TOP - 2,
                      (int32_t)(DIGIT_SIDE * DIGIT_UI_CELL) + 4, 2, FRAME_COLOR);
        gfx_fill_rect(s, (int32_t)DIGIT_UI_LEFT - 2,
                      (int32_t)(DIGIT_UI_TOP + DIGIT_SIDE * DIGIT_UI_CELL),
                      (int32_t)(DIGIT_SIDE * DIGIT_UI_CELL) + 4, 2, FRAME_COLOR);
        gfx_draw_text(s, (int32_t)DIGIT_UI_PANEL, 30, "ARROWS MOVE", 1, LABEL);
        gfx_draw_text(s, (int32_t)DIGIT_UI_PANEL, 40, "SPACE DRAW", 1, LABEL);
        gfx_draw_text(s, (int32_t)DIGIT_UI_PANEL, 50, "ENTER READ", 1, LABEL);
        gfx_draw_text(s, (int32_t)DIGIT_UI_PANEL, 60, "R CLEAR", 1, LABEL);
        gfx_draw_text(s, (int32_t)DIGIT_UI_PANEL, 70, "ESCAPE MENU", 1, LABEL);
        if (ui->classified && ui->status) {
            gfx_draw_text(s, (int32_t)DIGIT_UI_PANEL, 96, "DEVICE", 1, RESULT);
            gfx_draw_text(s, (int32_t)DIGIT_UI_PANEL, 106, "ERROR", 1, RESULT);
            gfx_draw_uint(s, (int32_t)DIGIT_UI_PANEL, 120, ui->status, 2, RESULT);
        } else if (ui->classified) {
            gfx_draw_digit(s, (int32_t)DIGIT_UI_PANEL + 20, 92, ui->predicted, 8, RESULT);
            gfx_draw_text(s, (int32_t)DIGIT_UI_PANEL, 140, "MARGIN", 1, LABEL);
            gfx_draw_uint(s, (int32_t)DIGIT_UI_PANEL + 30, 140, ui->margin, 1, LABEL);
            draw_scores(ui, s);
        } else {
            gfx_draw_text(s, (int32_t)DIGIT_UI_PANEL, 100, "PRESS", 1, LABEL);
            gfx_draw_text(s, (int32_t)DIGIT_UI_PANEL, 110, "ENTER", 1, LABEL);
        }
        ui->dirty = 0;
    } else {
        /* Only the cell the cursor left and the cells the brush just touched are
         * redrawn. Repainting the whole 196x196 canvas on every painted frame
         * would dominate an RTL replay of a drawing session. */
        if (ui->drawn_x != ui->cursor_x || ui->drawn_y != ui->cursor_y)
            cell_rect(s, ui->drawn_x, ui->drawn_y,
                      ui->canvas[ui->drawn_y * DIGIT_SIDE + ui->drawn_x] ? INK : BLANK);
        if (ui->painting) {
            for (uint32_t dy = 0; dy < 2; dy++)
                for (uint32_t dx = 0; dx < 2; dx++) {
                    uint32_t x = ui->cursor_x + dx, y = ui->cursor_y + dy;
                    if (x < DIGIT_SIDE && y < DIGIT_SIDE)
                        cell_rect(s, x, y, ui->canvas[y * DIGIT_SIDE + x] ? INK : BLANK);
                }
        }
    }
    if (!ui->canvas[ui->cursor_y * DIGIT_SIDE + ui->cursor_x])
        cell_rect(s, ui->cursor_x, ui->cursor_y, CURSOR);
    ui->drawn_x = ui->cursor_x;
    ui->drawn_y = ui->cursor_y;
}

uint32_t digit_ui_checksum(const struct digit_ui *ui)
{
    uint32_t h = 2166136261u;
#define HASH(v) h = (h ^ (uint32_t)(v)) * 16777619u
    HASH(ui->cursor_x); HASH(ui->cursor_y); HASH(ui->painting);
    HASH(ui->classified); HASH(ui->predicted); HASH(ui->margin);
    HASH(ui->status); HASH(ui->runs);
    for (uint32_t c = 0; c < DIGIT_CLASSES; c++) HASH(ui->logits[c]);
    for (uint32_t i = 0; i < DIGIT_PIXELS; i++) HASH(ui->canvas[i]);
#undef HASH
    return h;
}
