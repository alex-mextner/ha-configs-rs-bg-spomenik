# ~/.files (github.com/alex-mextner/.files) on a NixOS host, the way ubuntu-pc has it: a checkout at ~/.files of
# one user, applied by its own user-level installer (install-ubuntu.sh: links, shell rc blocks, submodules, zplug,
# vim-plug, rig config merge; it never calls apt or snap). Import it and set `homelab.dotfiles.enable = true`.
#
# The dotfiles.service oneshot runs once per boot (after network-online) and again on a switch that changes it
# (RemainAfterExit: an unchanged unit stays active, so other switches leave it alone); re-run it by hand with
# `sudo systemctl restart dotfiles`. It clones the public https URL, or fast-forwards an existing checkout that is
# on `branch` (and checks out the submodule commits it records), then runs install-ubuntu.sh. Exit 10 from the
# installer means only manual steps remain (GitHub access for the private submodules, bun): the unit logs them
# (`journalctl -u dotfiles`) and succeeds. Local commits that block a fast-forward fail the unit.
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
  # private submodules need once `gh auth login` is done), ssh (ssh submodule URLs)
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
      description = "The branch ~/.files follows (fast-forward only).";
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
      };
      script = ''
        # what the user installed by hand counts too (bun after the manual step, per-user and system packages)
        export PATH="$HOME/.local/bin:$HOME/.bun/bin:/etc/profiles/per-user/$USER/bin:/run/current-system/sw/bin:$PATH"
        files="$HOME/.files"
        if [ ! -e "$files" ]; then
          git clone --quiet --branch ${lib.escapeShellArg cfg.branch} ${lib.escapeShellArg cfg.repo} "$files"
        elif ! git -C "$files" rev-parse --git-dir >/dev/null 2>&1; then
          echo "dotfiles: $files exists but is not a git checkout; move it aside" >&2
          exit 1
        elif [ "$(git -C "$files" symbolic-ref --short -q HEAD || true)" = ${lib.escapeShellArg cfg.branch} ]; then
          git -C "$files" fetch --quiet origin ${lib.escapeShellArg cfg.branch}
          git -C "$files" merge --ff-only --quiet FETCH_HEAD
          # initialised submodules only; initialising the others is install-ubuntu.sh's business
          git -C "$files" submodule update --quiet --recursive
        else
          echo "dotfiles: $files is not on ${cfg.branch}: left as it is"
        fi
        rc=0
        "$files/install-ubuntu.sh" || rc=$?
        case $rc in
          0) ;;
          10) echo "dotfiles: applied; the manual: steps above remain (install-ubuntu.sh exit 10)" ;;
          *) exit "$rc" ;;
        esac
      '';
    };
  };
}
