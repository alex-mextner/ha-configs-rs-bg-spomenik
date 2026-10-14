#!/bin/sh
# X display for the headed browser, VNC on the container's loopback, noVNC (published on host
# loopback only by compose), then the bridge.
set -eu
rm -f /tmp/.X99-lock /tmp/.X11-unix/X99
Xvfb :99 -screen 0 1280x900x24 -nolisten tcp &
for _ in $(seq 1 50); do [ -S /tmp/.X11-unix/X99 ] && break; sleep 0.1; done
x11vnc -display :99 -localhost -rfbport 5900 -forever -shared -nopw -quiet -noxdamage >/tmp/x11vnc.log 2>&1 &
websockify --web /usr/share/novnc 0.0.0.0:6080 127.0.0.1:5900 >/tmp/novnc.log 2>&1 &
exec python /app/launcher.py
