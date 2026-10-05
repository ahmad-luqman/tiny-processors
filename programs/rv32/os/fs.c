/* The kernel's file system; see fs.h and tools/rv32_mkfs.py. A write, and a
 * read of part of a sector, goes through one sector buffer, so a partial sector
 * is read, changed and written back; since issue #35 a read of whole sectors
 * into a word-aligned buffer in RAM goes straight there (direct_sectors). */
#include "fs.h"

#include "sys.h"
#include "virtio.h"

#define MAGIC 0x31534654u
#define DIRECTORY 1u
#define DATA 2u
#define ENTRIES 16u

struct entry {
    char name[FS_NAME];
    uint32_t first, capacity, size;
};
_Static_assert(sizeof(struct entry) == 32, "a directory entry is 32 bytes");

static struct entry directory[ENTRIES] __attribute__((aligned(4)));
static uint8_t corrupt[ENTRIES]; /* entries fs_mount found inconsistent: kept on disk, never used */
static uint32_t sector_buffer[VIRTIO_SECTOR / 4];
static uint32_t sectors;
static int mounted;

static int same(const char *a, const char *b)
{
    for (uint32_t i = 0; i < FS_NAME; i++) {
        if (a[i] != b[i]) {
            return 0;
        }
        if (!a[i]) {
            return 1;
        }
    }
    return 1;
}

/* An entry in use that fs_mount found inconsistent is skipped everywhere but in the write-back. */
static int in_use(uint32_t i)
{
    return directory[i].name[0] && !corrupt[i];
}

int fs_mount(uint32_t capacity)
{
    if (!virtio_transfer(0, sector_buffer, 1, 0) || sector_buffer[0] != MAGIC || sector_buffer[2] != DIRECTORY ||
        sector_buffer[3] != DATA || sector_buffer[4] != ENTRIES || sector_buffer[1] > capacity ||
        sector_buffer[1] <= DATA) {
        return 0; /* not tfs, or a file system larger than the device */
    }
    sectors = sector_buffer[1];
    if (!virtio_transfer(DIRECTORY, directory, 1, 0)) {
        return 0;
    }
    for (uint32_t i = 0; i < ENTRIES; i++) {
        const struct entry *e = &directory[i];
        corrupt[i] = e->name[0] && (e->first < DATA || e->capacity > sectors || e->first > sectors - e->capacity ||
                                    e->size > e->capacity * VIRTIO_SECTOR || e->name[FS_NAME - 1]);
    }
    mounted = 1;
    return 1;
}

uint32_t fs_corrupt(void)
{
    uint32_t n = 0;
    for (uint32_t i = 0; i < ENTRIES; i++) {
        n += corrupt[i];
    }
    return n;
}

int fs_flush(void)
{
    return mounted && virtio_transfer(DIRECTORY, directory, 1, 1);
}

int fs_open(const char *name, int create)
{
    if (!mounted || !name[0]) {
        return -1;
    }
    int free_slot = -1;
    uint32_t next = DATA;
    for (uint32_t i = 0; i < ENTRIES; i++) {
        if (!directory[i].name[0]) {
            if (free_slot < 0) {
                free_slot = (int)i;
            }
            continue;
        }
        if (corrupt[i]) {
            continue; /* its extent cannot be trusted, so new files are not placed after it either */
        }
        if (same(directory[i].name, name)) {
            return (int)i;
        }
        if (directory[i].first + directory[i].capacity > next) {
            next = directory[i].first + directory[i].capacity;
        }
    }
    uint32_t length = 0;
    while (name[length] && length < FS_NAME) {
        length++;
    }
    if (!create || free_slot < 0 || length >= FS_NAME || next + FS_CAPACITY > sectors) {
        return -1;
    }
    struct entry *e = &directory[free_slot];
    for (uint32_t i = 0; i < FS_NAME; i++) {
        e->name[i] = i < length ? name[i] : 0;
    }
    e->first = next;
    e->capacity = FS_CAPACITY;
    e->size = 0;
    return fs_flush() ? free_slot : -1;
}

void fs_truncate(int file)
{
    directory[file].size = 0;
}

/* Issue #35: how many whole sectors from `position` may go straight to `to` in one request
 * rather than through the sector buffer: a sector-aligned position, a word-aligned buffer in RAM
 * below the slots' end (the device may write only RAM), and only sectors both the request and the
 * file fill, so no byte past either is written. */
static uint32_t direct_sectors(const struct entry *e, uint32_t position, const uint8_t *to, uint32_t left)
{
    /* RAM starts at the kernel's base, below the slots, and ends with the last slot. */
    const uint32_t ram = OS_SLOT_BASE - OS_KERNEL_SIZE, ram_end = OS_SLOT_BASE + OS_SLOTS * OS_SLOT_SIZE;
    uint32_t address = (uint32_t)(uintptr_t)to;
    if (position % VIRTIO_SECTOR || address & 3u || address < ram || address >= ram_end) {
        return 0;
    }
    uint32_t in_file = (e->size - position) / VIRTIO_SECTOR, wanted = left / VIRTIO_SECTOR;
    uint32_t whole = in_file < wanted ? in_file : wanted;
    uint32_t room = (ram_end - address) / VIRTIO_SECTOR;
    if (whole > room) {
        return 0; /* the device writes only RAM: leave a run past its end to the buffered path */
    }
    return whole;
}

uint32_t fs_read(int file, uint32_t position, uint8_t *to, uint32_t length)
{
    const struct entry *e = &directory[file];
    uint32_t done = 0;
    while (done < length && position < e->size) {
        uint32_t whole = direct_sectors(e, position, to + done, length - done);
        if (whole) {
            if (!virtio_transfer(e->first + position / VIRTIO_SECTOR, to + done, whole, 0)) {
                return FS_ERROR;
            }
            done += whole * VIRTIO_SECTOR;
            position += whole * VIRTIO_SECTOR;
            continue;
        }
        uint32_t at = position % VIRTIO_SECTOR;
        if (!virtio_transfer(e->first + position / VIRTIO_SECTOR, sector_buffer, 1, 0)) {
            return FS_ERROR;
        }
        const uint8_t *bytes = (const uint8_t *)sector_buffer;
        while (done < length && position < e->size && at < VIRTIO_SECTOR) {
            to[done++] = bytes[at++];
            position++;
        }
    }
    return done;
}

uint32_t fs_write(int file, uint32_t position, const uint8_t *from, uint32_t length)
{
    struct entry *e = &directory[file];
    uint32_t limit = e->capacity * VIRTIO_SECTOR, done = 0;
    while (done < length && position < limit) {
        uint32_t sector = e->first + position / VIRTIO_SECTOR, at = position % VIRTIO_SECTOR;
        if (!virtio_transfer(sector, sector_buffer, 1, 0)) {
            return FS_ERROR; /* as below: a device error is never a short count, which means full */
        }
        uint8_t *bytes = (uint8_t *)sector_buffer;
        uint32_t n = 0;
        while (done + n < length && position + n < limit && at < VIRTIO_SECTOR) {
            bytes[at++] = from[done + n];
            n++;
        }
        if (!virtio_transfer(sector, sector_buffer, 1, 1)) {
            return FS_ERROR; /* the sectors before this one are on the disk and counted in the size */
        }
        done += n; /* counted once the sector is on the disk */
        position += n;
        if (position > e->size) {
            e->size = position;
        }
    }
    return done;
}

uint32_t fs_size(int file)
{
    return directory[file].size;
}

int fs_name(uint32_t index, char *name, uint32_t *size)
{
    for (uint32_t i = 0; mounted && i < ENTRIES; i++) {
        if (in_use(i) && index-- == 0) {
            for (uint32_t k = 0; k < FS_NAME; k++) {
                name[k] = directory[i].name[k];
            }
            *size = directory[i].size;
            return 1;
        }
    }
    return 0;
}
