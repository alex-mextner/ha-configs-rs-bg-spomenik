# Memory limits: 3.7 GB of RAM and zram (configuration.nix) only, no disk swap. On Armbian the board froze in a
# swap thrash (Nov 14: memory and I/O pressure ~90%, 3 GB swapped to a USB disk). Here every service has a
# MemoryMax, so a runaway process is reclaimed or OOM-killed inside its own cgroup instead of dragging the
# whole board into swap. The units defined in the other modules set their own limits next to their definition;
# this file covers the slices and the services that come from nixpkgs.
#
# Budget (typical use / limit): containers ~1.3 GB / 2.7 GB together (docker.slice; each container also has
# its own compose limit), Kodi on demand ~0.3 GB / 1.2 GB, host services ~0.5 GB, interactive logins 1.5 GB.
{ lib, ... }:

let
  max = limit: { serviceConfig.MemoryMax = limit; };
in
{
  # All containers (compose limits per container: modules/home-assistant.nix, home-lan.nix, printops.nix) go
  # into docker.slice; past MemoryHigh the kernel reclaims (to zram) before MemoryMax OOM-kills inside the slice.
  virtualisation.docker.daemon.settings."cgroup-parent" = "docker.slice";
  systemd.slices.docker.sliceConfig = {
    MemoryHigh = "2400M";
    MemoryMax = "2700M";
  };

  # ssh sessions, agents and their node/python children.
  systemd.slices.user.sliceConfig.MemoryMax = "1536M";

  systemd.services = lib.mapAttrs (_: max) {
    # dockerd, its containerd and the containerd shims (the containers themselves are in docker.slice)
    docker = "640M";
    tailscaled = "256M";
    # local builds and substitutions only; evaluate on a bigger machine (configuration.nix, nix.settings)
    nix-daemon = "1536M";
    homelab-backup = "1G";
    homelab-backup-check = "1G";
    homelab-backup-drill = "768M";
    dotfiles = "512M";
    bluetooth = "96M";
    sshd = "128M";
    nscd = "64M";
    systemd-networkd = "64M";
    systemd-resolved = "64M";
    systemd-timesyncd = "32M";
    odroid-nic-alive = "32M";
  };
}
