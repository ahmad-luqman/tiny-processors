#!/bin/bash
# Runs inside the container (issue #36): tools/rv32_linux.py --build starts it with
#   /src    programs/rv32/linux, read-only
#   /dtb    the Linux tree tools/rv32_dtb.py wrote (linux.dtb), read-only
#   /build  a Docker volume: Buildroot, its downloads and its output
#   /out    third_party/linux, where the results go
# and BR_VERSION, BR_SHA256, SOURCE_DATE_EPOCH and INPUTS (a hash of everything above) set.
set -euo pipefail
cd /build
tarball=buildroot-$BR_VERSION.tar.xz
if [ ! -f "$tarball" ] || ! echo "$BR_SHA256  $tarball" | sha256sum -c --quiet; then
    rm -f "$tarball"
    wget -q "https://buildroot.org/downloads/$tarball"
    echo "$BR_SHA256  $tarball" | sha256sum -c --quiet
fi
[ -f "buildroot-$BR_VERSION/Makefile" ] || tar xf "$tarball"  # a seeded dl/ alone is not a checkout
cd "buildroot-$BR_VERSION"
# A build from other inputs starts over (the downloads in dl/ stay).
if [ "$(cat /build/inputs 2>/dev/null)" != "$INPUTS" ]; then
    make clean >/dev/null
    rm -rf /build/gen
fi
mkdir -p /build/gen
dtc -q -I dtb -O dts -o /build/gen/tiny-processors.dts /dtb/linux.dtb
cp /src/buildroot.defconfig configs/tiny_processors_defconfig
make tiny_processors_defconfig >/dev/null
# The kernel's and busybox's configurations need their sources, and the kernel's needs the
# compiler it will be built with, so the toolchain comes first.
make toolchain linux-extract busybox-extract
kernel=$(sed -n 's/^BR2_LINUX_KERNEL_CUSTOM_VERSION_VALUE="\(.*\)"$/\1/p' configs/tiny_processors_defconfig)
linux=output/build/linux-$kernel busybox=$(echo output/build/busybox-[0-9]*)
(cd "$linux" && ARCH=riscv CROSS_COMPILE=/build/buildroot-$BR_VERSION/output/host/bin/riscv32-buildroot-linux-uclibc- \
    scripts/kconfig/merge_config.sh -n kernel/configs/tiny-base.config kernel/configs/tiny.config /src/linux.fragment >/dev/null \
 && grep -q '^CONFIG_BUILTIN_DTB=y' .config \
 && make ARCH=riscv savedefconfig >/dev/null && cp defconfig /build/gen/linux.config)
rm -rf /tmp/busybox && cp -a "$busybox" /tmp/busybox
(cd /tmp/busybox && make allnoconfig >/dev/null \
 && grep -v '^#' /src/busybox.fragment | sed 's/=y$//' | while read -r opt; do
        sed -i "s/^# $opt is not set\$/$opt=y/" .config
    done \
 && { set +o pipefail; yes "" | make oldconfig >/dev/null 2>&1; set -o pipefail; } \
 && grep -v '^#' /src/busybox.fragment | while read -r line; do
        grep -qx "$line" .config || { echo "busybox: $line did not stick" >&2; exit 1; }
    done \
 && cp .config /build/gen/busybox.config)
make
echo "$INPUTS" > /build/inputs
cp output/images/Image output/images/rootfs.cpio /out/
cp "$linux/.config" /out/linux.config
cp "$busybox/.config" /out/busybox.config
cp /build/gen/tiny-processors.dts /out/
