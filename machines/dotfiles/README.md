# dotfiles: ~/.files on every homelab machine

[github.com/alex-mextner/.files](https://github.com/alex-mextner/.files) checked out at `~/.files` of `ultra` and
applied by its own user-level installer, `install-ubuntu.sh` (links, one marked block in `~/.profile` `~/.bashrc`
`~/.zshrc`, the `.gitconfig.shared` include, submodules, zplug, vim-plug, rig global config merge), the same way
ubuntu-pc has it. The installer calls no apt, snap or ubuntu-tweak, so it runs unchanged on Debian (arm64 too) and
NixOS; it only needs `bash git curl sha256sum awk zsh vim python3` + PyYAML, which both modules below provide.
Login shell: zsh (the dotfiles are a zsh setup).

| Host kind | Module | Hosts |
|---|---|---|
| NixOS | [`nixos.nix`](nixos.nix): `homelab.dotfiles.enable = true` | `macmini` (later `odroid-n2plus`) |
| Debian / Armbian | [`debian/`](debian/) (a debian-tweaks module: `check` / `plan` / `install`) | `bananapi-m4` (as `modules/dotfiles`), ODROID-N2+ while on Armbian ([`debian/hosts/odroidn2-armbian.toml`](debian/hosts/odroidn2-armbian.toml)) |

Both bring `~/.files` to the pinned commit with one script, [`debian/checkout`](debian/checkout): a fresh clone
(checked out next to `~/.files` and moved into place only then), a fast-forward, or a move back over upstream commits
only. Local commits on top of the pin and another checked-out branch are left alone; upstream commits past the pin
mixed with local ones, or a diverged `master`, fail loudly. Stale submodules in detached HEAD get their URL synced
from `.gitmodules` and are updated; one checked out on a branch is left alone. Then `install-ubuntu.sh` runs: its
exit 0 or 10 (only manual steps remain, printed) counts as converged, anything else as a failure, and so does a run
longer than 15 minutes (NixOS: the whole unit; Debian: the installer).

- NixOS: `dotfiles.service` (oneshot with `RemainAfterExit`, after `network-online.target`: once per boot and on a
  switch that changes the unit; `SuccessExitStatus=10`, `TimeoutStartSec=15min`). Output: `journalctl -u dotfiles`.
  Re-run by hand: `sudo systemctl restart dotfiles`.
- Debian: `sudo ./install.sh --commit` in the host's debian-tweaks directory; `./install.sh check` is 0 once
  converged. ODROID-N2+ on Armbian (only this module), with its own host file installed as `machine.toml` there
  (bananapi-m4's would converge the ODROID into a bananapi; `install.sh` also refuses a `machine.toml` whose
  `[modules.system] hostname` is not the box's `hostname`, exit 2, unless `--force-host <current hostname>`):

  ```sh
  rsync -aL --delete --exclude /machine.toml machines/bananapi-m4/ ultra@odroidn2:debian-tweaks/   # -L: modules/dotfiles is a symlink
  rsync machines/dotfiles/debian/hosts/odroidn2-armbian.toml ultra@odroidn2:debian-tweaks/machine.toml
  ssh -t ultra@odroidn2 'cd debian-tweaks && sudo ./install.sh --only dotfiles --commit'
  ```

## The pin: which .files commit the hosts run

`install-ubuntu.sh` runs as `ultra`, who is root-equivalent (docker group, sudo/wheel), so the hosts never follow
`master` of .files: they run the commit in [`debian/rev`](debian/rev), one sha for every host (the NixOS module
reads it as the default of `homelab.dotfiles.rev`; the Debian module as the default of `[modules.dotfiles] rev`,
which a `machine.toml` may override). A push to .files reaches a host only once that file is bumped here, and moving
it back rolls the hosts back. Bump: resolve the new sha first, read exactly that range, then write that sha (never a
moving `master`, which could take a push made in between):

```sh
new=$(git ls-remote https://github.com/alex-mextner/.files.git refs/heads/master | cut -f1)
echo "https://github.com/alex-mextner/.files/compare/$(cat machines/dotfiles/debian/rev)...$new"   # read it
git -C ~/.files fetch -q origin && git -C ~/.files diff --submodule=log "$(cat machines/dotfiles/debian/rev)" "$new"   # and the submodule ranges it moves
echo "$new" > machines/dotfiles/debian/rev
```

then commit, and apply: `sudo nixos-rebuild switch --flake ./machines#<host>` (the unit changes, so it re-runs), the
`rsync` + `sudo ./install.sh --commit` above on Debian.

## Manual steps (once per machine, as `ultra`)

The installer reports these as `manual:` lines; no run of a module can do them.

1. Private submodule `vscode-configs` (and `repos/tg-cli` while `.gitmodules` still uses its ssh URL,
   [alex-mextner/.files#222](https://github.com/alex-mextner/.files/issues/222)): give the machine read-only GitHub
   access, then re-run. Not `gh auth login` in the browser: its token has the `repo` scope (read and write to every
   repository of the account) and would sit on every host. Either:
   - a fine-grained personal access token (github.com → Settings → Developer settings → Fine-grained tokens):
     resource owner `alex-mextner`, "Only select repositories": `vscode-configs`, permission Contents: Read-only,
     an expiry date. It also reads public repositories such as `tg-cli`. On the machine (paste the token, then
     Ctrl-D, so it stays out of the shell history):

     ```sh
     gh auth login --hostname github.com --git-protocol https --with-token
     gh auth setup-git            # git uses gh's credential helper; install-ubuntu.sh then fetches ssh URLs over https
     ~/.files/install-ubuntu.sh   # or: sudo systemctl restart dotfiles / sudo ./install.sh --commit
     ```

   - or a deploy key: read-only, one repository. Add the machine's `~/.ssh/id_ed25519.pub` (`ssh-keygen -t ed25519`
     if there is none) at github.com/alex-mextner/vscode-configs → Settings → Deploy keys with "Allow write access"
     off, then make git use ssh for that repository and https for the public `tg-cli`, and accept github.com's host
     key once (the modules run git with `BatchMode=yes`, so they never prompt):

     ```sh
     git config --global url.git@github.com:alex-mextner/vscode-configs.git.insteadOf https://github.com/alex-mextner/vscode-configs.git
     git config --global url.https://github.com/alex-mextner/tg-cli.git.insteadOf git@github.com:alex-mextner/tg-cli.git
     ssh -T git@github.com        # compare the fingerprint with docs.github.com, answer yes
     ~/.files/install-ubuntu.sh
     ```

2. bun (optional, for `ts-import-trace`): <https://bun.sh>, then re-run.
3. rig (optional): `install-ubuntu.sh` merges `~/.config/rig/config.yaml`; converging the machine to it is
   `rig apply commit` once the rig CLI is installed.
