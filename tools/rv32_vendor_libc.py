#!/usr/bin/env python3
"""Vendor the C library and compiler runtime for the OS's programs (Track 3, L1).

The programs link picolibc (configured as third_party/picolibc/picolibc.h records) and the
compiler-rt builtins clang calls for soft floating point and 64-bit arithmetic on RV32I. Only the
sources the programs actually link are kept, with every header they include, so the Makefile
compiles a few hundred files rather than picolibc's thousand. This script picks them:

  1. Build picolibc once with meson for RV32I (the command is in third_party/picolibc/README.md)
     and link the programs against that build with -Map (the Makefile's link line, with the meson
     libc.a and the builtins objects in place of ours).
  2. Run this script with the map files: every archive member a map names becomes its source file
     (found through meson's compile_commands.json), and every builtins object its .c or .S.
  3. The headers come from preprocessing each kept source, and the programs, with the include
     directories the Makefile uses.

Run it again when a program starts to need a function that is not vendored: the link names it.
Existing files are replaced, nothing else is removed (delete the directories first to prune).
"""
import argparse
import hashlib
import json
import os
import re
import shutil
import subprocess
import sys

# The include directories the Makefile gives picolibc's sources, relative to the picolibc tree
# (the generated picolibc.h sits at its top), and the ones programs get: the public headers.
PICOLIBC_INCLUDES = [
    ".",
    "newlib/libc/stdlib",
    "newlib/libm/common",
    "newlib/libc/tinystdio",
    "newlib/libc/locale",
    "newlib/libc/machine/riscv",
    "newlib/libc/include",
]
PROGRAM_INCLUDES = [".", "newlib/libc/tinystdio", "newlib/libc/machine/riscv", "newlib/libc/include"]
PICOLIBC_LICENSES = ["COPYING.picolibc", "COPYING.NEWLIB"]
COMPILER_RT_LICENSES = ["LICENSE.TXT", "CODE_OWNERS.TXT", "CREDITS.TXT"]

MEMBER = re.compile(r"\blibc\.a\(([^)]+\.o)\)")
BUILTIN = re.compile(r"\bbuiltins\.a\(([^)]+)\.o\)")


def run(command):
    return subprocess.run(command, check=True, capture_output=True, text=True).stdout


def dependencies(cc, flags, source):
    """Every file the preprocessor reads for `source`."""
    text = run([cc] + flags + ["-M", "-MT", "x", source]).replace("\\\n", " ")
    return text.split(":", 1)[1].split()


def copy(source_root, relative, destination_root, copied):
    target = os.path.join(destination_root, relative)
    os.makedirs(os.path.dirname(target), exist_ok=True)
    shutil.copyfile(os.path.join(source_root, relative), target)
    copied.add(relative)


def within(path, root):
    path, root = os.path.realpath(path), os.path.realpath(root)
    return os.path.commonpath([path, root]) == root and os.path.relpath(path, root)


def write_sums(root, files):
    sums = {}
    for relative in sorted(files):
        with open(os.path.join(root, relative), "rb") as f:
            sums[relative] = hashlib.sha256(f.read()).hexdigest()
    with open(os.path.join(root, "SHA256SUMS.json"), "w") as f:
        json.dump(sums, f, indent=2)
        f.write("\n")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--picolibc", required=True, help="the picolibc checkout")
    parser.add_argument("--picolibc-build", required=True, help="its meson build directory")
    parser.add_argument("--compiler-rt", required=True, help="compiler-rt/lib/builtins of an llvm-project checkout")
    parser.add_argument("--map", action="append", required=True, help="a program's link map (repeatable)")
    parser.add_argument("--program-source", action="append", default=[], help="a program source, for its headers")
    parser.add_argument("--program-include", action="append", default=[], help="an include directory programs use")
    parser.add_argument("--out", default="third_party", help="where picolibc/ and compiler-rt/ go")
    parser.add_argument("--cc", default="clang")
    args = parser.parse_args()

    target = ["--target=riscv32-unknown-elf", "-march=rv32i", "-mabi=ilp32", "-D_LIBC", "-D_FILE_OFFSET_BITS=64"]
    build = os.path.abspath(args.picolibc_build)
    commands = json.load(open(os.path.join(build, "compile_commands.json")))
    by_object = {}
    for entry in commands:
        obj = os.path.basename(entry["output"]) if "output" in entry else entry["command"].split(" -o ")[1].split()[0]
        source = os.path.normpath(os.path.join(entry["directory"], entry["file"]))
        by_object.setdefault(os.path.basename(obj), set()).add(source)

    members, builtins = set(), set()
    for path in args.map:
        text = open(path).read()
        members |= set(MEMBER.findall(text))
        builtins |= set(BUILTIN.findall(text))

    picolibc_out = os.path.join(args.out, "picolibc")
    rt_out = os.path.join(args.out, "compiler-rt")
    picolibc_files, rt_files = set(), set()

    sources = []
    for member in sorted(members):
        found = by_object.get(member, set())
        if len(found) != 1:
            sys.exit(f"{member}: {len(found)} sources in compile_commands.json")
        relative = within(found.pop(), args.picolibc)
        if not relative:
            sys.exit(f"{member}: its source is outside the picolibc tree")
        sources.append(relative)
        copy(args.picolibc, relative, picolibc_out, picolibc_files)
    shutil.copyfile(os.path.join(build, "picolibc.h"), os.path.join(picolibc_out, "picolibc.h"))
    picolibc_files.add("picolibc.h")

    flags = target + ["-nostdlibinc", "-I" + build] + ["-I" + os.path.join(args.picolibc, d) for d in PICOLIBC_INCLUDES]
    for relative in sources:
        for path in dependencies(args.cc, flags, os.path.join(args.picolibc, relative)):
            header = within(path, args.picolibc)
            if header:
                copy(args.picolibc, header, picolibc_out, picolibc_files)
    program_flags = target[:3] + ["-DLIBC_PROGRAM=\"x\"", "-nostdlibinc", "-I" + build] + \
        ["-I" + os.path.join(args.picolibc, d) for d in PROGRAM_INCLUDES[1:]] + ["-I" + d for d in args.program_include]
    for source in args.program_source:
        for path in dependencies(args.cc, program_flags, source):
            header = within(path, args.picolibc)
            if header:
                copy(args.picolibc, header, picolibc_out, picolibc_files)
    for name in PICOLIBC_LICENSES:
        copy(args.picolibc, name, picolibc_out, picolibc_files)
    with open(os.path.join(picolibc_out, "SOURCES"), "w") as f:
        f.write("".join(s + "\n" for s in sources))
    picolibc_files.add("SOURCES")

    rt_sources = []
    rt_flags = target[:3] + ["-fforce-enable-int128"]
    for name in sorted(builtins):
        # The generic C first: riscv/muldi3.S, say, is for RV64 only and assembles to nothing here.
        for candidate in (name + ".c", "riscv/" + name + ".S", "riscv/" + name + ".c"):
            if os.path.exists(os.path.join(args.compiler_rt, candidate)):
                rt_sources.append(candidate)
                break
        else:
            sys.exit(f"builtins/{name}: no source")
    for relative in rt_sources:
        copy(args.compiler_rt, relative, rt_out, rt_files)
        for path in dependencies(args.cc, rt_flags, os.path.join(args.compiler_rt, relative)):
            header = within(path, args.compiler_rt)
            if header:
                copy(args.compiler_rt, header, rt_out, rt_files)
    licenses = os.path.join(args.compiler_rt, "..", "..")
    for name in COMPILER_RT_LICENSES:
        if os.path.exists(os.path.join(licenses, name)):
            copy(licenses, name, rt_out, rt_files)
    with open(os.path.join(rt_out, "SOURCES"), "w") as f:
        f.write("".join(s + "\n" for s in rt_sources))
    rt_files.add("SOURCES")

    write_sums(picolibc_out, picolibc_files)
    write_sums(rt_out, rt_files)
    print(f"picolibc: {len(sources)} sources, {len(picolibc_files)} files; compiler-rt: {len(rt_sources)} sources, {len(rt_files)} files")


if __name__ == "__main__":
    main()
