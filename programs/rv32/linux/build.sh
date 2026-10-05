#!/bin/bash
# Runs inside the container (issue #36): tools/rv32_linux.py --build starts it with
#   /src    programs/rv32/linux, read-only
#   /dtb    the Linux tree tools/rv32_dtb.py wrote (linux.dtb), read-only
#   /build  this checkout's Docker volume: Buildroot and its output; the results go to /build/results
#   /dl     a Docker volume of downloads shared by every checkout (Buildroot checks each by hash)
# and BR_VERSION, BR_SHA256, SOURCE_DATE_EPOCH, INPUTS (a hash of everything above) and BR2_DL_DIR
# set. The caller copies /build/results out as the host's user.
set -euo pipefail
cd /build
tarball=buildroot-$BR_VERSION.tar.xz
tree=buildroot-$BR_VERSION
if [ ! -f "$tarball" ] || ! echo "$BR_SHA256  $tarball" | sha256sum -c --quiet; then
    rm -f "$tarball"
    wget -q "https://buildroot.org/downloads/$tarball"
    echo "$BR_SHA256  $tarball" | sha256sum -c --quiet
fi
# Extract beside the tree and move into place, so an interrupted extraction is never reused.
if [ ! -f "$tree/.extracted" ]; then
    rm -rf "$tree.new" && mkdir "$tree.new"
    tar xf "$tarball" -C "$tree.new" --strip-components=1
    rm -rf "$tree" && mv "$tree.new" "$tree" && touch "$tree/.extracted"
fi
cd "$tree"
cross=$PWD/output/host/bin/riscv32-buildroot-linux-uclibc-

# Every `CONFIG_X=...` line of a fragment must be in a configuration, and no `# CONFIG_X is not set`
# option may be enabled there (one that is absent is not visible here, which is also not set).
check_fragment() { # fragment config name
    local missing
    missing=$( { grep '^CONFIG_' "$1" | grep -vxF -f "$2"
                 sed -n 's/^# \(CONFIG_[A-Za-z0-9_]*\) is not set$/\1/p' "$1" | while read -r option; do
                     grep -q "^$option=" "$2" && echo "# $option is not set"; done; } || true)
    if [ -n "$missing" ]; then
        echo "$3: these fragment lines did not stick:" >&2
        echo "$missing" >&2
        return 1
    fi
}

# A build from other inputs, or one that never finished, starts over (dl/ stays). The
# configurations are made only here, in a tree Buildroot has not configured yet.
if [ "$(cat /build/inputs 2>/dev/null)" != "$INPUTS" ]; then
    make clean >/dev/null
    rm -rf /build/gen /build/inputs
    mkdir -p /build/gen
    dtc -q -I dtb -O dts -o /build/gen/tiny-processors.dts /dtb/linux.dtb
    cp /src/buildroot.defconfig configs/tiny_processors_defconfig
    make tiny_processors_defconfig >/dev/null
    # The kernel's configuration needs its sources and the compiler it will be built with, so the
    # toolchain comes first.
    make toolchain linux-extract busybox-extract
    kernel=$(sed -n 's/^BR2_LINUX_KERNEL_CUSTOM_VERSION_VALUE="\(.*\)"$/\1/p' configs/tiny_processors_defconfig)
    # Kconfig probes the compiler (TOOLCHAIN_HAS_ZBB and the like), so every step that evaluates the
    # configuration must see the cross compiler, not the container's own.
    (cd "output/build/linux-$kernel" && export ARCH=riscv CROSS_COMPILE=$cross \
     && scripts/kconfig/merge_config.sh -n \
            kernel/configs/tiny-base.config kernel/configs/tiny.config /src/linux.fragment > /build/gen/merge.log \
     && check_fragment /src/linux.fragment .config linux \
     && make savedefconfig >/dev/null && cp defconfig /build/gen/linux.config)
    rm -rf /tmp/busybox && cp -a output/build/busybox-[0-9]*/ /tmp/busybox
    options=$(grep -v '^#' /src/busybox.fragment)
    (cd /tmp/busybox && make allnoconfig >/dev/null \
     && while read -r line; do sed -i "s/^# ${line%=y} is not set\$/$line/" .config; done <<< "$options" \
     && { make oldconfig < <(yes "") > /build/gen/busybox-oldconfig.log 2>&1 \
          || { cat /build/gen/busybox-oldconfig.log >&2; echo "busybox: make oldconfig failed" >&2; false; }; } \
     && check_fragment /src/busybox.fragment .config busybox \
     && cp .config /build/gen/busybox.config)
fi
make
# Buildroot adjusts both configurations again (initramfs, nommu); the fragments must survive that too.
linux=$(echo output/build/linux-[0-9]*/) busybox=$(echo output/build/busybox-[0-9]*/)
check_fragment /src/linux.fragment "$linux/.config" "linux (as built)"
check_fragment /src/busybox.fragment "$busybox/.config" "busybox (as built)"
rm -rf /build/results && mkdir /build/results
cp output/images/Image output/images/rootfs.cpio /build/gen/tiny-processors.dts /build/results/
cp "$linux/.config" /build/results/linux.config
cp "$busybox/.config" /build/results/busybox.config
echo "$INPUTS" > /build/inputs
