# The Linux build's container (issue #36, docs/rv32-linux.md): Debian bookworm pinned by digest,
# with its packages from snapshot.debian.org at a fixed date, so every build sees the same host
# tools. Buildroot builds the cross toolchain itself, from sources whose hashes it checks.
FROM debian:bookworm@sha256:f37a335e82bca302e955fa39f9dfe28f1be618f016f8a2b56318e5a5111afc26
ARG SNAPSHOT=20261001T000000Z
RUN rm -f /etc/apt/sources.list.d/debian.sources \
 && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian/${SNAPSHOT} bookworm main" > /etc/apt/sources.list \
 && echo "deb [check-valid-until=no] http://snapshot.debian.org/archive/debian-security/${SNAPSHOT} bookworm-security main" >> /etc/apt/sources.list \
 && apt-get update \
 && DEBIAN_FRONTEND=noninteractive apt-get install -y --no-install-recommends \
    bc bison build-essential bzip2 ca-certificates cpio device-tree-compiler file findutils flex git gzip \
    libncurses-dev patch perl python3 rsync tar unzip wget which xz-utils \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 builder && mkdir /build /dl && chown builder /build /dl
USER builder
WORKDIR /build
