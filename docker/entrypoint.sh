#!/bin/sh
# Tubarr container entrypoint: refuses root, checks the folders, optionally loads a newer yt-dlp, then runs the command.
set -eu

# Never run as root: nothing here needs it, and the data folder holds the encryption key for stored credentials.
if [ "$(id -u)" = "0" ] && [ "${TUBARR_ALLOW_ROOT:-0}" != "1" ]; then
  echo "tubarr: refusing to run as root (uid 0). Run as an unprivileged user: set PUID/PGID in .env (docker compose" >&2
  echo "        uses user: \"\${PUID}:\${PGID}\") or pass --user 1000:1000 to docker run, and chown the data and library" >&2
  echo "        folders to that user. If you really must run as root, set TUBARR_ALLOW_ROOT=1 (not recommended)." >&2
  exit 1
fi

# umask 002 for the whole process: the library (/youtube) is shared with Plex and often with a media group, so new
# files and folders there must stay group-writable. umask is process-wide, so the data folder is kept private by its
# permissions instead: $DATA and its subfolders are chmod 700, which makes every file inside unreachable for other
# users whatever its own mode.
umask 002

DATA="${TUBARR_DATA:-/data}"
ROOT="${TUBARR_ROOT:-/youtube}"

for d in "$DATA" "$ROOT"; do
  if ! mkdir -p "$d" 2>/dev/null || [ ! -w "$d" ]; then
    echo "tubarr: $d is not writable by uid $(id -u):$(id -g). Fix the folder's owner on the host (chown -R PUID:PGID) or set PUID/PGID." >&2
    exit 1
  fi
done
mkdir -p "$DATA/cache" "$DATA/logs"
# Best effort: a folder owned by someone else (e.g. a root-owned bind mount the user can still write to) can't be chmodded.
if ! chmod 700 "$DATA" "$DATA/cache" "$DATA/logs" 2>/dev/null; then
  echo "tubarr: note: couldn't restrict $DATA to mode 700; make sure only uid $(id -u) can read it on the host." >&2
fi

# yt-dlp has to keep up with YouTube. The image ships a pinned, hash-verified yt-dlp. With TUBARR_YTDLP_AUTOUPDATE=1
# the newest yt-dlp release is fetched from PyPI at every start into the in-memory /tmp/pylib (never into the data
# volume, so nothing downloaded persists or survives a restart) and loaded ahead of the image's copy. That code is not
# hash-pinned: it trades reproducibility for keeping up with YouTube. Default 0: fully pinned, update by pulling a newer
# image. Only the service itself does this; one-off commands (hashpw, resetpw) don't.
if [ -d "$DATA/.pylib" ]; then
  echo "tubarr: note: $DATA/.pylib (from an older version) is no longer used and can be deleted." >&2
fi
PYLIB=/tmp/pylib
if [ "${TUBARR_YTDLP_AUTOUPDATE:-0}" = "1" ] && [ "${1:-}" = "sh" ]; then
  rm -rf "$PYLIB"
  if timeout 180 python -m pip install --isolated --only-binary=:all: --index-url https://pypi.org/simple --no-deps \
       --upgrade --target "$PYLIB" --quiet --no-cache-dir --disable-pip-version-check yt-dlp yt-dlp-ejs >/dev/null 2>&1; then
    # Keep only yt-dlp itself (yt_dlp, yt_dlp_ejs and their dist-info); drop scripts or anything else pip put there.
    for entry in "$PYLIB"/* "$PYLIB"/.[!.]*; do
      [ -e "$entry" ] || continue
      case "${entry##*/}" in
        yt_dlp|yt_dlp-*.dist-info|yt_dlp_ejs|yt_dlp_ejs-*.dist-info) ;;
        *) rm -rf "$entry" ;;
      esac
    done
    if [ -d "$PYLIB/yt_dlp" ]; then
      export PYTHONPATH="$PYLIB${PYTHONPATH:+:$PYTHONPATH}"
      echo "tubarr: yt-dlp $(python -m yt_dlp --version 2>/dev/null || echo '?') (auto-update, loaded from $PYLIB)"
    fi
  else
    rm -rf "$PYLIB"
    echo "tubarr: yt-dlp auto-update skipped (no network or PyPI unreachable); using the image's version" >&2
  fi
fi

exec "$@"
