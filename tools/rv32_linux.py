#!/usr/bin/env python3
"""Build the Linux image (issue #36, docs/rv32-linux.md).

A nommu, machine-mode Linux 6.12.27 for RV32IMA, with busybox in a built-in initramfs and our
device tree compiled in, built by upstream Buildroot in a Docker container. Nothing it produces is
committed: `--build` writes the image to the git-ignored third_party/linux/, and `--check` holds it
to the SHA-256 pinned here, so every backend boots the same bytes.

The inputs are pinned:
- the container (Debian by digest, its packages from snapshot.debian.org at a fixed date);
- Buildroot's release tarball by SHA-256;
- Linux, busybox, uClibc-ng, gcc and binutils by the hashes Buildroot checks;
- the configurations under programs/rv32/linux/;
- tools/rv32_dtb.py's LINUX tree.

Buildroot builds reproducibly (BR2_REPRODUCIBLE, a fixed SOURCE_DATE_EPOCH), so a second build
from a clean volume gives the same image.

    python3 tools/rv32_linux.py --build    # make rv32-linux-image; does nothing when the image is current
    python3 tools/rv32_linux.py --check    # exit 1, saying how to build it, unless the pinned image is in place
    python3 tools/rv32_linux.py --clean    # drop the build volume (the next build starts from nothing)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from tools import rv32_dtb  # noqa: E402

SOURCE = ROOT / "programs" / "rv32" / "linux"
OUT = ROOT / "third_party" / "linux"
IMAGE = OUT / "Image"
DTB = ROOT / "build" / "rv32" / "linux" / "linux.dtb"
BUILDROOT_VERSION = "2025.02.18"
BUILDROOT_SHA256 = "e38ad1df6ea0479fff6419a87a64535d02131674d463be154a763ef725b55321"  # its .sign file's
SOURCE_DATE_EPOCH = "1759276800"  # 2025-10-01 00:00:00 UTC: the date in the kernel's and busybox's banners
IMAGE_SHA256 = "10631095cdc025c194e413e1291239d3223e08a078a93025c2af0b38ad92ed9c"  # 2,650,076 bytes; two clean builds agree
VOLUME = os.environ.get("RV32_LINUX_VOLUME", "tiny-processors-linux")  # another name builds from nothing beside it
# What the container reads from programs/rv32/linux/ (the session and its transcripts are not inputs).
BUILD_FILES = ("Dockerfile", "build.sh", "buildroot.defconfig", "linux.fragment", "busybox.fragment", "rootfs-overlay")


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def inputs() -> str:
    """One hash over everything the build reads that is not pinned by its own hash: BUILD_FILES,
    Linux's tree, and the pins above."""
    digest = hashlib.sha256()
    files = [p for name in BUILD_FILES for p in ([SOURCE / name] if (SOURCE / name).is_file() else (SOURCE / name).rglob("*"))]
    for path in sorted(p for p in files if p.is_file()):
        digest.update(str(path.relative_to(SOURCE)).encode() + b"\0" + path.read_bytes() + b"\0")
    digest.update(rv32_dtb.build(rv32_dtb.LINUX))
    digest.update(f"{BUILDROOT_VERSION} {BUILDROOT_SHA256} {SOURCE_DATE_EPOCH}".encode())
    return digest.hexdigest()


def check() -> int:
    if not IMAGE.exists():
        print(f"rv32_linux: {IMAGE.relative_to(ROOT)} is missing; run `make rv32-linux-image` (Docker; minutes, plus the first download)",
              file=sys.stderr)
        return 1
    got = sha256(IMAGE)
    if IMAGE_SHA256 is None:
        print(f"rv32_linux: no image is pinned yet; {IMAGE.relative_to(ROOT)} is {got}", file=sys.stderr)
        return 1
    if got != IMAGE_SHA256:
        print(f"rv32_linux: {IMAGE.relative_to(ROOT)} is {got}, not the pinned {IMAGE_SHA256}; "
              "run `make rv32-linux-image`", file=sys.stderr)
        return 1
    print(f"rv32_linux: {IMAGE.relative_to(ROOT)} is the pinned image ({IMAGE.stat().st_size} bytes)")
    return 0


def build() -> int:
    stamp = OUT / "inputs"
    want = inputs()
    if IMAGE.exists() and stamp.exists() and stamp.read_text().strip() == want:
        print(f"rv32_linux: {IMAGE.relative_to(ROOT)} is current")
        return check()
    rv32_dtb.main(["--linux-dtb", str(DTB)])
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"tiny-processors-linux:{want[:12]}"
    subprocess.run(["docker", "build", "-q", "-t", tag, str(SOURCE)], check=True, stdout=subprocess.DEVNULL)
    env = {"BR_VERSION": BUILDROOT_VERSION, "BR_SHA256": BUILDROOT_SHA256,
           "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH, "INPUTS": want}
    command = ["docker", "run", "--rm", "-v", f"{VOLUME}:/build", "-v", f"{SOURCE}:/src:ro",
               "-v", f"{DTB.parent}:/dtb:ro", "-v", f"{OUT}:/out"]
    for key, value in env.items():
        command += ["-e", f"{key}={value}"]
    result = subprocess.run(command + [tag, "bash", "/src/build.sh"])
    if result.returncode != 0:
        print(f"rv32_linux: the build failed (status {result.returncode})", file=sys.stderr)
        return 1
    stamp.write_text(want + "\n")
    return check()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--build", action="store_true", help="build the image unless it is current")
    action.add_argument("--check", action="store_true", help="fail unless the pinned image is in place")
    action.add_argument("--clean", action="store_true", help="remove the Docker build volume")
    args = parser.parse_args(argv)
    if args.clean:
        return subprocess.run(["docker", "volume", "rm", "-f", VOLUME]).returncode
    return build() if args.build else check()


if __name__ == "__main__":
    sys.exit(main())
