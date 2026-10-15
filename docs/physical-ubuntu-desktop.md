# Physical Ubuntu desktop in Home Assistant

The `Ubuntu Desktop` and `ChatGPT Native` sidebar panels now display the **existing logged-in GNOME desktop** on `home`. They no longer start a separate Openbox session or a separate ChatGPT profile. Opening/minimizing an application through the panel changes the real monitor too.

## Topology

HA runs on `odroidn2` (`100.116.57.66`). Its existing administrator-authenticated `/api/chatgpt_native/*` proxy reaches `home` (`100.74.26.95:6081`). The host bridge accepts only the configured Odroid source IP and connects to a mode-0600 Unix socket under `/run/user/1000/ha-physical-desktop`.

GNOME Desktop Sharing, FreeRDP and the VNC transport run together in a Bubblewrap network namespace. The RDP listener on 3390 and VNC listener on 5902 do **not** exist in the host network namespace, LAN or Tailnet. GNOME uses `mirror-primary`, not `--headless` or Remote Login. FreeRDP renders that live desktop into an Xvfb transport surface (`:110`), exported through noVNC. Xvfb is not a second user desktop: it has no window manager, app launcher or separate account/profile.

RDP uses a generated password stored privately in the desktop user's keyring and `~/.config/ha-physical-desktop/credentials.json` (0600). Passwords go through stdin, not process arguments or logs. The local RDP certificate is pinned by SHA-256. VNC has no second password because it is reachable only through the private Unix socket and the existing HA administrator proxy.

## Installation and restart

The desktop host needs GNOME Remote Desktop, Bubblewrap, FreeRDP X11, Xvfb, x11vnc, noVNC and Python aiohttp. The existing extracted GUI runtime remains at `~/.local/share/chatgpt-native-ha/gui-runtime/root`; the new FreeRDP client is under `~/.local/share/ha-physical-desktop/runtime`.

To obtain the FreeRDP client without changing system packages:

```sh
mkdir -p ~/.local/share/ha-physical-desktop/{downloads,runtime}
cd ~/.local/share/ha-physical-desktop/downloads
apt-get download freerdp-x11
for package in freerdp-x11_*.deb; do
  dpkg-deb -x "$package" ../runtime
done
```

From the checked-out repository on the desktop host:

```sh
python3 scripts/physical-desktop/install.py --listen 100.74.26.95 --peer 100.116.57.66
```

The installer backs up previous settings, installs user services, preserves generated credentials on reruns and disables the superseded `chatgpt-native-ha` service when replacing port 6081. No HA restart is needed to switch the stream. HA panel JavaScript is a tracked static file; reload the HA page after deploying a changed panel script.

Services:

```sh
systemctl --user status gnome-remote-desktop ha-physical-desktop-web
systemctl --user restart gnome-remote-desktop
systemctl --user restart ha-physical-desktop-web
```

The GNOME service override lives in `~/.config/systemd/user/gnome-remote-desktop.service.d/ha-private-desktop.conf`. The installer does not change GDM, autologin, suspend settings, the user password, or the desktop's existing applications. Backend startup fails closed if the private network namespace is absent. noVNC reconnects after a transport restart.

## Limitations

Screen locking closes the GNOME Desktop Sharing connection. The transport blanks and reconnects after the actual local session is unlocked; it does not bypass the screen lock. A logged-in GNOME session is required.

RDP clipboard redirection is disabled: GNOME's file-clipboard FUSE helper aborts inside this unprivileged namespace. Keyboard and mouse control work; cross-device clipboard copying is not implemented by this transport. This does not disable the local Ubuntu clipboard.

GNOME's on-screen screen-sharing indicator is deliberately retained.

## Verification

```sh
PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 <test-python> -m pytest -p pytest_asyncio.plugin -q tests/physical_desktop
node tests/physical_desktop/test_panel.cjs
<python-with-aiohttp-and-pillow> scripts/physical-desktop/verify.py --url http://100.116.57.66:8123 --input-test
```

The live verification authenticates with the existing HA token, obtains the actual 1920x1080 framebuffer through HA's WebSocket, sends Super to open GNOME's overview, captures the result, then Escape to restore the previously focused window. Screenshots are atomically written with mode 0600 under `~/.cache/ha-physical-desktop/verification` by default, never committed. Also verify anonymous HA access is 401, unrelated backend peers are 403, and host `ss -ltn` shows no 3390/5902 listeners.

## Rollback

Disable `ha-physical-desktop-web`, stop `gnome-remote-desktop`, restore the saved service/settings from `~/.local/share/ha-physical-desktop/backups`, and daemon-reload the user manager. The old separate desktop service remains installed but disabled; re-enable it only when explicitly restoring the old behavior. Never delete existing user profiles or runtime backups.

Child process output goes to capped systemd journald rather than append-only files in `/run/user`. Repeated connection failures back off from 5 seconds to at most 30 seconds and reset after a stable connection. The VNC transport uses X DAMAGE change detection.
