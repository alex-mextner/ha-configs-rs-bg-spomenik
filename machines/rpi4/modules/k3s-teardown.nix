# Clears what is left of the k3s + ArgoCD cluster this Pi used to run (nothing on it was in use:
# only ArgoCD itself and a traefik/whoami sample app, no volumes). Dropping services.k3s stops
# k3s.service on switch, but that unit has KillMode=process: containerd, the pods, their mounts,
# the CNI links and the KUBE-* iptables rules outlive it until a reboot, and /var/lib/rancher keeps
# ~2 GB of images on the SD card. nixpkgs' k3s-killall.sh does this cleanup but would pull the whole
# k3s closure (~650 MB) back in, so the same steps run here with tools the system already has.
# Once /var/lib/rancher is gone the unit is skipped on every switch and boot.
{ config, pkgs, ... }:
{
  systemd.services.k3s-teardown = {
    description = "Remove the leftovers of the k3s cluster that used to run here";
    wantedBy = [ "multi-user.target" ];
    # The iptables save/restore below must not race the firewall loading its rules at boot.
    after = [ "firewall.service" ];
    unitConfig.ConditionPathExists = "/var/lib/rancher";
    path = [
      pkgs.coreutils
      pkgs.util-linux
      pkgs.iproute2
      pkgs.gnugrep
      pkgs.gawk
      config.networking.firewall.package
    ];
    serviceConfig.Type = "oneshot";
    script = ''
      # The pods live in kubepods.slice (systemd cgroup driver); containerd and the shims stay in
      # the cgroup of the stopped k3s.service.
      cgroups="/sys/fs/cgroup/kubepods.slice /sys/fs/cgroup/system.slice/k3s.service"
      for cg in $cgroups; do
        if [ -e "$cg/cgroup.kill" ]; then echo 1 > "$cg/cgroup.kill"; fi
      done
      # cgroup.kill is asynchronous: wait until both cgroups are empty.
      for _ in $(seq 30); do
        busy=0
        for cg in $cgroups; do
          if grep -qx 'populated 1' "$cg/cgroup.events" 2>/dev/null; then busy=1; fi
        done
        [ "$busy" = 0 ] && break
        sleep 1
      done

      # Container rootfs, pod volumes and network namespaces, deepest first.
      k3s_mounts() {
        awk '{ print $2 }' /proc/self/mounts \
          | grep -E '^(/run/k3s|/var/lib/kubelet|/var/lib/rancher|/run/netns/cni-)' || true
      }
      k3s_mounts | sort -r | while read -r mount; do umount -l "$mount" || true; done
      # A mount left behind (including a bind mount of the root filesystem, which
      # rm --one-file-system would walk into) stops the unit before any rm; the next boot,
      # with nothing mounted, retries.
      if [ -n "$(k3s_mounts)" ]; then
        echo "k3s mounts still present, not deleting:" >&2
        k3s_mounts >&2
        exit 1
      fi
      rm -f /run/netns/cni-*

      for link in cni0 flannel.1 flannel-v6.1 flannel-wg flannel-wg-v6 kube-ipvs0; do
        if ip link show "$link" > /dev/null 2>&1; then ip link delete "$link"; fi
      done

      # Same filter as k3s-killall.sh: keep every rule that is not k3s's (the NixOS firewall's
      # nixos-fw chains stay untouched). Rules do not survive a reboot, so a retry at boot has
      # nothing to drop and leaves the tables alone.
      for tool in iptables ip6tables; do
        if "$tool-save" | grep -qiE 'KUBE-|CNI-|flannel'; then
          "$tool-save" | grep -v KUBE- | grep -v CNI- | grep -iv flannel | "$tool-restore"
        fi
      done

      # --one-file-system as a second guard against crossing into another filesystem.
      rm -rf --one-file-system /var/lib/rancher /etc/rancher /var/lib/kubelet /var/lib/cni \
        /run/k3s /run/flannel /root/.kube
    '';
  };
}
