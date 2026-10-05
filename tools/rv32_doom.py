#!/usr/bin/env python3
"""Fetch Doom's shareware WAD (issue #35, docs/rv32-doom.md).

The shareware `doom1.wad` v1.9 is freely distributable unmodified but is not free software, so it
is never committed: `--fetch` downloads Debian's `doom-wad-shareware` package (a stable URL in the
archive's pool), checks the package's SHA-256, takes `doom1.wad` out of it (an `ar` archive whose
`data.tar.xz` holds the file) and checks the WAD's SHA-256 too, which is the Doom Wiki's v1.9
(4,196,020 bytes, MD5 f0cefca49926d00903cf57551d901abe). The file lands in the git-ignored
third_party/doom-wad/, beside the package's copyright file, which carries id Software's licence and
John Carmack's statement that the shareware WAD is freely distributable.

    python3 tools/rv32_doom.py --fetch       # make fetch-rv32-doom-wad
    python3 tools/rv32_doom.py --check       # exit 1, saying how to fetch it, unless it is there
"""
from __future__ import annotations

import argparse
import hashlib
import io
import lzma
import sys
import tarfile
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WAD_DIR = ROOT / "third_party" / "doom-wad"
WAD = WAD_DIR / "doom1.wad"
PACKAGE_URL = "https://deb.debian.org/debian/pool/non-free/d/doom-wad-shareware/doom-wad-shareware_1.9.fixed-5_all.deb"
PACKAGE_SHA256 = "5802f176c0303e228095b5312def53de602781cf4c53e79842257484a0d9e938"
WAD_SHA256 = "1d7d43be501e67d927e415e0b8f3e29c3bf33075e859721816f652a526cac771"
WAD_SIZE = 4196020
MEMBERS = {"./usr/share/games/doom/doom1.wad": "doom1.wad",
           "./usr/share/doc/doom-wad-shareware/copyright": "copyright"}


class FetchError(RuntimeError):
    """A download or a file that is not the one pinned."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ar_members(blob: bytes) -> dict[str, bytes]:
    """The members of a Unix `ar` archive (a .deb is one)."""
    if not blob.startswith(b"!<arch>\n"):
        raise FetchError("the package is not an ar archive")
    members, at = {}, 8
    while at + 60 <= len(blob):
        header = blob[at:at + 60]
        name = header[:16].decode().strip().rstrip("/")
        size = int(header[48:58].decode().strip())
        members[name] = blob[at + 60:at + 60 + size]
        at += 60 + size + (size & 1)
    return members


def extract(package: bytes) -> dict[str, bytes]:
    """The WAD and the copyright file out of the package's data.tar.xz."""
    data = ar_members(package).get("data.tar.xz")
    if data is None:
        raise FetchError("the package has no data.tar.xz")
    found = {}
    with tarfile.open(fileobj=io.BytesIO(lzma.decompress(data))) as tar:
        for member in tar.getmembers():
            if member.name in MEMBERS and member.isfile():
                found[MEMBERS[member.name]] = tar.extractfile(member).read()
    missing = set(MEMBERS.values()) - set(found)
    if missing:
        raise FetchError(f"the package lacks {', '.join(sorted(missing))}")
    return found


def fetch(url: str = PACKAGE_URL) -> None:
    with urllib.request.urlopen(url, timeout=120) as response:
        package = response.read()
    if sha256(package) != PACKAGE_SHA256:
        raise FetchError(f"{url}: SHA-256 {sha256(package)}, expected {PACKAGE_SHA256}")
    files = extract(package)
    wad = files["doom1.wad"]
    if len(wad) != WAD_SIZE or sha256(wad) != WAD_SHA256:
        raise FetchError(f"doom1.wad: {len(wad)} bytes, SHA-256 {sha256(wad)}; expected {WAD_SIZE}, {WAD_SHA256}")
    WAD_DIR.mkdir(parents=True, exist_ok=True)
    for name, data in files.items():
        (WAD_DIR / name).write_bytes(data)


def check() -> None:
    if not WAD.is_file():
        raise FetchError(f"{WAD.relative_to(ROOT)} is missing: run `make fetch-rv32-doom-wad`")
    if sha256(WAD.read_bytes()) != WAD_SHA256:
        raise FetchError(f"{WAD.relative_to(ROOT)} is not the pinned shareware v1.9: run `make fetch-rv32-doom-wad`")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fetch", action="store_true", help="download, check and unpack the WAD")
    group.add_argument("--check", action="store_true", help="fail unless the pinned WAD is in place")
    args = parser.parse_args(argv)
    try:
        if args.fetch:
            try:
                check()
                print(f"rv32_doom: {WAD.relative_to(ROOT)} is already the pinned WAD")
                return 0
            except FetchError:
                fetch()
            print(f"rv32_doom: {WAD.relative_to(ROOT)}, {WAD_SIZE} bytes, SHA-256 {WAD_SHA256}")
        else:
            check()
    except (FetchError, OSError, lzma.LZMAError, tarfile.TarError, ValueError) as error:
        print(f"rv32_doom: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
