# NixOS 26.05 configuration for the Mac mini Mid 2010 (Macmini4,1).
#
# Hardware: Core 2 Duo P8600 (x86-64 baseline, no SSE4.2/POPCNT/AVX),
# GeForce 320M, Broadcom BCM57765 Ethernet (tg3), BCM43224 Wi-Fi (unused),
# PNY CS900 240 GB SSD, 6 GB RAM, 64-bit Apple EFI.
#
# Headless server: SSH (keys only), Docker, Tailscale, mbpfan fan control.
# Written against nixpkgs f68407657ad2 (the nixos-26.05 channel shipped by
# nixos-minimal-26.05.14630.f68407657ad2-x86_64-linux.iso).
{
  config,
  lib,
  pkgs,
  ...
}:

{
  imports = [
    ./hardware-configuration.nix
    ../dotfiles/nixos.nix
  ];

  # --- Boot -----------------------------------------------------------------
  boot.loader.systemd-boot.enable = true;
  boot.loader.systemd-boot.configurationLimit = 10;
  # Apple EFI firmware does not use Boot#### NVRAM entries the way PC firmware
  # does, and writing its NVRAM from Linux is a known way to upset old Macs.
  # With this false, `bootctl install` runs with --no-variables; it still
  # copies systemd-boot to \EFI\BOOT\BOOTX64.EFI, the removable-media fallback
  # path that the Mac firmware boots from the internal disk.
  boot.loader.efi.canTouchEfiVariables = false;

  # Pin the 6.18 LTS kernel (also the 26.05 default). Linux 7.3 moved the
  # applesmc fan attributes to the standard hwmon ABI; mbpfan 2.4.0 (the
  # 26.05 package) only understands the old layout and exits with "could not
  # detect any fan" on 7.3+. Upstream fixed this in mbpfan 2.5.0. The
  # assertion below stops an accidental switch to linuxPackages_latest.
  boot.kernelPackages = pkgs.linuxPackages_6_18;

  # applesmc exposes the SMC fan/temperature interface, coretemp the CPU
  # sensors that mbpfan reads. (The mbpfan module adds these too; listed
  # explicitly so they stay even if mbpfan is disabled for debugging.)
  boot.kernelModules = [
    "applesmc"
    "coretemp"
  ];

  # Wi-Fi is unused; keep its drivers from loading.
  boot.blacklistedKernelModules = [
    "b43"
    "bcma"
    "brcmsmac"
  ];

  # nouveau is left enabled on purpose: it drives the local console that is
  # needed for recovery, and blacklisting it would not lower power use (the
  # firmware leaves the 320M at boot clocks either way).
  # To disable it anyway, add "nouveau" to the blacklist above.

  boot.tmp.cleanOnBoot = true;

  hardware.enableRedistributableFirmware = true;
  hardware.cpu.intel.updateMicrocode = true;

  # --- Fan control ----------------------------------------------------------
  # The SMC runs the fan at full speed (the original HDD thermal sensor is
  # likely missing). mbpfan takes manual control and scales the fan speed
  # with CPU temperature. Min/max fan RPM are read from applesmc (fan1_min /
  # fan1_max), so they are not hardcoded here.
  services.mbpfan = {
    enable = true;
    aggressive = false;
    settings.general = {
      low_temp = 55; # below this: minimum fan speed
      high_temp = 65; # above this: speed starts to rise
      max_temp = 80; # above this: full speed (P8600 Tjmax is 105 C)
      polling_interval = 2;
    };
  };

  assertions = [
    {
      assertion =
        lib.versionOlder config.boot.kernelPackages.kernel.version "7.3"
        || lib.versionAtLeast config.services.mbpfan.package.version "2.5.0";
      message = "mbpfan < 2.5.0 cannot find the applesmc fan on Linux >= 7.3; keep a kernel < 7.3 or use mbpfan >= 2.5.0.";
    }
  ];

  # --- Networking -----------------------------------------------------------
  networking.hostName = "macmini";

  # systemd-networkd with DHCP on the tg3 Ethernet port, matched by driver so
  # the interface name does not matter. useDHCP = false overrides the
  # mkDefault true written by nixos-generate-config.
  networking.useDHCP = false;
  networking.useNetworkd = true;
  systemd.network.networks."10-ethernet" = {
    matchConfig.Driver = "tg3";
    networkConfig.DHCP = "yes";
  };
  systemd.network.wait-online.anyInterface = true;

  networking.firewall.enable = true;
  # services.openssh.openFirewall (default true) opens TCP 22.

  services.tailscale = {
    enable = true;
    openFirewall = true; # UDP 41641 for direct connections
  };

  # --- SSH ------------------------------------------------------------------
  services.openssh = {
    enable = true;
    settings = {
      PasswordAuthentication = false;
      KbdInteractiveAuthentication = false;
      PermitRootLogin = "no";
    };
  };

  # --- Users ----------------------------------------------------------------
  users.users.ultra = {
    isNormalUser = true;
    extraGroups = [
      "wheel"
      "docker"
    ];
    openssh.authorizedKeys.keys = [
      # ~/.ssh/id_ed25519_home.pub on ubuntu-pc
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIMdEyhpamHIpibpBBnExkEe/sTf4ga0frBRWEQhBLQtD ultra@home"
      # from ultra@odroidn2:~/.ssh/authorized_keys
      "ssh-ed25519 AAAAC3NzaC1lZDI1NTE5AAAAIJ+7HMHqd/YcxRyIdnitdj3RA1WAqw1IN1ApJHHHW8er moshi"
    ];
  };
  # sudo asks for ultra's password; set it once with `passwd ultra`
  # (install-steps.md does this from the installer).

  # --- Containers -----------------------------------------------------------
  virtualisation.docker = {
    enable = true;
    autoPrune.enable = true;
  };

  # --- Memory ---------------------------------------------------------------
  zramSwap = {
    enable = true;
    memoryPercent = 50;
  };

  # --- Nix ------------------------------------------------------------------
  nix.settings = {
    max-jobs = 1;
    cores = 2;
    experimental-features = [
      "nix-command"
      "flakes"
    ];
    trusted-users = [
      "root"
      "@wheel"
    ];
    auto-optimise-store = true;
  };
  nix.gc = {
    automatic = true;
    dates = "weekly";
    options = "--delete-older-than 14d";
  };

  environment.systemPackages = with pkgs; [
    git
    htop
    lm_sensors
    smartmontools
    vim
  ];

  # ~/.files for ultra, zsh as its login shell (../dotfiles/nixos.nix)
  homelab.dotfiles.enable = true;

  time.timeZone = "Europe/Belgrade";
  i18n.defaultLocale = "en_US.UTF-8";

  system.stateVersion = "26.05";
}
