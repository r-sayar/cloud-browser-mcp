# cloud_agents — BrowserOS in a headed Linux container with noVNC live-view
# and the BrowserOS MCP exposed on :9200 for remote Claude Desktop clients.

FROM debian:bookworm-slim

ENV DEBIAN_FRONTEND=noninteractive \
    LANG=C.UTF-8 \
    LC_ALL=C.UTF-8 \
    DISPLAY=:10

# Runtime deps:
#  - xvfb / x11vnc / novnc / websockify  → headed display + browser-based VNC
#  - libnss3, libgtk-3, libgbm1, …       → Chromium runtime libs that AppImage doesn't bundle
#  - fonts                               → CJK + Latin so most pages render
#  - dbus-x11                            → BrowserOS expects a DBus session
RUN apt-get update && apt-get install -y --no-install-recommends \
        xvfb x11vnc novnc websockify socat jq xdotool \
        ca-certificates curl wget tini procps \
        dbus dbus-x11 \
        libnss3 libnspr4 libgbm1 libgtk-3-0 libxss1 libasound2 \
        libxcomposite1 libxdamage1 libxrandr2 libxshmfence1 \
        libdrm2 libxkbcommon0 libxfixes3 libpango-1.0-0 libpangocairo-1.0-0 \
        libcairo2 libcups2 libatspi2.0-0 libatk-bridge2.0-0 libatk1.0-0 \
        fonts-liberation fonts-noto-cjk fonts-noto-color-emoji \
    && rm -rf /var/lib/apt/lists/*

# Extract the AppImage at build time so we don't need FUSE in the container.
# (AppImage normally mounts via FUSE; --appimage-extract dumps a plain dir.)
WORKDIR /opt
COPY BrowserOS.AppImage /opt/BrowserOS.AppImage
RUN chmod +x /opt/BrowserOS.AppImage \
    && /opt/BrowserOS.AppImage --appimage-extract \
    && mv squashfs-root browseros \
    && rm /opt/BrowserOS.AppImage \
    && ls /opt/browseros/AppRun >/dev/null

# Symlink novnc's index.html so http://host:6080/ goes straight to the viewer.
RUN ln -sf /usr/share/novnc/vnc_lite.html /usr/share/novnc/index.html

COPY entrypoint.sh /entrypoint.sh
RUN chmod +x /entrypoint.sh

EXPOSE 9200 6080
VOLUME /data

# tini reaps zombies — Chromium spawns many subprocesses
ENTRYPOINT ["/usr/bin/tini", "--", "/entrypoint.sh"]
