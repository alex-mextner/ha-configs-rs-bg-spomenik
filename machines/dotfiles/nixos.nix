# ~/.files (github.com/alex-mextner/.files) on a NixOS host, the way ubuntu-pc has it: a checkout at ~/.files of
# one user, applied by its own user-level installer (install-ubuntu.sh: links, shell rc blocks, submodules, zplug,
# vim-plug, rig config merge; it never calls apt or snap). Import it and set `homelab.dotfiles.enable = true`.
#
# The dotfiles.service oneshot runs once per boot (after network-online) and again on a switch that changes it
# (RemainAfterExit: an unchanged unit stays active, so other switches leave it alone); re-run it by hand with
# `sudo systemctl restart dotfiles`. It brings ~/.files to the pinned `rev` with debian/checkout (shared with the
# Debian module: a fresh clone, a fast-forward, or back over upstream commits only; local commits on top of `rev`
# stay; stale detached submodules synced and updated), then runs install-ubuntu.sh. The installer runs as a
# root-equivalent user, so a push to .files reaches a host only once its pin is bumped in this repo. The unit
# succeeds on the installer's exit 0 or 10 (only manual steps remain: GitHub access for the private submodules, bun;
# logged, `journalctl -u dotfiles`); anything else fails it, and so does a whole run longer than 15 minutes.
{
  config,
  lib,
  pkgs,
  ...
}:

let
  cfg = config.homelab.dotfiles;
  # rig/merge-config.py needs PyYAML; the installer runs it with `python3 -I`, which still sees this env's packages
  python = pkgs.python3.withPackages (ps: [ ps.pyyaml ]);
  # install-ubuntu.sh: git + curl (clones, vim-plug), zsh (zplug), vim (plugins), gh (the credential helper the
  # private submodules need once GitHub access is set up), ssh (ssh submodule URLs)
  tools = with pkgs; [
    git
    curl
    zsh
    vim
    gh
    openssh
    python
  ];
in
{
  options.homelab.dotfiles = {
    enable = lib.mkEnableOption "~/.files (github.com/alex-mextner/.files) for one user";
    user = lib.mkOption {
      type = lib.types.str;
      default = "ultra";
      description = "Whose home gets ~/.files (an existing users.users entry).";
    };
    repo = lib.mkOption {
      type = lib.types.str;
      default = "https://github.com/alex-mextner/.files.git";
      description = "Clone URL for a fresh checkout (public https: no GitHub auth needed).";
    };
    branch = lib.mkOption {
      type = lib.types.str;
      default = "master";
      description = "The branch ~/.files is checked out on (fast-forward only).";
    };
    rev = lib.mkOption {
      type = lib.types.strMatching "[0-9a-f]{40}";
      # one pin for the NixOS and the Debian module; debian/ is the directory rsync copies to a Debian host
      default = lib.fileContents ./debian/rev;
      defaultText = lib.literalExpression "lib.fileContents ./debian/rev";
      description = "The pinned .files commit (on `branch`): ~/.files is moved to it, forward or back.";
    };
    zshLoginShell = lib.mkOption {
      type = lib.types.bool;
      default = true;
      description = "Make zsh the user's login shell (the dotfiles are a zsh setup: .zprofile, zplug).";
    };
  };

  config = lib.mkIf cfg.enable {
    programs.zsh.enable = true;
    users.users.${cfg.user}.shell = lib.mkIf cfg.zshLoginShell pkgs.zsh;
    # the same tools for a manual re-run: ~/.files/install-ubuntu.sh [--plan|--check]
    environment.systemPackages = tools;

    systemd.services.dotfiles = {
      description = "~/.files for ${cfg.user} (github.com/alex-mextner/.files, install-ubuntu.sh)";
      wantedBy = [ "multi-user.target" ];
      wants = [ "network-online.target" ];
      after = [ "network-online.target" ];
      path =
        tools
        ++ (with pkgs; [
          bash
          coreutils
          findutils
          gawk
          gnugrep
          gnused
        ]);
      # Never prompt: an unreachable private repository must fail fast, so the installer reports a manual step.
      environment = {
        GIT_TERMINAL_PROMPT = "0";
        GIT_SSH_COMMAND = "ssh -o BatchMode=yes";
      };
      serviceConfig = {
        Type = "oneshot";
        RemainAfterExit = true;
        User = cfg.user;
        WorkingDirectory = "~";
        TimeoutStartSec = "15min";
        # install-ubuntu.sh exit 10: applied, only manual steps remain
        SuccessExitStatus = "10";
      };
      script = ''
        # what the user installed by hand counts too (bun after the manual step, per-user and system packages)
        export PATH="$HOME/.local/bin:$HOME/.bun/bin:/etc/profiles/per-user/$USER/bin:/run/current-system/sw/bin:$PATH"
        bash ${./debian/checkout} apply "$HOME/.files" ${lib.escapeShellArg cfg.repo} ${lib.escapeShellArg cfg.branch} ${lib.escapeShellArg cfg.rev}
        rc=0
        "$HOME/.files/install-ubuntu.sh" || rc=$?
        if [ "$rc" = 10 ]; then echo "dotfiles: applied; the manual: steps above remain (install-ubuntu.sh exit 10)"; fi
        exit "$rc"
      '';
    };
  };
}
