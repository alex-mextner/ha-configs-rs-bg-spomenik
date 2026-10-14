# ChatGPT web bridge: headed Chrome on a private X display, noVNC for the user's own sign-in,
# and an OpenAI-compatible API for OpenClaw. See README.md and RECOVERY.md.
FROM python:3.12-slim-bookworm
RUN apt-get update \
 && apt-get install -y --no-install-recommends tini xvfb x11vnc novnc websockify fonts-dejavu-core \
    fonts-noto-color-emoji \
 && rm -rf /var/lib/apt/lists/*
RUN pip install --no-cache-dir playwright==1.63.0 aiohttp==3.14.3 pytest==8.4.2 \
 && python -m playwright install --with-deps chrome \
 && rm -rf /var/lib/apt/lists/*
RUN useradd -m -u 1000 bridge && mkdir -p /data /tmp/.X11-unix && chown bridge:bridge /data && chmod 1777 /tmp/.X11-unix
COPY bridge.py entrypoint.sh launcher.py test_bridge.py test_launcher.py /app/
USER bridge
ENV DISPLAY=:99 CHATGPT_WEB_PROFILE=/data/profile PYTHONUNBUFFERED=1 PYTHONPATH=/opt/agent-tools/lib
EXPOSE 8787 6080
HEALTHCHECK --interval=60s --timeout=5s --start-period=60s \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8787/health', timeout=4)"
ENTRYPOINT ["tini", "--", "/app/entrypoint.sh"]
