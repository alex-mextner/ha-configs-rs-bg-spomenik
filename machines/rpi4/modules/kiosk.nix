# Kiosk: cage (Wayland, a single app) + Chromium full screen, without any browser UI.
# Chromium is driven from outside over CDP (DevTools) on 127.0.0.1:9222: tabs, scrolling, the
# Home Assistant auto-login (kiosk-ha-login.nix). Port 9222 is not open in the firewall.
{
  config,
  lib,
  pkgs,
  ...
}:
let
  cfg = config.rpi4.kiosk;
  cdpPort = 9222;
  pages = cfg.urls ++ lib.optional (cfg.agentTv.url != null) cfg.agentTv.url;

  # Root cause of "Chromium with its UI": cage 0.3.0 ignores a fullscreen request a window sends
  # before it is mapped, which is exactly what Chromium does with --kiosk. The window came up
  # maximized with tabs and the address bar. Fixed upstream in cage 0.3.1, nixpkgs 26.05 still
  # ships 0.3.0. Second patch: the cursor is never drawn (Chromium itself asks for an arrow
  # in the middle of the screen on pointer-enter, and libinput takes the vc4-hdmi HDMI-CEC devices
  # for a pointer).
  cage = pkgs.cage.overrideAttrs (old: {
    patches = (old.patches or [ ]) ++ [
      ./cage-fullscreen-on-startup.patch
      ./cage-hide-cursor.patch
    ];
  });

  browser = pkgs.writeShellScript "kiosk-browser" ''
    exec ${lib.getExe pkgs.chromium} \
      --kiosk \
      --ozone-platform=wayland \
      --user-data-dir="$HOME/.config/chromium-kiosk" \
      --remote-debugging-address=127.0.0.1 \
      --remote-debugging-port=${toString cdpPort} \
      --noerrdialogs \
      --disable-infobars \
      --hide-crash-restore-bubble \
      --no-first-run \
      --no-default-browser-check \
      --password-store=basic \
      --overscroll-history-navigation=0 \
      --check-for-update-interval=31536000 \
      ${lib.escapeShellArgs pages}
  '';
in
{
  users.users.kiosk = {
    isNormalUser = true;
    extraGroups = [
      "video"
      "render" # GPU 3D acceleration
    ];
  };

  services.cage = {
    enable = true;
    package = cage;
    user = "kiosk";
    program = "${browser}";
    # -s: allow VT switching, -d: no client-side window decorations
    extraArguments = [
      "-s"
      "-d"
    ];
    # Without a keyboard or mouse wlroots otherwise fails ("no input devices") and the kiosk
    # does not start.
    environment.WLR_LIBINPUT_NO_DEVICES = "1";
  };

  # Fonts for ordinary pages (NixOS has none by default).
  fonts.enableDefaultPackages = true;

  systemd.services."cage-tty1" = {
    # The cage module sets restartIfChanged=false: without this a new URL would wait for a reboot.
    restartIfChanged = lib.mkForce true;
    # cage exits together with Chromium: bring the kiosk back after a browser crash.
    serviceConfig = {
      Restart = lib.mkForce "always";
      RestartSec = 5;
    };
  };
}
