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
PROGRAM_INCLUDES = ["newlib/libc/tinystdio", "newlib/libc/machine/riscv", "newlib/libc/include"]  # and picolibc.h's
PICOLIBC_LICENSES = ["COPYING.picolibc", "COPYING.NEWLIB"]
COMPILER_RT_LICENSES = ["LICENSE.TXT", "CODE_OWNERS.TXT", "CREDITS.TXT"]

# The libcalls clang emits for RV32I's missing multiply and divide and for 64-bit integers and
# single and double floats (overflow-checked multiplies included), vendored whether or not today's
# programs reach them: which of them a program calls depends on the compiler's version (clang 20
# calls __floatundidf for Lua's math.random, clang 18 does not), not only on the program. Not
# here: long double (binary128, `*tf*`), which needs __int128, complex division, and -ftrapv's
# checks. The link maps add anything else, and the preprocessor the files these include (fp_mode.c
# comes with the float functions, which ask it for the rounding mode).
RV32I_BUILTINS = [
    "mulsi3", "divsi3", "modsi3", "udivsi3", "umodsi3", "udivmodsi4", "divmodsi4",
    "muldi3", "divdi3", "moddi3", "udivdi3", "umoddi3", "udivmoddi4", "divmoddi4",
    "ashldi3", "ashrdi3", "lshrdi3", "negdi2", "cmpdi2", "ucmpdi2",
    "clzsi2", "clzdi2", "ctzsi2", "ctzdi2", "popcountsi2", "popcountdi2", "paritysi2", "paritydi2",
    "bswapsi2", "bswapdi2", "mulosi4", "mulodi4",
    "adddf3", "subdf3", "muldf3", "divdf3", "negdf2", "comparedf2", "powidf2",
    "addsf3", "subsf3", "mulsf3", "divsf3", "negsf2", "comparesf2", "powisf2",
    "extendsfdf2", "truncdfsf2",
    "fixdfsi", "fixdfdi", "fixunsdfsi", "fixunsdfdi", "floatsidf", "floatdidf", "floatunsidf", "floatundidf",
    "fixsfsi", "fixsfdi", "fixunssfsi", "fixunssfdi", "floatsisf", "floatdisf", "floatunsisf", "floatundisf",
]

MEMBER = re.compile(r"\blibc\.a\(([^)]+\.o)\)")
BUILTIN = re.compile(r"\bbuiltins\.a\(([^)]+)\.o\)")


def run(command):
    result = subprocess.run(command, capture_output=True, text=True)
    if result.returncode:
        sys.exit(f"{' '.join(command)}\n{result.stderr}")
    return result.stdout


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


def copy_headers(cc, flags, source, source_root, destination_root, copied):
    """Copy every file under `source_root` the preprocessor reads for `source`."""
    for path in dependencies(cc, flags, source):
        relative = within(path, source_root)
        if relative:
            copy(source_root, relative, destination_root, copied)


def write_sums(root, files):
    sums = {}
    for relative in sorted(files):
        with open(os.path.join(root, relative), "rb") as f:
            sums[relative] = hashlib.sha256(f.read()).hexdigest()
    with open(os.path.join(root, "SHA256SUMS.json"), "w") as f:
        json.dump(sums, f, indent=2)
        f.write("\n")


def verify(root):
    """Problems with a vendored directory: a file whose SHA-256 is not its manifest's, a file the
    manifest lists that is missing, a source SOURCES lists that the manifest does not, or a file
    that is in neither the manifest nor the hand-written README.md. Empty when all is well."""
    with open(os.path.join(root, "SHA256SUMS.json")) as f:
        sums = json.load(f)
    problems = []
    for relative, expected in sorted(sums.items()):
        path = os.path.join(root, relative)
        if not os.path.isfile(path):
            problems.append(f"{relative}: missing")
            continue
        with open(path, "rb") as f:
            if hashlib.sha256(f.read()).hexdigest() != expected:
                problems.append(f"{relative}: SHA-256 differs from SHA256SUMS.json")
    sources = os.path.join(root, "SOURCES")
    if os.path.exists(sources):
        with open(sources) as f:
            problems += [f"{s}: in SOURCES, not in SHA256SUMS.json" for s in f.read().split() if s not in sums]
    for directory, _, names in os.walk(root):
        for name in names:
            relative = os.path.relpath(os.path.join(directory, name), root)
            if relative not in sums and relative not in ("SHA256SUMS.json", "README.md"):
                problems.append(f"{relative}: not in SHA256SUMS.json")
    return problems


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--picolibc", help="the picolibc checkout")
    parser.add_argument("--picolibc-build", help="its meson build directory")
    parser.add_argument("--compiler-rt", help="compiler-rt/lib/builtins of an llvm-project checkout")
    parser.add_argument("--map", action="append", default=[], help="a program's link map (repeatable)")
    parser.add_argument("--program-source", action="append", default=[], help="a program source, for its headers")
    parser.add_argument("--program-include", action="append", default=[], help="an include directory programs use")
    parser.add_argument("--out", default="third_party", help="where picolibc/ and compiler-rt/ go")
    parser.add_argument("--cc", default="clang")
    parser.add_argument("--verify", action="append", default=[], metavar="DIR",
                        help="only check a vendored directory against its SHA256SUMS.json (repeatable)")
    args = parser.parse_args()
    if args.verify:
        problems = [f"{d}/{p}" for d in args.verify for p in verify(d)]
        print("\n".join(problems) or f"{len(args.verify)} vendored directories match their manifests")
        return 1 if problems else 0
    if not (args.picolibc and args.picolibc_build and args.compiler_rt and args.map):
        parser.error("--picolibc, --picolibc-build, --compiler-rt and --map are required to vendor")

    target = ["--target=riscv32-unknown-elf", "-march=rv32i", "-mabi=ilp32"]
    library_defines = ["-D_LIBC", "-D_FILE_OFFSET_BITS=64"]
    build = os.path.abspath(args.picolibc_build)
    with open(os.path.join(build, "compile_commands.json")) as f:
        commands = json.load(f)
    by_object = {}
    for entry in commands:
        obj = os.path.basename(entry["output"]) if "output" in entry else entry["command"].split(" -o ")[1].split()[0]
        source = os.path.normpath(os.path.join(entry["directory"], entry["file"]))
        by_object.setdefault(os.path.basename(obj), set()).add(source)

    members, builtins = set(), set(RV32I_BUILTINS)
    for path in args.map:
        with open(path) as f:
            text = f.read()
        members |= set(MEMBER.findall(text))
        builtins |= set(BUILTIN.findall(text))
    if not members:
        sys.exit("the maps name no libc.a member: are they the links against the meson build's libc.a?")

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

    def includes(directories):
        return ["-nostdlibinc", "-I" + build] + ["-I" + os.path.join(args.picolibc, d) for d in directories]

    library_flags = target + library_defines + includes(PICOLIBC_INCLUDES)
    program_flags = target + ['-DLIBC_PROGRAM="x"'] + includes(PROGRAM_INCLUDES) + ["-I" + d for d in args.program_include]
    for relative in sources:
        copy_headers(args.cc, library_flags, os.path.join(args.picolibc, relative), args.picolibc, picolibc_out, picolibc_files)
    for source in args.program_source:
        copy_headers(args.cc, program_flags, source, args.picolibc, picolibc_out, picolibc_files)
    for name in PICOLIBC_LICENSES:
        copy(args.picolibc, name, picolibc_out, picolibc_files)
    with open(os.path.join(picolibc_out, "SOURCES"), "w") as f:
        f.write("".join(s + "\n" for s in sources))
    picolibc_files.add("SOURCES")

    rt_sources = []
    rt_flags = target + ["-fforce-enable-int128"]
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
        copy_headers(args.cc, rt_flags, os.path.join(args.compiler_rt, relative), args.compiler_rt, rt_out, rt_files)
    licenses = os.path.join(args.compiler_rt, "..", "..")
    for name in COMPILER_RT_LICENSES:
        if not os.path.exists(os.path.join(licenses, name)):
            sys.exit(f"compiler-rt/{name}: missing; is --compiler-rt compiler-rt/lib/builtins?")
        copy(licenses, name, rt_out, rt_files)
    with open(os.path.join(rt_out, "SOURCES"), "w") as f:
        f.write("".join(s + "\n" for s in rt_sources))
    rt_files.add("SOURCES")

    write_sums(picolibc_out, picolibc_files)
    write_sums(rt_out, rt_files)
    print(f"picolibc: {len(sources)} sources, {len(picolibc_files)} files; compiler-rt: {len(rt_sources)} sources, {len(rt_files)} files")
    return 0


if __name__ == "__main__":
    sys.exit(main())
