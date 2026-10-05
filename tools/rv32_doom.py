#!/usr/bin/env python3
"""Fetch Doom's shareware WAD (issue #35, docs/rv32-doom.md).

The shareware `doom1.wad` v1.9 is freely distributable unmodified but is not free software, so it
is never committed. `--fetch` downloads Debian's `doom-wad-shareware` package and checks its
SHA-256. It tries the archive's pool first and then snapshot.debian.org, which keeps the file by its
SHA-1 after the pool drops a superseded version. It takes `doom1.wad` out of the package (an `ar`
archive whose `data.tar.xz` holds the file) and checks the WAD's SHA-256 too, which is the Doom
Wiki's v1.9 (4,196,020 bytes, MD5 f0cefca49926d00903cf57551d901abe). The WAD lands in the
git-ignored third_party/doom-wad/, beside the package's copyright file, which carries id Software's
licence and John Carmack's statement that the shareware WAD is freely distributable.

    python3 tools/rv32_doom.py --fetch       # make fetch-rv32-doom-wad; does nothing when both are in place
    python3 tools/rv32_doom.py --check       # exit 1, saying how to fetch them, unless both are in place
"""
from __future__ import annotations

import argparse
import hashlib
import io
import lzma
import os
import sys
import tarfile
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WAD_DIR = ROOT / "third_party" / "doom-wad"
WAD = WAD_DIR / "doom1.wad"
COPYRIGHT = WAD_DIR / "copyright"
PACKAGE_URLS = (
    "https://deb.debian.org/debian/pool/non-free/d/doom-wad-shareware/doom-wad-shareware_1.9.fixed-5_all.deb",
    "https://snapshot.debian.org/file/732562ea15f0a6623611bdb739a03af72d8dbaf1",  # the same file, by its SHA-1
)
PACKAGE_SHA256 = "5802f176c0303e228095b5312def53de602781cf4c53e79842257484a0d9e938"
WAD_SHA256 = "1d7d43be501e67d927e415e0b8f3e29c3bf33075e859721816f652a526cac771"
WAD_SIZE = 4196020
COPYRIGHT_SHA256 = "1482321ff0640a41f039d771f7a2abe62b32ff76f6fd458c291cb981df94d083"  # 8,656 bytes, the package's
MEMBERS = {"./usr/share/games/doom/doom1.wad": "doom1.wad",
           "./usr/share/doc/doom-wad-shareware/copyright": "copyright"}


class FetchError(RuntimeError):
    """A download, a package or a file in place that is not the one pinned."""


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def ar_members(blob: bytes) -> dict[str, bytes]:
    """The members of a Unix `ar` archive (a .deb is one); a member cut short is refused."""
    if not blob.startswith(b"!<arch>\n"):
        raise FetchError("the package is not an ar archive")
    members, at = {}, 8
    while at + 60 <= len(blob):
        header = blob[at:at + 60]
        name = header[:16].decode("ascii", "replace").strip().rstrip("/")
        try:
            size = int(header[48:58].decode("ascii").strip())
        except (UnicodeDecodeError, ValueError):
            raise FetchError(f"the package's member at {at} has no size") from None
        if at + 60 + size > len(blob):
            raise FetchError(f"the package's member {name} is cut short")
        members[name] = blob[at + 60:at + 60 + size]
        at += 60 + size + (size & 1)
    return members


def extract(package: bytes) -> dict[str, bytes]:
    """The WAD and the copyright file out of the package's data.tar.xz, read into memory by exact
    name, so no path in the archive is ever written."""
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


def download() -> bytes:
    """The pinned package, from the first URL that serves it."""
    problems = []
    for url in PACKAGE_URLS:
        try:
            with urllib.request.urlopen(url, timeout=120) as response:
                package = response.read()
        except (urllib.error.URLError, OSError) as error:
            problems.append(f"{url}: {error}")
            continue
        digest = sha256(package)
        if digest == PACKAGE_SHA256:
            return package
        problems.append(f"{url}: SHA-256 {digest}, expected {PACKAGE_SHA256}")
    raise FetchError("no URL served the pinned package: " + "; ".join(problems))


def write(path: Path, data: bytes) -> None:
    """Write `path` whole or not at all: a fetch cut short leaves no truncated file."""
    partial = path.with_name(path.name + ".partial")
    partial.write_bytes(data)
    os.replace(partial, path)


def fetch() -> None:
    files = extract(download())
    wad = files["doom1.wad"]
    digest = sha256(wad)
    if len(wad) != WAD_SIZE or digest != WAD_SHA256:
        raise FetchError(f"doom1.wad: {len(wad)} bytes, SHA-256 {digest}; expected {WAD_SIZE}, {WAD_SHA256}")
    licence = sha256(files["copyright"])
    if licence != COPYRIGHT_SHA256:
        raise FetchError(f"the copyright file's SHA-256 is {licence}, expected {COPYRIGHT_SHA256}")
    WAD_DIR.mkdir(parents=True, exist_ok=True)
    write(COPYRIGHT, files["copyright"])  # the licence first: a WAD never sits without it
    write(WAD, wad)


def check() -> None:
    """Raise unless the pinned WAD and its pinned licence are in place, naming the target that fetches them."""
    if not WAD.is_file():
        raise FetchError(f"{WAD.relative_to(ROOT)} is missing: run `make fetch-rv32-doom-wad`")
    if sha256(WAD.read_bytes()) != WAD_SHA256:
        raise FetchError(f"{WAD.relative_to(ROOT)} is not the pinned shareware v1.9: run `make fetch-rv32-doom-wad`")
    if not COPYRIGHT.is_file() or sha256(COPYRIGHT.read_bytes()) != COPYRIGHT_SHA256:
        raise FetchError(f"{COPYRIGHT.relative_to(ROOT)}, the WAD's licence, is missing or not the pinned file: "
                         "run `make fetch-rv32-doom-wad`")


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    group = parser.add_mutually_exclusive_group(required=True)
    group.add_argument("--fetch", action="store_true", help="download, check and unpack the WAD unless it is in place")
    group.add_argument("--check", action="store_true", help="fail unless the pinned WAD and its licence are in place")
    args = parser.parse_args(argv)
    try:
        if args.check:
            check()
            return 0
        try:
            check()
            print(f"rv32_doom: {WAD.relative_to(ROOT)} is already the pinned WAD")
            return 0
        except FetchError as reason:
            if WAD.is_file():
                print(f"rv32_doom: fetching again: {reason}")
        fetch()
        print(f"rv32_doom: {WAD.relative_to(ROOT)}, {WAD_SIZE} bytes, SHA-256 {WAD_SHA256}")
    except (FetchError, OSError, lzma.LZMAError, tarfile.TarError) as error:
        print(f"rv32_doom: {error}", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
