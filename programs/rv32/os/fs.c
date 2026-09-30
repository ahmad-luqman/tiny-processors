/* The kernel's file system; see fs.h and tools/rv32_mkfs.py. Every transfer
 * goes through one sector buffer, so a partial sector is read, changed and
 * written back. */
#include "fs.h"

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

int fs_mount(void)
{
    if (!virtio_transfer(0, sector_buffer, 1, 0) || sector_buffer[0] != MAGIC || sector_buffer[2] != DIRECTORY ||
        sector_buffer[3] != DATA || sector_buffer[4] != ENTRIES) {
        return 0;
    }
    sectors = sector_buffer[1];
    if (!virtio_transfer(DIRECTORY, directory, 1, 0)) {
        return 0;
    }
    for (uint32_t i = 0; i < ENTRIES; i++) { /* a corrupt entry is dropped rather than trusted */
        struct entry *e = &directory[i];
        if (e->name[0] && (e->first < DATA || e->capacity > sectors || e->first > sectors - e->capacity ||
                           e->size > e->capacity * VIRTIO_SECTOR || e->name[FS_NAME - 1])) {
            e->name[0] = 0;
        }
    }
    mounted = 1;
    return 1;
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

uint32_t fs_size(int file)
{
    return directory[file].size;
}

void fs_truncate(int file)
{
    directory[file].size = 0;
}

uint32_t fs_read(int file, uint32_t position, uint8_t *to, uint32_t length)
{
    const struct entry *e = &directory[file];
    uint32_t done = 0;
    while (done < length && position < e->size) {
        uint32_t at = position % VIRTIO_SECTOR;
        if (!virtio_transfer(e->first + position / VIRTIO_SECTOR, sector_buffer, 1, 0)) {
            break;
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
            break;
        }
        uint8_t *bytes = (uint8_t *)sector_buffer;
        while (done < length && position < limit && at < VIRTIO_SECTOR) {
            bytes[at++] = from[done++];
            position++;
        }
        if (!virtio_transfer(sector, sector_buffer, 1, 1)) {
            break;
        }
        if (position > e->size) {
            e->size = position;
        }
    }
    return done;
}

int fs_name(uint32_t index, char *name, uint32_t *size)
{
    for (uint32_t i = 0; mounted && i < ENTRIES; i++) {
        if (directory[i].name[0] && index-- == 0) {
            for (uint32_t k = 0; k < FS_NAME; k++) {
                name[k] = directory[i].name[k];
            }
            *size = directory[i].size;
            return 1;
        }
    }
    return 0;
}
