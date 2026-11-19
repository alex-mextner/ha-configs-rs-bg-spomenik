#!/usr/bin/env bash
# Build the ODROID-N2+ SD/eMMC image on an x86_64 Linux host that has Docker but no system Nix
# and no host-wide aarch64 binfmt registration (no root needed beyond the docker group).
#
# How: a privileged nixos/nix container creates its own user namespace and mounts a private
# binfmt_misc instance there (Linux >= 6.7 "sandboxed" binfmt_misc). qemu-aarch64 is registered
# only inside that namespace, so the host's /proc/sys/fs/binfmt_misc is never touched. The
# aarch64 parts of the closure come from cache.nixos.org; the few local derivations (U-Boot,
# initrd, rootfs image) run under qemu-user, and the Amlogic signing step runs natively on x86_64.
#
# Usage: machines/odroid-n2plus/scripts/build-sd-image.sh [output-dir]
#   (default output: ${XDG_CACHE_HOME:-~/.cache}/odroid-n2plus-image, outside the repo)
# The Nix store lives in the docker volume `nix-odroid-n2plus` and is reused across runs.
set -euo pipefail

if [ "${1:-}" = "--inside-container" ]; then
  export NIX_CONFIG="experimental-features = nix-command flakes
extra-platforms = aarch64-linux
system-features = nixos-test benchmark big-parallel
max-jobs = 1
cores = 4"
  tools=$(nix build --no-link --print-out-paths --inputs-from /flake \
    nixpkgs#qemu-user nixpkgs#util-linux.bin)
  qemu=$(echo "$tools" | grep qemu-user)/bin/qemu-aarch64
  utilbin=$(echo "$tools" | grep util-linux)/bin
  # Full 0..65535 identity map so nixbld build users and the volume's file owners stay valid.
  exec "$utilbin/unshare" --user --map-users=0:0:65536 --map-groups=0:0:65536 --mount -- sh -euc '
    mkdir -p /run/binfmt
    "$1/mount" -t binfmt_misc binfmt_misc /run/binfmt
    # aarch64 ELF magic/mask as in qemu scripts/qemu-binfmt-conf.sh; F = open interpreter now.
    printf "%s" ":qemu-aarch64:M::\x7fELF\x02\x01\x01\x00\x00\x00\x00\x00\x00\x00\x00\x00\x02\x00\xb7\x00:\xff\xff\xff\xff\xff\xff\xff\x00\xff\xff\xff\xff\xff\xff\xff\xff\xfe\xff\xff\xff:$0:F" > /run/binfmt/register
    out=$(nice -n 10 "$1/ionice" -c3 nix build --no-link --print-out-paths --print-build-logs \
      /flake#packages.x86_64-linux.odroid-n2plus-sdImage)
    img=$(ls "$out"/sd-image/*.img.zst)
    cp -f "$img" /out/
    cd /out && sha256sum "$(basename "$img")" > "$(basename "$img").sha256"
    # Hand the files to whoever owns the output directory instead of container root.
    chown "$(stat -c %u:%g /out)" "$(basename "$img")" "$(basename "$img").sha256"
    echo "built: /out/$(basename "$img")"
  ' "$qemu" "$utilbin"
fi

# The machines/ flake (two levels up from this script) is mounted read-only as /flake.
here=$(cd "$(dirname "$0")/../.." && pwd)
default_out="${XDG_CACHE_HOME:-$HOME/.cache}/odroid-n2plus-image"
outdir=$(mkdir -p "${1:-$default_out}" && cd "${1:-$default_out}" && pwd)

# Bounded so the build host (shared desktop, already deep in swap) stays responsive. nice/ionice
# are applied to nix inside the container: the docker client's priority does not reach it.
docker run --rm --privileged \
  --memory 8g --memory-swap 8g --cpus 4 \
  -v nix-odroid-n2plus:/nix \
  -v "$here":/flake:ro \
  -v "$outdir":/out \
  nixos/nix@sha256:b49cb33f3a7e5cf3933ad8774529de0e3807ec5cf54987aa7ccdfb49f2fc55ed \
  bash /flake/odroid-n2plus/scripts/build-sd-image.sh --inside-container
