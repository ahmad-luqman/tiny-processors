/* The digit screen: draw a digit with the keyboard, then classify it.
 *
 * This file knows nothing about the accelerator or any device. Classification
 * goes through a callback the host installs, so the guest firmware can run the
 * software model and the hardware model and compare them, while the native tests
 * install the software model alone. The runtime therefore stays device-free.
 */
#ifndef RV32_DIGIT_UI_H
#define RV32_DIGIT_UI_H
#include <stdint.h>
#include "digit_model.h"
#include "gfx.h"

/* Fill `logits` for the 28x28 canvas and return zero on success. A non-zero
 * value is shown on screen and folded into the checksum, so a device failure
 * cannot be mistaken for a classification. */
typedef uint32_t (*digit_classify)(const uint8_t canvas[DIGIT_PIXELS], int32_t logits[DIGIT_CLASSES]);

#define DIGIT_UI_CELL 7u                 /* screen pixels per canvas cell */
#define DIGIT_UI_LEFT 6u
#define DIGIT_UI_TOP 30u
#define DIGIT_UI_PANEL 214u
#define DIGIT_UI_REPEAT 4u               /* frames between moves while a key is held */

struct digit_ui {
    uint8_t canvas[DIGIT_PIXELS];
    int32_t logits[DIGIT_CLASSES];
    uint32_t cursor_x, cursor_y, drawn_x, drawn_y;
    uint32_t repeat, painting, classified, predicted, margin, status, runs, dirty;
    digit_classify classify;
};

void digit_ui_init(struct digit_ui *ui);
/* Install the classifier. Kept separate from init so the runtime never has to
 * know which one it is running with. */
void digit_ui_attach(struct digit_ui *ui, digit_classify classify);
void digit_ui_event(struct digit_ui *ui, uint32_t code);
void digit_ui_frame(struct digit_ui *ui, uint32_t keys);
void digit_ui_draw(struct digit_ui *ui, const struct gfx_surface *s);
uint32_t digit_ui_checksum(const struct digit_ui *ui);
#endif
