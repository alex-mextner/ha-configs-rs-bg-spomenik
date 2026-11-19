# tests/in-docker.sh: Debian trixie (the hosts' distribution) with what roundtrip.sh and module.sh need besides the
# pinned restic and rclone, which in-docker.sh mounts from the host
FROM debian:trixie
RUN apt-get update -q && DEBIAN_FRONTEND=noninteractive apt-get install -y -q --no-install-recommends \
	sqlite3 jq python3 diffutils util-linux curl ca-certificates && rm -rf /var/lib/apt/lists/*
