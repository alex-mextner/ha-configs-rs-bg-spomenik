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

Both clone the public https URL (no GitHub auth needed), only fast-forward an existing checkout that is on
`master` (local commits or another checked-out branch are left alone; a diverged `master` fails loudly), check out
the submodule commits it records, then run `install-ubuntu.sh`. Its exit 10 (only manual steps remain) counts as
converged and the steps are printed.

- NixOS: `dotfiles.service` (oneshot with `RemainAfterExit`, after `network-online.target`: once per boot and on a
  switch that changes the unit). Output: `journalctl -u dotfiles`. Re-run by hand: `sudo systemctl restart dotfiles`.
- Debian: `sudo ./install.sh --commit` in the host's debian-tweaks directory; `./install.sh check` is 0 once
  converged. ODROID-N2+ on Armbian (only this module):

  ```sh
  rsync -aL --delete machines/bananapi-m4/ ultra@odroidn2:debian-tweaks/   # -L: modules/dotfiles is a symlink
  ssh -t ultra@odroidn2 'cd debian-tweaks && sudo ./install.sh --machine modules/dotfiles/hosts/odroidn2-armbian.toml --only dotfiles --commit'
  ```

## Manual steps (once per machine, as `ultra`)

The installer reports these as `manual:` lines; no run of a module can do them.

1. Private submodule `vscode-configs` (and `repos/tg-cli` while `.gitmodules` still uses its ssh URL,
   [alex-mextner/.files#222](https://github.com/alex-mextner/.files/issues/222)): give the machine GitHub access
   as `alex-mextner`, then re-run:

   ```sh
   gh auth login        # GitHub.com, HTTPS, "Authenticate Git with your GitHub credentials": yes
   ~/.files/install-ubuntu.sh   # or: sudo systemctl restart dotfiles / sudo ./install.sh --commit
   ```

   An SSH key on that account works too for ssh URLs (accept github.com's host key once with `ssh -T git@github.com`:
   the modules run git with `BatchMode=yes`, so they never prompt).
2. bun (optional, for `ts-import-trace`): <https://bun.sh>, then re-run.
3. rig (optional): `install-ubuntu.sh` merges `~/.config/rig/config.yaml`; converging the machine to it is
   `rig apply commit` once the rig CLI is installed.
