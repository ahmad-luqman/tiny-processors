/* The digit model on the SIMD4 accelerator: the CPU streams operands, the engine
 * multiplies and accumulates, the CPU requantizes. Every arithmetic decision
 * outside the two dot products stays in digit_model.c, so this path and the
 * software path can differ only where the hardware actually does the work.
 */
#ifndef RV32_DIGIT_HW_H
#define RV32_DIGIT_HW_H
#include <stdint.h>
#include <stdbool.h>
#include "digit_model.h"

enum digit_status {
    DIGIT_OK,
    DIGIT_FAULT,        /* the engine finished with FAULT set */
    DIGIT_TIMEOUT,      /* the poll budget ran out; simd4_wait has reset the device */
    DIGIT_REFUSED,      /* a driver call was refused, so the device was not idle */
};

/* Load the program bank and zero the data window. Safe to call repeatedly: the
 * screen can be entered, left and re-entered, and only the first call works. */
enum digit_status digit_hw_init(void);

/* Classify one preprocessed input. Initializes on first use, so nothing touches
 * the accelerator until a classification is actually asked for; an image that
 * never classifies keeps its trace free of accelerator accesses. */
enum digit_status digit_hw_infer(const uint8_t x[DIGIT_INPUTS], int32_t logits[DIGIT_CLASSES]);

/* Device counters accumulated over the launches of the last inference. Cycles
 * and stalls differ between backends by design, so they are measurements only:
 * never print, draw or checksum them. Transfers and instructions are
 * deterministic and are checked against the kernel's predicted counts. */
struct digit_counters { uint32_t launches, cycles, stalls, transfers, instructions; };
/* False when the last inference did not finish, in which case the counters are a
 * partial sum: without this a caller cannot tell 17 launches from 35. */
bool digit_hw_counters(struct digit_counters *out);

/* Forget that the device was initialized, so the next inference reloads it. Any
 * code that loads its own program into the accelerator must call this, or the next
 * inference launches whatever that code left behind. A fault or a timeout inside
 * digit_hw_infer already does it: both mean program memory is no longer what this
 * driver believes, and a reload is the only thing that can fix that. */
void digit_hw_forget(void);
#endif
