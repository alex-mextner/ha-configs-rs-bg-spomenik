# Physical Ubuntu desktop in Home Assistant

The `Ubuntu Desktop` and `ChatGPT Native` sidebar panels now display the **existing logged-in GNOME desktop** on `home`. They no longer start a separate Openbox session or a separate ChatGPT profile. Opening/minimizing an application through the panel changes the real monitor too.

## Topology

HA runs on `odroidn2` (`100.116.57.66`). Its existing administrator-authenticated `/api/chatgpt_native/*` proxy reaches `home` (`100.74.26.95:6081`). The host bridge accepts only the configured Odroid source IP and connects to a mode-0600 Unix socket under `/run/user/1000/ha-physical-desktop`.

GNOME Desktop Sharing, FreeRDP and the VNC transport run together in a Bubblewrap network namespace. The RDP listener on 3390 and VNC listener on 5902 do **not** exist in the host network namespace, LAN or Tailnet. GNOME uses `mirror-primary`, not `--headless` or Remote Login. FreeRDP renders that live desktop into an Xvfb transport surface (`:110`), exported through noVNC. Xvfb is not a second user desktop: it has no window manager, app launcher or separate account/profile.

RDP uses a generated password stored privately in the desktop user's keyring and `~/.config/ha-physical-desktop/credentials.json` (0600). Passwords go through stdin, not process arguments or logs. The local RDP certificate is pinned by SHA-256. VNC has no second password because it is reachable only through the private Unix socket and the existing HA administrator proxy.

## Installation and restart

The desktop host needs GNOME Remote Desktop, Bubblewrap, FreeRDP X11, Xvfb, x11vnc, noVNC, xauth, xsetroot, OpenSSL and Python aiohttp. The existing extracted GUI runtime remains at `~/.local/share/chatgpt-native-ha/gui-runtime/root`; the new FreeRDP client is under `~/.local/share/ha-physical-desktop/runtime`.

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

With the pinned locked-desktop extension enabled, the real GNOME password screen remains accessible. Without that extension, GNOME closes Desktop Sharing on lock. Neither mode bypasses password authentication. A logged-in GNOME session is required.

RDP file-clipboard redirection stays disabled because its FUSE helper fails in this namespace. Cross-device text copying is implemented through the separate authenticated clipboard channel described below; the local Ubuntu clipboard is not disabled.

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

Rollback also requires restoring the old GNOME keyring login: its pre-install `grdctl status --show-credentials` snapshot is retained privately as `rdp-credentials.txt` (0600) in the installer backup. Use those saved credentials with `grdctl rdp set-credentials` or clear credentials if the snapshot reports none. Do not publish this snapshot.

## Clipboard and locked access (2026-10-16)

The HA panel's top **Буфер обмена** button opens a text field and explicit actions:
**Вставить с устройства → На Ubuntu**, and **С Ubuntu → Копировать на устройство**.
Use the normal paste command in the destination Ubuntu application. Unicode, emoji,
newlines, and up to 64 KiB of text are supported. When HTTP or browser permissions
prevent the Clipboard API, the same field supports ordinary manual paste/copy.
No clipboard is automatically read, transmitted, stored, or logged in the background.
Clipboard access is denied while the native desktop is locked, including to HA admins.

The RDP file clipboard remains disabled deliberately: GNOME 50 creates a FUSE clipboard
mount when that channel is enabled, which failed in this deployment's isolated namespace.
The text-only path uses `xclip` on the **real Xwayland display**, not the RDP framebuffer.
The existing private Tailnet peer restriction and per-request HA administrator checks
remain in place. No RDP/VNC port is opened on the host network.

Provision xclip using the distribution package (the same architecture as the desktop):

```sh
cd ~/.local/share/ha-physical-desktop/downloads
apt-get download xclip
for package in xclip_*.deb; do dpkg-deb -x "$package" ../runtime; done
cd /path/to/this/checkout
python3 scripts/physical-desktop/install.py --listen <desktop-tailnet-IP> --peer <HA-tailnet-IP>
python3 scripts/physical-desktop/install-lock-screen.py
```

Locked access uses the pinned `submodules/allow-locked-remote-desktop` extension.
It preserves GNOME's normal lock screen and password authentication. It does **not**
log out, unlock automatically, change the password, or disable the screen lock.
An unchanged known extension enables in place; a new or changed extension may require the next
normal graphical login, which the installer reports rather than forcing logout.
The first live installation can also use GNOME's official extension-install dialog.

To undo locked access, disable `allowlockedremotedesktop@kamens.us` using GNOME
Extensions. Previous extension files and the enabled-extension setting are backed up.
The native lock and desktop session remain intact. To undo the text bridge, restore
its saved `bridge.env` and `bridge.py` from the private host backup and restart only
`ha-physical-desktop-web.service`.

### Compact toolbar and remote shortcuts

The top strip is 28 px high. Its Tabler chevron collapses the strip completely,
leaving a small restore tab over the desktop. This changes layout only: the noVNC
iframe and its connection are retained. The Tabler clipboard icon opens the text
transfer drawer; collapsing also closes the drawer and clears its text preview.
The caret before C/V is a Tabler `chevron-up` SVG, not a text symbol or emoji.

The C/V shortcut buttons send Ctrl+C / Ctrl+V to the active Ubuntu application.
They do not read or overwrite the phone/browser clipboard. Transfer text first
using the existing drawer actions when copying between devices. Shortcut buttons
refuse disconnected or view-only sessions and release pressed modifiers even if
an input error occurs. The real-browser regression can be run from the repository
root with a Python environment containing Playwright:
`python tests/physical_desktop/check_toolbar_browser.py` (uses installed Chrome).
It checks desktop, portrait-phone and landscape-phone viewport sizes.
