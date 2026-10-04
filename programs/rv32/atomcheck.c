/* Atomics check (issue #34): C built with -march=rv32ima, one image for QEMU's
 * virt board, our emulator and the RTL.
 *
 * The compiler lowers the __atomic builtins to the A extension: a fetch-and-op
 * becomes one AMO (amoadd.w, amoand.w, amoor.w, amoxor.w, amoswap.w, and the
 * signed and unsigned amomin.w/amomax.w), and a compare-and-swap becomes an
 * lr.w/sc.w loop. The program checks each result against plain C arithmetic,
 * counts to 1000 through a compare-and-swap loop, and takes and releases a
 * test-and-set spinlock. Nothing interrupts it, so every SC here succeeds; the
 * reservation's other rules are tests/test_rv32_a.py's and rv32ua's.
 *
 * The PASS word folds every old value an atomic returned and the final memory
 * words, so the three backends print the same word.
 */
#include <stdint.h>

#include "console.h"

static uint32_t checksum = 2166136261u;
static uint32_t failures;
static uint32_t cell;
static int32_t signed_cell;
static uint32_t counter;
static uint32_t lock;

static void fold(uint32_t value)
{
    checksum = (checksum ^ value) * 16777619u; /* FNV-1a, as the other checks */
}

static void check(const char *what, uint32_t got, uint32_t want)
{
    fold(got);
    if (got != want) {
        rv32_puts("atomcheck: FAILED ");
        rv32_puts(what);
        rv32_puts(" got ");
        rv32_put_hex32(got);
        rv32_puts(" want ");
        rv32_put_hex32(want);
        rv32_putc('\n');
        failures++;
    }
}

/* Each fetch-and-op: the old value comes back and memory holds the op's result. */
static void fetch_ops(void)
{
    __atomic_store_n(&cell, 0x12345678u, __ATOMIC_RELAXED);
    check("add old", __atomic_fetch_add(&cell, 0xf0000001u, __ATOMIC_SEQ_CST), 0x12345678u);
    check("add new", cell, 0x02345679u);
    check("and old", __atomic_fetch_and(&cell, 0x0ff00ff0u, __ATOMIC_ACQUIRE), 0x02345679u);
    check("and new", cell, 0x02300670u);
    check("or old", __atomic_fetch_or(&cell, 0x80000001u, __ATOMIC_RELEASE), 0x02300670u);
    check("or new", cell, 0x82300671u);
    check("xor old", __atomic_fetch_xor(&cell, 0xffffffffu, __ATOMIC_ACQ_REL), 0x82300671u);
    check("xor new", cell, 0x7dcff98eu);
    check("swap old", __atomic_exchange_n(&cell, 0xdeadbeefu, __ATOMIC_SEQ_CST), 0x7dcff98eu);
    check("swap new", cell, 0xdeadbeefu);
    /* Unsigned here: 0xdeadbeef is large (it would be negative as signed; the signed cases below
     * use signed_cell). */
    check("minu old", __atomic_fetch_min(&cell, 0x10u, __ATOMIC_SEQ_CST), 0xdeadbeefu);
    check("minu new", cell, 0x10u);
    check("maxu old", __atomic_fetch_max(&cell, 0x80000000u, __ATOMIC_SEQ_CST), 0x10u);
    check("maxu new", cell, 0x80000000u);
    __atomic_store_n(&signed_cell, -5, __ATOMIC_RELAXED);
    check("min old", (uint32_t)__atomic_fetch_min(&signed_cell, 3, __ATOMIC_SEQ_CST), (uint32_t)-5);
    check("min new", (uint32_t)signed_cell, (uint32_t)-5);
    check("max old", (uint32_t)__atomic_fetch_max(&signed_cell, 3, __ATOMIC_SEQ_CST), (uint32_t)-5);
    check("max new", (uint32_t)signed_cell, 3u);
    check("min neg", (uint32_t)__atomic_fetch_min(&signed_cell, INT32_MIN, __ATOMIC_SEQ_CST), 3u);
    check("min neg new", (uint32_t)signed_cell, 0x80000000u);
}

/* Compare-and-swap: a matching expected value stores, a stale one returns the current value. */
static void compare_and_swap(void)
{
    uint32_t expected = 0x80000000u;
    check("cas hit", __atomic_compare_exchange_n(&cell, &expected, 7u, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST), 1u);
    check("cas hit value", cell, 7u);
    expected = 6u;
    check("cas miss", __atomic_compare_exchange_n(&cell, &expected, 9u, 0, __ATOMIC_SEQ_CST, __ATOMIC_SEQ_CST), 0u);
    check("cas miss expected", expected, 7u);
    check("cas miss value", cell, 7u);
    /* A counter incremented by compare-and-swap, as lock-free code does. */
    for (uint32_t i = 0; i < 1000u; i++) {
        uint32_t seen = __atomic_load_n(&counter, __ATOMIC_RELAXED);
        while (!__atomic_compare_exchange_n(&counter, &seen, seen + 1u, 1, __ATOMIC_SEQ_CST, __ATOMIC_RELAXED)) {
        }
    }
    check("cas counter", counter, 1000u);
}

/* A test-and-set spinlock: free, taken, refused while held, released. */
static void spinlock(void)
{
    check("lock free", __atomic_exchange_n(&lock, 1u, __ATOMIC_ACQUIRE), 0u);
    check("lock held", __atomic_exchange_n(&lock, 1u, __ATOMIC_ACQUIRE), 1u);
    __atomic_store_n(&lock, 0u, __ATOMIC_RELEASE);
    check("lock released", lock, 0u);
}

int main(void)
{
    fetch_ops();
    rv32_puts("atomcheck: fetch ops ok\n");
    compare_and_swap();
    rv32_puts("atomcheck: compare and swap ok\n");
    spinlock();
    rv32_puts("atomcheck: spinlock ok\n");
    if (failures) {
        return 1;
    }
    rv32_puts("PASS ");
    rv32_put_hex32(checksum);
    rv32_putc('\n');
    return 0;
}
