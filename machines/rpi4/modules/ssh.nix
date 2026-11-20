# SSH with keys only + mDNS (rpi4.local).
{ lib, ... }:
{
  services.openssh = {
    enable = true;
    # Keys only from this repo (/etc/ssh/authorized_keys.d): a key removed from ../keys must not
    # keep working through ~/.ssh/authorized_keys.
    authorizedKeysInHomedir = false;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "prohibit-password";
    };
  };

  # Every public key in ../keys/*.pub gets root access: adding a key = adding a file there.
  users.users.root.openssh.authorizedKeys.keyFiles = lib.pipe (builtins.readDir ../keys) [
    builtins.attrNames
    (lib.filter (lib.hasSuffix ".pub"))
    (map (name: ../keys + "/${name}"))
  ];

  services.avahi = {
    enable = true;
    nssmdns4 = true;
    publish = {
      enable = true;
      addresses = true;
    };
  };
}
