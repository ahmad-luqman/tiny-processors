/* The kernel's file system (Track 2, O3): "tfs" on the virtio-blk disk, as
 * tools/rv32_mkfs.py writes it. A superblock, one directory sector of 16
 * entries, and one contiguous extent per file, allocated at creation with a
 * fixed capacity. Files are found by name; an open file is its directory
 * index. */
#ifndef RV32_OS_FS_H
#define RV32_OS_FS_H

#include <stdint.h>

#define FS_NAME 20u
#define FS_CAPACITY 8u /* sectors a new file gets: 4 KiB */
#define FS_ERROR 0xffffffffu /* a transfer failed: fs_read and fs_write return this, never a partial count */

/* 1 when the disk, `capacity` sectors, holds tfs; a corrupt directory entry is reported and kept
 * out of use (fs_open, fs_name skip it) but written back unchanged. */
int fs_mount(uint32_t capacity);
uint32_t fs_corrupt(void);                                  /* how many entries fs_mount set aside */
int fs_open(const char *name, int create);                   /* the file's index, or -1 */
void fs_truncate(int file);                                  /* size 0, in memory until fs_flush */
uint32_t fs_read(int file, uint32_t position, uint8_t *to, uint32_t length);
/* Bytes written, fewer than `length` when the file's capacity ends first; grows the size. */
uint32_t fs_write(int file, uint32_t position, const uint8_t *from, uint32_t length);
uint32_t fs_size(int file);                                  /* the file's size in bytes (Track 3) */
int fs_flush(void);                                          /* write the directory back; 1 on success */
int fs_name(uint32_t index, char *name, uint32_t *size);     /* the index-th file; 0 past the last */

#endif
