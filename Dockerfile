# syntax=docker/dockerfile:1
# Tubarr: your YouTube subscriptions -> a Plex TV library. Multi-arch (linux/amd64, linux/arm64).
# Base images are pinned by multi-arch index digest; the tag is kept for readability. Written as literal FROM lines
# (not ARGs) so Dependabot can bump tag and digest together.

# Deno: the JavaScript runtime yt-dlp needs for YouTube's player challenges (EJS). Copied from the official image.
FROM denoland/deno:bin-2.4.5@sha256:4a0c035e554ee9961e40d2d20ff8ad05f273230c8a0290a7cbad214d5d26ab10 AS deno

FROM python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f
ARG TARGETARCH
# PYTHONNOUSERSITE: never import from ~/.local (HOME is the writable /data volume).
ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    HOME=/data XDG_CACHE_HOME=/data/cache DENO_DIR=/data/cache/deno TZ=UTC \
    TUBARR_WEB_PORT=9194 TUBARR_WEB_DIR=/app/web

RUN apt-get update && apt-get install -y --no-install-recommends \
        ffmpeg ca-certificates tzdata fontconfig fonts-noto-core tini \
    && rm -rf /var/lib/apt/lists/*

COPY --from=deno /deno /usr/local/bin/deno
RUN deno --version

# Poster fonts, vendored in docker/fonts/ (SIL Open Font License 1.1; license texts alongside). Taken from the Google
# Fonts repository at commit 9710da1eacb3be272583c3224dcb70f9da6eadbb (ofl/montserrat/Montserrat[wght].ttf,
# ofl/inter/Inter[opsz,wght].ttf, ofl/bebasneue/BebasNeue-Regular.ttf).
COPY docker/fonts/ /usr/local/share/fonts/tubarr/
RUN fc-cache -f >/dev/null

# Every package is pinned with its sha256 hashes (requirements.txt is generated from requirements.in), wheels only.
COPY requirements.txt /app/requirements.txt
RUN pip install --require-hashes --only-binary=:all: -r /app/requirements.txt \
    && yt-dlp --version \
    && python -c "import yt_dlp_ejs, PIL, argon2, cryptography, itsdangerous, fastapi, socks; print('deps ok')"

COPY docker/entrypoint.sh /usr/local/bin/tubarr-entrypoint
COPY web /app/web
COPY tubarr /app/tubarr
RUN chmod 755 /usr/local/bin/tubarr-entrypoint && python -m compileall -q /app/tubarr \
    && mkdir -p /data /youtube && chown 1000:1000 /data /youtube && chmod 700 /data

WORKDIR /app
# Runs as an unprivileged user (the entrypoint refuses uid 0 unless TUBARR_ALLOW_ROOT=1). docker-compose.yml
# overrides it with PUID:PGID (user: "${PUID}:${PGID}").
USER 1000:1000
EXPOSE 9194
HEALTHCHECK --interval=60s --timeout=10s --start-period=60s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:9194/health', timeout=8)" || exit 1

# tini -g: signals reach the whole process group (the web app loop and the worker).
ENTRYPOINT ["/usr/bin/tini", "-g", "--", "/usr/local/bin/tubarr-entrypoint"]
# The worker is the main process; the web app (UI + API) runs beside it and is restarted if it ever exits.
CMD ["sh", "-c", "while :; do python -m tubarr.webapp; sleep 3; done & exec python -m tubarr.worker"]

LABEL org.opencontainers.image.title="Tubarr" \
      org.opencontainers.image.description="Your YouTube subscriptions as a Plex TV library, downloaded at a human pace." \
      org.opencontainers.image.licenses="MIT"
