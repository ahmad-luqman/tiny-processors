# doomgeneric

[doomgeneric](https://github.com/ozkl/doomgeneric) by ozkl, from the repository's `master` at
commit `dcb7a8dbc7a16ce3dda29382ac9aae9d77d21284` (2026-04-12). It is Chocolate Doom's engine with
its platform layer cut down to six hooks. It is Track 3 B3's `doom` program
([record](../../docs/rv32-doom.md)), built on picolibc for the OS with our hooks in
[doom_rv32.c](../../programs/rv32/os/libc/doom_rv32.c).

The files are copied unmodified from the repository's `doomgeneric/` directory:
- the 80 sources its own `Makefile` compiles, less the X11 back end `doomgeneric_xlib.c`, listed in
  `SOURCES` (the Makefile's input);
- every header in that directory;
- the repository's `LICENSE` (GNU GPL version 2) and `README.TXT`.

Left out:
- the other back ends (SDL, Allegro, Win32, emscripten and the rest), with their sound and music
  files;
- the Makefiles and Visual Studio files;
- the screenshots.

The software is under the GNU General Public License, version 2. The `doom` program built from it
is therefore GPL as well; nothing else in the repository links it. `SHA256SUMS.json` holds each
file's SHA-256.

The game data is not here. The shareware `doom1.wad` is fetched by
[tools/rv32_doom.py](../../tools/rv32_doom.py) (`make fetch-rv32-doom-wad`) into the git-ignored
`third_party/doom-wad/`.
