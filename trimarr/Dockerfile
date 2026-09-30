# Trimarr (optional add-on for Tubarr): trims SponsorBlock "sponsor" / "self-promotion" segments out of videos that
# are already in the library, keeping the original for undo. One small container: python-slim + ffmpeg, SQLite.
# Base image pinned by multi-arch index digest (tag kept for readability; Dependabot bumps both).
FROM python:3.12-slim-trixie@sha256:f77ac9e44ae96ef2c90b8053ea08c31f8be030f824196b0ae4db6d462c84e51f

ENV PYTHONUNBUFFERED=1 PYTHONDONTWRITEBYTECODE=1 PYTHONNOUSERSITE=1 PIP_NO_CACHE_DIR=1 PIP_DISABLE_PIP_VERSION_CHECK=1 \
    TZ=UTC HOME=/data

RUN apt-get update && apt-get install -y --no-install-recommends ffmpeg tini tzdata ca-certificates \
    && rm -rf /var/lib/apt/lists/* && ffmpeg -hide_banner -version | head -1

# Every package is pinned with its sha256 hashes (requirements.txt is generated from requirements.in), wheels only.
COPY requirements.txt /app/requirements.txt
RUN pip install --require-hashes --only-binary=:all: -r /app/requirements.txt \
    && python -c "import requests, numpy; print('ok')"

COPY trimarr /app/trimarr
RUN python -m compileall -q /app/trimarr
WORKDIR /app
USER 1000:1000
EXPOSE 8791
# /health needs no token and answers for Host 127.0.0.1.
HEALTHCHECK --interval=60s --timeout=10s --start-period=30s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8791/health', timeout=8)" || exit 1
# Everything in this container runs at low priority: CPU nice 19, I/O best-effort lowest (-c2 -n7). The real I/O
# brake is the read-rate cap (settings: io_limit_mb_s), because some filesystems ignore ionice.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/bin/ionice", "-c2", "-n7", "/usr/bin/nice", "-n19"]
CMD ["python", "-m", "trimarr", "serve"]
