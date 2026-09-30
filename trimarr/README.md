# Trimarr (optional add-on for Tubarr)

Trimarr removes SponsorBlock-labelled **sponsor** and **self-promotion** segments from YouTube videos that are
already in your Tubarr library. It works in place: the file keeps its path and name, so the Plex item and its watch
state stay. The captions (`.en.srt`) and chapters are shifted to match.

It's a separate small container (python-slim + ffmpeg, SQLite for state, an HTTP API in the same process). It never
changes Tubarr's code, database or `.staging` folder, and it ships **off**: turn it on in Tubarr, **Settings → Trim ads**.

## Run it

```bash
# in Tubarr's .env (both containers read it):
#   TRIMARR_URL=http://trimarr:8791
#   TRIMARR_TOKEN=<a long random secret, e.g. from: openssl rand -hex 32>
mkdir -p trimarr-data && sudo chown 1000:1000 trimarr-data     # match PUID:PGID
docker compose --profile trimarr up -d --build
```

Trimarr has **no published port and no users of its own**. You use it through Tubarr's signed-in web UI, which
talks to it over the internal Docker network. Don't publish port 8791.

Every request to Trimarr's API except `GET /health` must carry the shared secret `X-Trimarr-Token`, which is
`TRIMARR_TOKEN` (at least 16 characters, the same value for Tubarr and Trimarr). If `TRIMARR_TOKEN` is missing or
too short, Trimarr refuses every API request (HTTP 503) and logs why: it fails closed. It also only answers to the
host names `trimarr`, `localhost` and `127.0.0.1` (a DNS-rebinding defense); if you run it under another name, add
it to `TRIMARR_ALLOWED_HOSTS` (comma-separated). There are no CORS headers: browsers never call Trimarr directly.

**Plex:** Trimarr has no Plex access at all (no token, no `Preferences.xml`). After a trim or an undo, Tubarr, which
already has its own Plex connection, asks Plex for a partial scan of that folder and an analyze of the episode,
within a minute or two.

## What it cuts, and when

- **Data:** SponsorBlock's crowd-sourced segments (the same data as the browser extension), fetched through the
  privacy-preserving hash-prefix endpoint.
- **Categories:** `sponsor` and `selfpromo` by default. Like/subscribe reminders (`interaction`), outros and others
  can be added. Intros are never cut.
- **Trust:** only segments that are **locked** by a SponsorBlock VIP, or that have at least `min_votes` (1) net
  votes **and** are at least `min_segment_age_hours` (24) old. Brand-new, unvoted submissions are where mistakes and
  vandalism live, so they are never cut. (When SponsorBlock doesn't send a submission time, the age counts from when
  Trimarr first saw the segment.)
- **Filters:** only `actionType` "skip"; segments shorter than 1 s are ignored; segments submitted for a
  different-length version of the video are ignored (their times would be wrong).
- **Sanity limit:** a video that would lose more than 40% is marked `suspicious` and left alone.
- **Timing:** only uploads at least 24 h old (segments appear hours to days after upload) and files at least
  30 minutes old. Untrimmed videos are re-checked daily.

## How a trim works (one video at a time, at low priority)

1. **Plan.** Each removed range is widened outward to keyframes, so no ad audio or video is left. Less than one
   keyframe interval of real content is lost per edge (about 2 s on average; each report shows `extra_seconds`).
2. **Cut.** One ffmpeg stream copy (no re-encode). The read rate is capped (`io_limit_mb_s`).
3. **Verify** before anything is replaced: the duration matches, the streams are the same, the start, the end and
   both sides of every join decode cleanly, and the audio at every join matches the original. If any check fails,
   the original stays.
4. **Captions and chapters** are shifted through the cuts.
5. **Swap.** The original is hard-linked into `.trim-originals/` and the new file is renamed over the library path
   (atomic). It never swaps a file that changed during the trim.
6. **Plex** (if Tubarr is connected to it): Tubarr asks for a partial scan and an analyze of the same item, so the
   watch state stays. The new file's length was already confirmed locally with ffprobe in step 3.
7. **Originals** are kept `keep_originals_days` (7) for undo, capped at `max_originals_gb`, then deleted.

## Commands

```bash
docker compose run --rm trimarr python -m trimarr dry-run                 # what it WOULD cut
docker compose run --rm trimarr python -m trimarr trim <videoID|file> [--force] [--plan]
docker compose run --rm trimarr python -m trimarr undo <videoID|file>
docker compose run --rm trimarr python -m trimarr approve <videoID|file>  # or --all: delete kept originals now
docker compose run --rm trimarr python -m trimarr settings enabled=true categories='["sponsor","selfpromo"]'
docker compose run --rm trimarr python -m trimarr channel <channel id|folder> on|off|default
docker compose run --rm trimarr python -m trimarr status
```

## Settings

`enabled`, `paused`, `channel_default`, `channels`, `categories`, `min_segment_seconds` (1), `min_votes` (1),
`min_segment_age_hours` (24), `max_removed_fraction` (0.40), `min_upload_age_hours` (24), `min_file_age_minutes`
(30), `recheck_hours` (24), `pass_interval_hours` (24), `keep_originals_days` (7), `max_originals_gb`,
`io_limit_mb_s` (40). The safety rules are floors: settings can only make them stricter. An existing
`settings.json` keeps the values it already has (change `keep_originals_days` there if it still says 3).

## Known limits

- **Stream copy means keyframe precision:** a couple of seconds of real content next to each ad are lost; that's the
  price of never re-encoding.
- A trimmed video isn't re-cut when new segments are submitted later (undo, then trim, applies them).
- Plex's resume position for a half-watched video isn't shifted.
- Trimarr can't see who is watching in Plex. A stream that is playing while a file is swapped keeps reading the old
  file (it stays open) until the player reloads it.

## Tests

```bash
docker build -t trimarr:test ./trimarr
docker run --rm --network none -v "$PWD/trimarr/tests:/app/tests:ro" --entrypoint python trimarr:test   -m unittest discover -s tests -v
```
