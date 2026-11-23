#!/bin/sh
set -eu
if [ "$#" -eq 0 ]; then
    printf '%s\n' 'Missing original Music Assistant entrypoint' >&2
    exit 64
fi
/app/venv/bin/python /opt/music-assistant-reconnect/apply_guard.py \
    /app/venv/lib/python3.14/site-packages/hass_client/client.py \
    /app/venv/lib/python3.14/site-packages/music_assistant/providers/hass/__init__.py
/app/venv/bin/python /opt/music-assistant-reconnect/apply_intent_guard.py \
    /app/venv/lib/python3.14/site-packages/music_assistant
exec "$@"
