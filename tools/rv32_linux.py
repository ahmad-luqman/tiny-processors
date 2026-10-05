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
- the files under programs/rv32/linux/ that BUILD_FILES names;
- tools/rv32_dtb.py's LINUX tree.

Buildroot builds reproducibly (BR2_REPRODUCIBLE, a fixed SOURCE_DATE_EPOCH): two builds from empty
build volumes, on the same aarch64 Docker host, gave the same image.

    python3 tools/rv32_linux.py --build    # make rv32-linux-image; does nothing when the pinned image is in place
    python3 tools/rv32_linux.py --check    # exit 1, saying how to build it, unless the pinned image is in place
    python3 tools/rv32_linux.py --clean    # drop this checkout's build volume and stamp (the next build starts over)
"""
from __future__ import annotations

import argparse
import hashlib
import os
import stat
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
STAMP = OUT / "inputs"
DTB = ROOT / "build" / "rv32" / "linux" / "linux.dtb"
BUILDROOT_VERSION = "2025.02.18"
BUILDROOT_SHA256 = "e38ad1df6ea0479fff6419a87a64535d02131674d463be154a763ef725b55321"  # its .sign file's
SOURCE_DATE_EPOCH = "1759276800"  # 2025-10-01 00:00:00 UTC: the date in the kernel's banner
IMAGE_SHA256 = "01dcf663a6794749fd0ddbc9d9ccc0286f28aab555a062b54178a2173f0f483f"
# What the container reads from programs/rv32/linux/ (the session and its transcripts are not inputs).
BUILD_FILES = ("Dockerfile", "build.sh", "buildroot.defconfig", "linux.fragment", "busybox.fragment", "rootfs-overlay")
# Each checkout builds in its own volume, so two worktrees with different inputs do not undo each
# other; the downloads, which Buildroot checks by hash, are shared.
VOLUME = os.environ.get("RV32_LINUX_VOLUME", "tiny-processors-linux-" + hashlib.sha256(str(ROOT).encode()).hexdigest()[:8])
DOWNLOADS = "tiny-processors-linux-dl"


class BuildError(RuntimeError):
    """A build input or step that is not what it must be."""


def shown(path: Path) -> str:
    """A path as messages show it: relative to the checkout when it is inside it."""
    return str(path.relative_to(ROOT)) if path.is_relative_to(ROOT) else str(path)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def build_paths() -> list[Path]:
    """Every file and link BUILD_FILES names, refusing a missing name and hidden files (a .swp or
    .DS_Store in the overlay would end up in the root file system)."""
    paths = []
    for name in BUILD_FILES:
        path = SOURCE / name
        if not path.exists():
            raise BuildError(f"{shown(path)} is missing")
        paths += sorted(path.rglob("*")) if path.is_dir() else [path]
    hidden = [p for p in paths if any(part.startswith(".") for part in p.relative_to(SOURCE).parts)]
    if hidden:
        raise BuildError("hidden files would go into the image: " + ", ".join(shown(p) for p in hidden))
    return paths


def inputs() -> str:
    """One hash over everything the build reads that is not pinned by its own hash: BUILD_FILES
    (contents, modes and link targets, which the overlay copy keeps), Linux's tree, and the source
    pins. The image's own pin is not an input; build() compares the image with it."""
    digest = hashlib.sha256()
    for path in build_paths():
        info = path.lstat()
        if stat.S_ISLNK(info.st_mode):
            body = b"link " + os.readlink(path).encode()
        elif path.is_file():
            body = b"file " + path.read_bytes()
        else:
            body = b"dir"
        digest.update(f"{path.relative_to(SOURCE)}\0{stat.S_IMODE(info.st_mode):o}\0".encode() + body + b"\0")
    digest.update(rv32_dtb.build(rv32_dtb.LINUX))
    digest.update(f"{BUILDROOT_VERSION} {BUILDROOT_SHA256} {SOURCE_DATE_EPOCH}".encode())
    return digest.hexdigest()


def check() -> int:
    if not IMAGE.exists():
        print(f"rv32_linux: {shown(IMAGE)} is missing; run `make rv32-linux-image` "
              "(Docker; 7 minutes, plus the first download)", file=sys.stderr)
        return 1
    got = sha256(IMAGE)
    if got != IMAGE_SHA256:
        print(f"rv32_linux: {shown(IMAGE)} is {got}, not the pinned {IMAGE_SHA256}; "
              "run `make rv32-linux-image`", file=sys.stderr)
        return 1
    print(f"rv32_linux: {shown(IMAGE)} is the pinned image ({IMAGE.stat().st_size} bytes)")
    return 0


def docker(*args: str, quiet: bool = False) -> None:
    """Run docker, turning a missing binary or a failure into a BuildError."""
    try:
        subprocess.run(["docker", *args], check=True, stdout=subprocess.DEVNULL if quiet else None)
    except FileNotFoundError:
        raise BuildError("Docker is needed to build the Linux image, and `docker` is not on the PATH") from None
    except subprocess.CalledProcessError as error:
        raise BuildError(f"`docker {args[0]}` failed (status {error.returncode})") from None


def build() -> int:
    want = inputs()
    if IMAGE.exists() and STAMP.exists() and STAMP.read_text().strip() == want and sha256(IMAGE) == IMAGE_SHA256:
        return check()
    STAMP.unlink(missing_ok=True)  # written again only once the new image is the pinned one
    print(f"rv32_linux: building the Linux image in Docker (volume {VOLUME}; 7 minutes, plus the first download)")
    DTB.parent.mkdir(parents=True, exist_ok=True)
    DTB.write_bytes(rv32_dtb.build(rv32_dtb.LINUX))
    OUT.mkdir(parents=True, exist_ok=True)
    tag = f"tiny-processors-linux:{want[:12]}"
    docker("build", "-q", "-t", tag, str(SOURCE), quiet=True)
    env = {"BR_VERSION": BUILDROOT_VERSION, "BR_SHA256": BUILDROOT_SHA256, "SOURCE_DATE_EPOCH": SOURCE_DATE_EPOCH,
           "INPUTS": want, "BR2_DL_DIR": "/dl"}
    docker("run", "--rm", "-v", f"{VOLUME}:/build", "-v", f"{DOWNLOADS}:/dl", "-v", f"{SOURCE}:/src:ro",
           "-v", f"{DTB.parent}:/dtb:ro", *(f"--env={key}={value}" for key, value in env.items()), tag, "bash", "/src/build.sh")
    # The results are copied out as the host's user, who owns third_party/linux/ (the build runs as
    # the image's uid 1000, which on a Linux host may not be allowed to write there).
    docker("run", "--rm", "--user", f"{os.getuid()}:{os.getgid()}", "-v", f"{VOLUME}:/build:ro", "-v", f"{OUT}:/out",
           tag, "sh", "-c", "cp /build/results/* /out/")
    status = check()
    if status == 0:
        STAMP.write_text(want + "\n")
    else:
        print("rv32_linux: the build finished but its image is not the pinned one; the build is not "
              "reproducible on this host, or the pin is stale", file=sys.stderr)
    return status


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    action = parser.add_mutually_exclusive_group(required=True)
    action.add_argument("--build", action="store_true", help="build the image unless the pinned one is in place")
    action.add_argument("--check", action="store_true", help="fail unless the pinned image is in place")
    action.add_argument("--clean", action="store_true", help="remove this checkout's build volume and the stamp")
    args = parser.parse_args(argv)
    try:
        if args.clean:
            STAMP.unlink(missing_ok=True)
            docker("volume", "rm", "-f", VOLUME, quiet=True)
            return 0
        return build() if args.build else check()
    except BuildError as error:
        print(f"rv32_linux: {error}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
