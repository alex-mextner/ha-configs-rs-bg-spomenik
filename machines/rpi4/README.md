# rpi4: wall-display kiosk

Raspberry Pi 4 Model B, 2 GB RAM, SD card, NixOS 26.05, Wi-Fi only. Its one job is the wall display:
`cage` (Wayland) + Chromium full screen, without browser UI or cursor, on Home Assistant, logged in
automatically. Reached as `root@rpi4.local` (mDNS) with a key from [`keys/`](keys/).

Moved here from [alex-mextner/rpi-nixos-display](https://github.com/alex-mextner/rpi-nixos-display),
which also ran k3s + ArgoCD on this Pi. Nothing in that cluster was in use (ArgoCD itself and a
traefik/whoami sample app, no volumes), and it took most of the 2 GB, so it is gone;
[`modules/k3s-teardown.nix`](modules/k3s-teardown.nix) removes its leftover containers, mounts and
`/var/lib/rancher` on the first switch to this config.

## Layout

```
configuration.nix            host settings: kiosk pages, hostname, odroidn2 in /etc/hosts, nix gc
modules/options.nix          rpi4.kiosk.* options
modules/hardware.nix         sd-image-aarch64 (fileSystems by the labels NIXOS_SD / FIRMWARE, U-Boot + extlinux), zram, HDMI params
modules/secrets.nix          sops-nix: decryption key = the Pi's SSH host key
modules/wifi.nix             wpa_supplicant, password from the secret wifi-env; Wi-Fi power saving off
modules/ssh.nix              sshd with keys/*.pub for root, avahi (rpi4.local)
modules/kiosk.nix            cage (two patches) + Chromium --kiosk, CDP on 127.0.0.1:9222
modules/kiosk-ha-login.*     Home Assistant auto-login: tokens into the tab's localStorage over CDP
modules/autoupgrade.nix      nightly nixos-rebuild switch from main of this repo
modules/k3s-teardown.nix     one-time cleanup of the removed k3s cluster
secrets/secrets.sops.yaml    ENCRYPTED secrets (sops, age), see below
.sops.yaml                   age recipients: the admin key and the Pi's host key
scripts/flash-sd.sh          write an SD image while keeping the Pi's SSH host key (tests: scripts/test-flash-sd.sh)
scripts/check-secrets-encrypted.py   every value in a sops file is ciphertext (run by .githooks/pre-commit;
                                     tests: scripts/test-check-secrets-encrypted.py)
```

## Kiosk

`rpi4.kiosk` in [`configuration.nix`](configuration.nix):

- `urls`: the pages; the first one is active, the others open in background tabs.
- `homeAssistant.url`: the HA page out of `urls` that `kiosk-ha-login` signs in to with the secrets
  `ha-username` and `ha-password`; `null` turns the auto-login off.
- `agentTv.url`: the agent-tv dashboard as one more background tab (off by default). Put only the
  base URL there: the one-time `?token=` link is a secret, open it once over CDP and agent-tv keeps
  the key in a cookie of the kiosk profile.

Chromium listens for DevTools only on `127.0.0.1:9222` (not open in the firewall). From the Pi:
`curl -s http://127.0.0.1:9222/json/list`; from another machine: `ssh -L 9222:127.0.0.1:9222 root@rpi4.local`.

## Secrets

`secrets/secrets.sops.yaml` is encrypted for two age recipients ([`.sops.yaml`](.sops.yaml)): the admin
key (`~/.config/sops/age/keys.txt` on the admin machine) and the Pi (`ssh-to-age` of
`/etc/ssh/ssh_host_ed25519_key.pub`). The Pi decrypts at activation into `/run/secrets` (tmpfs);
nothing reaches `/nix/store`.

| key | content |
|---|---|
| `wifi-env` | one line `WIFI_PSK=<Wi-Fi password>` (8-63 characters; everything after the first `=` counts) |
| `ha-username`, `ha-password` | Home Assistant account of the kiosk auto-login |

Edit (sops finds `.sops.yaml` from the current directory):

```sh
cd machines/rpi4
sops secrets/secrets.sops.yaml
```

The file name is `*.sops.yaml` because the repo's `.gitignore` drops every `secrets.yaml` (Home
Assistant's plain one).

### New SD card or host key

A new `/etc/ssh/ssh_host_ed25519_key` is a new age key: the Pi can no longer decrypt the old file, so it
has no Wi-Fi either. Read the public key off the card on a PC (partition `NIXOS_SD`) or over Ethernet:

```sh
ssh-to-age < /mnt/etc/ssh/ssh_host_ed25519_key.pub   # card mounted at /mnt; new age1...
# replace &rpi4 in .sops.yaml, then from machines/rpi4:
sops updatekeys secrets/secrets.sops.yaml
sops rotate --in-place secrets/secrets.sops.yaml   # new data key: the old host key counts as compromised
ssh-keygen -R rpi4.local
```

## Updates

`system.autoUpgrade` switches every night at 04:00 (+ up to 30 min) to
`github:alex-mextner/ha-configs-rs-bg-spomenik?dir=machines#rpi4`, i.e. `main` of this repo, without a
reboot. Kernel, initrd, `boot.kernelParams` and the bootloader take effect at the next boot; everything
else (kiosk pages, services, secrets, Wi-Fi) applies on the switch.

Evaluating the flake on the Pi itself costs 0.4-1 GB of RAM. For a deploy right now, evaluate on a
workstation and build only what is missing on the Pi (the rest comes from cache.nixos.org):

```sh
drv=$(nix eval --raw ./machines#nixosConfigurations.rpi4.config.system.build.toplevel.drvPath)
nix copy --derivation --substitute-on-destination --to ssh-ng://root@rpi4.local "$drv"
out=$(ssh root@rpi4.local "nix-store --realise --max-jobs 1 $drv" | tail -1)
ssh root@rpi4.local "$out/bin/switch-to-configuration test"   # apply without touching the bootloader; a reboot rolls back
ssh root@rpi4.local "nix-env -p /nix/var/nix/profiles/system --set $out && $out/bin/switch-to-configuration switch"
```

Rollback: `nixos-rebuild switch --rollback` on the Pi.

## SD image

Commands in this and the next section run from `machines/rpi4`.

```sh
nix build ..#packages.aarch64-linux.rpi4-sdImage   # aarch64 builder, native or binfmt
# -> machines/rpi4/result/sd-image/rpi4-kiosk.img.zst
```

The image carries the encrypted secrets but no host key. `scripts/flash-sd.sh --image <file>` (as root)
finds the one removable SD card, copies its `/etc/ssh/ssh_host_ed25519_key` to tmpfs, writes the image,
verifies it and puts the key back, so the Pi can still decrypt its secrets. `--dry-run` only checks and
prints the plan.

### If the host key is lost

Make a new key, re-encrypt for it with a new data key (whoever has the lost card holds the old key),
change the secrets themselves, then build and flash with it. `flash-sd.sh` only accepts a card that
already carries a NixOS SD image (partitions `FIRMWARE` and `NIXOS_SD`); write a blank card once with
`zstdcat result/sd-image/rpi4-kiosk.img.zst | sudo dd of=/dev/sdX bs=4M conv=fsync` first.

```sh
ssh-keygen -t ed25519 -N '' -C root@rpi4 -f ~/rpi4-hostkey
ssh-to-age < ~/rpi4-hostkey.pub        # new age1...: put it into &rpi4 in .sops.yaml
sops updatekeys secrets/secrets.sops.yaml
sops rotate --in-place secrets/secrets.sops.yaml
sops secrets/secrets.sops.yaml         # new Wi-Fi and HA passwords (change them on the router and in HA too)
# commit, build the image, then:
sudo bash scripts/flash-sd.sh --image result/sd-image/rpi4-kiosk.img.zst --hostkey ~/rpi4-hostkey \
  && shred -u ~/rpi4-hostkey ~/rpi4-hostkey.pub
```

## Risks

- The auto-upgrade trusts `main` of this public repo: whoever can merge into it is root on the Pi a day
  later. The secrets stay encrypted, but the config does not.
- The encrypted secrets are in public history forever. After a key leak remove the key from
  `.sops.yaml`, run `sops updatekeys` and `sops rotate`, and change the secrets themselves.
- The host key exists only on the SD card; losing the card means the steps above.
- `flash-sd.sh` recognises the Pi's card only by the partition labels every NixOS SD image has, and does
  not check that the host key it keeps is the `&rpi4` recipient: check the device it prints before
  confirming.
