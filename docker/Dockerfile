# Arena-in-a-box: headless OpenTTD + Nutz AI + libraries + bridge GameScript +
# the HERO MCP bridge, as one runnable image. An agent spins up a whole arena
# with `docker run`. Built for linux/amd64 (OpenTTD ships no arm64 generic
# build); runs under emulation on Apple Silicon.
FROM --platform=linux/amd64 debian:bookworm-slim

ARG OTTD_VER=15.3
ARG OPENGFX_VER=7.1
ENV DEBIAN_FRONTEND=noninteractive \
    OTTD_DIR=/arena \
    ADMIN_PW=nutzarena

# Runtime deps for the generic OpenTTD build + python for the bridge.
RUN apt-get update && apt-get install -y --no-install-recommends \
      ca-certificates curl xz-utils unzip python3 \
      liblzo2-2 libicu72 fontconfig libpng16-16 zlib1g libsdl2-2.0-0 \
      libgomp1 libglib2.0-0 libfluidsynth3 \
    && rm -rf /var/lib/apt/lists/*

# OpenTTD (pinned generic linux release).
RUN curl -fsSL "https://cdn.openttd.org/openttd-releases/${OTTD_VER}/openttd-${OTTD_VER}-linux-generic-amd64.tar.xz" \
      -o /tmp/ottd.tar.xz \
    && tar -xJf /tmp/ottd.tar.xz -C /opt \
    # Install the WHOLE tarball into the standard data dir, not just the
    # binary. lang/ ships beside the executable, and symlinking the binary
    # alone leaves OpenTTD unable to find it: it exits with "No available
    # language packs" before the admin port ever opens.
    && mkdir -p /usr/share/games/openttd \
    && cp -R "/opt/openttd-${OTTD_VER}-linux-generic-amd64/." /usr/share/games/openttd/ \
    && ln -sf /usr/share/games/openttd/openttd /usr/local/bin/openttd \
    && rm -rf /tmp/ottd.tar.xz "/opt/openttd-${OTTD_VER}-linux-generic-amd64"

# Prove every shared library the binary needs is actually present. Without
# this the first symptom of a missing dep is a container that boots, prints
# "Starting OpenTTD...", and serves a port nobody can reach - and each missing
# library only reveals itself one run at a time. ldd lists them all at once,
# at build time, where it is cheap to fix.
RUN missing="$(ldd /usr/local/bin/openttd | grep 'not found' || true)"; \
    if [ -n "$missing" ]; then \
      echo "FATAL: openttd is missing shared libraries:"; echo "$missing"; \
      exit 1; \
    fi; \
    openttd --help >/dev/null 2>&1 || true; \
    echo "all shared libraries resolve"

# Base graphics (headless server can't fetch them on its own).
RUN mkdir -p "${OTTD_DIR}/baseset" \
    && curl -fsSL "https://cdn.openttd.org/opengfx-releases/${OPENGFX_VER}/opengfx-${OPENGFX_VER}-all.zip" -o /tmp/g.zip \
    && unzip -oq /tmp/g.zip -d /tmp/g \
    && tar -xf /tmp/g/opengfx-*.tar -C "${OTTD_DIR}/baseset/" \
    && rm -rf /tmp/g /tmp/g.zip

# Arena code: AI + its libraries + the bridge generator + admin/MCP layer.
WORKDIR /app
COPY agent /app/agent
COPY ottd_user /app/ottd_user
COPY docker/entrypoint.sh /app/entrypoint.sh
COPY docker/smoke.py /app/docker/smoke.py
RUN chmod +x /app/entrypoint.sh \
    && mkdir -p "${OTTD_DIR}/ai" "${OTTD_DIR}/game" "${OTTD_DIR}/content_download/ai/library" \
    && ln -sfn /usr/share/games/openttd/lang "${OTTD_DIR}/lang" \
    && cp -R /app/ottd_user/ai/nutz_executor "${OTTD_DIR}/ai/" \
    && cp /app/ottd_user/ai/library/*.tar "${OTTD_DIR}/content_download/ai/library/" \
    # generate the bridge GameScript at build time (bare, no towns)
    && python3 - <<'PY'
import sys
from pathlib import Path
sys.path.insert(0, "/app/agent/sandbox")
import bridge_gs
bridge_gs.generate_bridge_with_towns([], "arena", out_dir=Path("/arena/game"))
print("bridge GS generated")
PY

# Game port (join/spectate), MCP bridge, dashboard. Admin port 3977 is the
# control plane and is deliberately NOT exposed.
EXPOSE 3979/tcp 3979/udp 8990/tcp 8080/tcp

ENV SERVER_NAME="Hero Arena" \
    PUBLIC=0 \
    MAX_COMPANIES=8 \
    MAX_CLIENTS=25 \
    START_MCP=1 \
    START_DASHBOARD=1

ENTRYPOINT ["/app/entrypoint.sh"]
