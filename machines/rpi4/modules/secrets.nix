# sops-nix: the secrets are in git encrypted (age) and are decrypted at activation with the host
# key into the tmpfs /run/secrets; they never reach /nix/store. The file is *.sops.yaml because the
# repo's .gitignore keeps every plain secrets.yaml (Home Assistant's) out of git.
{
  sops = {
    defaultSopsFile = ../secrets/secrets.sops.yaml;
    # SSH host key of the Pi -> age key. Its public half (ssh-to-age) is the rpi4 recipient in
    # ../.sops.yaml.
    age.sshKeyPaths = [ "/etc/ssh/ssh_host_ed25519_key" ];
    # No separate age key on the device.
    age.generateKey = false;
  };
}
