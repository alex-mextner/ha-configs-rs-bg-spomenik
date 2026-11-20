# sops-nix: the secrets are in git encrypted (age, ../secrets/secrets.sops.yaml, recipients in ../.sops.yaml) and
# are decrypted at activation into the tmpfs /run/secrets; they never reach /nix/store. The host decrypts with
# /etc/ssh/ssh_host_ed25519_key, the Armbian install's key carried over by MIGRATION.md (step 6): the same key keeps
# known_hosts valid and is the `odroidn2` recipient. Edit: `cd machines/odroid-n2plus && sops secrets/secrets.sops.yaml`.
#
# Here: long-lived values that configure a service, and values that must not appear in this public repository
# (tailnet addresses). Not here (README.md, Secrets): application state that moves with its data (Home Assistant's
# secrets.yaml/.env and .storage, tailscale node state, Anton's keys under /home/anton), the backup password and
# rclone token (machines/backup README, provisioned by hand), and the one-hour GitHub runner registration token
# (edge.nix).
{
  sops = {
    defaultSopsFile = ../secrets/secrets.sops.yaml;
    age.sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
    # No separate age key on the device.
    age.generateKey = false;
    # Cloudflare Tunnel token of the remotely managed tunnel (Armbian: /etc/cloudflared/token).
    secrets.cloudflared-token = { };
    # JSON merged over the Transmission settings: the RPC whitelist with this host's tailnet address.
    secrets.transmission-settings = { };
  };
}
