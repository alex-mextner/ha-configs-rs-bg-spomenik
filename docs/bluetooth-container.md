# Bluetooth in Home Assistant Container

## Root cause and design

A privileged container on a Docker bridge can discover an adapter via the mounted
system D-Bus, yet cannot create Linux HCI sockets. Linux rejects AF_BLUETOOTH
outside the initial network namespace with EAFNOSUPPORT (errno 97). HA then
reports a degraded adapter even when effective capabilities already include
NET_ADMIN and NET_RAW. Adding capabilities alone does not fix that namespace.

The main Compose file therefore sets `network_mode: host`, explicitly lists
NET_ADMIN and NET_RAW, and mounts `/run/dbus` read-only. Existing `privileged`
access is retained for the installation's USB devices; this change does not
silently remove access required by other integrations.

HA still needs Docker names such as `wyoming-satellite`, `wyoming-vosk`,
`printer-proxy`, `u1-camera-proxy` and `hga-postgres`. The private `ha-docker-dns`
CoreDNS service forwards requests to Docker's dynamic resolver at 127.0.0.11.
Only DNS has a reserved IP (172.20.0.53); other containers may be recreated or
receive new addresses. When changing the Dockerfile base pins, bump the local image tag or rebuild it
with `scripts/ha_compose.sh build ha-docker-dns` before applying. The Corefile is
bind-mounted and does not require an image rebuild.
The small DNS image copies the official CoreDNS binary
into a BusyBox runtime; both multi-architecture base image indexes are pinned.
Its health check resolves `ha-docker-dns.` through Docker's forwarding path,
not the static HA hosts entry; HA waits for `service_healthy`.
Crash/OOM recovery retains Docker's `unless-stopped` restart policy. No public
secondary resolver is injected: that would leak private service names and cannot
resolve them. A stopped DNS service must be restored, not hidden by a fallback.
HA uses `dns_search: ["."]` so a host/VPN suffix does not rewrite Docker names.
Bridge services retain callbacks via `homeassistant` and `homeassistant.local`
using `host-gateway`; HA-to-service resolution remains dynamic through CoreDNS. No DNS port or database port is published on the host.
The subnet matches the existing Odroid deployment. A site using another subnet
must change the Compose IPAM, HA DNS IP, DNS static IP and Corefile gateway together.
Before `up`, `create`, or `run`, the wrapper compares the existing network's
actual IPAM with the rendered configuration and checks the DNS address owner.
For normal `up`, the wrapper first reserves and starts only DNS and waits for
its health check before other services can be recreated. DNS failure therefore
stops the apply before touching unrelated services. `up --no-start` and `up --dry-run` keep their
no-start/preview meaning; a requested `--build` also repairs DNS before waiting
for its health check. Commands go first; use deployment settings and
`COMPOSE_PROFILES` for global options.
Docker does not safely reallocate a live bridge merely because YAML changed;
a mismatch is refused before changing services. The wrapper never tears down a
shared network automatically. A fresh host creates the specified network directly.

## Deployment and migration

Use `scripts/ha_compose.sh` instead of invoking raw Compose, including voice-stack maintenance. It loads optional
machine overlays from the root-owned `/etc/default/ha-compose`, then always
applies `docker/bluetooth/compose.final.yaml` last. The final overlay removes
legacy bridge attachments and published HA ports, but preserves machine image
pins, volumes, memory limits and all other services. Compose >= 2.24.0 is required.

As part of provisioning a Debian/Ubuntu/Armbian Docker host:

```bash
sudo ./scripts/install_ha_bluetooth.sh /path/to/machine-overlay.yaml
./scripts/ha_compose.sh up -d
```

Without overlay arguments, the installer preserves the active HA container's
Compose overlays. For a fresh checkout without a machine overlay, omit the
argument. Docker Engine/Compose and the normal HA secrets/config must already
be provisioned. This installer is Bluetooth/HA-service provisioning, not a full
OS restore or secret-distribution system.

The installer backs up the previous unit/settings, installs missing BlueZ/D-Bus
packages, starts Bluetooth, and enables the tracked `ha-compose.service`.
Every boot repeats the idempotent host prerequisites before bringing up Compose.
It does not bind configuration to a MAC, hci index, hostname or network-card name.
The privileged boot helper is installed as a root-owned system copy at
`/usr/local/libexec/ha-bluetooth-host`, not executed from a writable checkout.
Candidate settings and units are checked before they replace active files;
failed installation preserves the previous deployment configuration.
An already running BlueZ daemon is not restarted. The installer itself does not
interrupt HA: the explicit Compose apply above recreates HA when needed.

For an existing machine the safe targeted application is:

```bash
./scripts/ha_compose.sh config --quiet
./scripts/ha_validate_yaml.sh configuration.yaml
./scripts/ha_compose.sh up -d --no-deps ha-docker-dns
./scripts/ha_compose.sh up -d --no-deps homeassistant
```

Do not pull a new HA version as part of this permission fix. Machine overlays
retain the deployed version. Do not edit `.storage` or databases to hide repairs.

## Verification

```bash
python3 -m unittest discover -s tests -p test_bluetooth_deployment.py -v
systemctl is-enabled bluetooth.service ha-compose.service
systemctl is-active bluetooth.service ha-compose.service
./scripts/ha_compose.sh ps
```

After applying, verify the actual container (not only YAML): its network mode is
`host`, both capabilities are effective, the D-Bus socket is accessible, a raw
HCI management socket can read the management protocol version, and HA's
Bluetooth diagnostics show received advertisements without `missing_permissions`.
Check the Repairs API and HA logs. Also resolve the above Docker service names
from inside HA, test their existing ports, and confirm external DNS still works.
Repeat after a service reload or host reboot; an HTTP-healthy HA alone is not
proof of working Bluetooth.

## References

- https://www.home-assistant.io/integrations/bluetooth/#additional-details-for-container
- https://github.com/torvalds/linux/blob/master/net/bluetooth/af_bluetooth.c
- https://docs.docker.com/compose/how-tos/multiple-compose-files/merge/
- https://coredns.io/plugins/forward/
