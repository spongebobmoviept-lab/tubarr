# Tubarr web API (v1)

The JSON API behind Tubarr's web UI. Scripts can read it with a read-only API key (Settings -> Account).

The contract between the web UI (`tubarr/app/web/`) and the backend. The backend implements exactly this. The mock
in `web/mock/mock-server.js` implements it too (including the event stream and the fill-and-roll model), so it is a
working reference for every shape below: run the UI with `?mock=1` and watch what it sends and expects.

Contents: [The real server](#the-real-server-v020) · [Conventions](#conventions) · [Live updates](#live-updates-server-sent-events) ·
[How space is used](#how-space-is-used-fill-and-roll) · [States](#states) · [Objects](#objects) ·
[Endpoints](#endpoints) · [Topics and series](#topics-and-series-plex-organization) ·
[Trimarr (ad trimmer)](#trimarr-ad-trimmer-proxied) · [Serving the UI](#serving-the-ui)

## Conventions

| Topic | Rule |
|---|---|
| Base | Same origin as the UI. JSON endpoints live under `/api/`. The backend serves `web/` as static files at `/`. |
| Auth | Every `/api/` route except sign-in needs the admin session cookie (plus `X-CSRF-Token` on changes) or the read-only `X-Api-Key` header. See [Sign-in and accounts](#sign-in-and-accounts). The API never returns Plex tokens, proxy credentials or passwords. |
| Bodies | `Content-Type: application/json`, UTF-8, unless an endpoint says `multipart/form-data`. Unknown request fields are ignored. |
| Times | ISO 8601 UTC with `Z`, e.g. `"2025-06-26T13:05:00Z"`. |
| Dates | `"YYYY-MM-DD"` in the configured time zone (`TZ`), e.g. `upload_date` (the date the episode number uses). |
| Sizes | Integers in **bytes**. The UI formats them 1024-based, labelled GB/TB, like `df -h` (a 3 TiB disk shows as "3.0 TB"). |
| Durations | Integers in **seconds**. Speeds in **bytes per second**. Progress is a float `0..1`. |
| IDs | Channel `id` = YouTube channel ID (`UC…`, 24 chars). Video `id` = YouTube video ID (11 chars). |
| Images | Every `*_url` image field is a same-origin URL (or a `data:` URL) or `null`. `null` = no art yet: the UI draws a generated placeholder. Put a version in the URL (`?v=<mtime>`) so edits show up at once. |
| Nulls | A documented field is always present; `null` means unknown / not applicable. |
| Errors | Any non-2xx response has the body `{"error": {"code": "not_found", "message": "Channel not found."}}`. `message` is one plain sentence the UI shows as-is. |
| Background work | Actions that start work return `202` with `{"ok": true, "message": "…"}` (the UI toasts `message`). Results arrive as events. |

Error codes: `bad_request` (400), `not_found` (404), `conflict` (409), `too_large` (413), `youtube_unreachable` (502),
`not_signed_in` (409), `plex_unreachable` (502), `internal` (500), `not_implemented` (501: "Coming soon: …", shown as an
info note).

## Live updates (Server-Sent Events)

**The UI is live: nothing needs a page refresh.** It opens one stream and patches the screen as events arrive: new
videos slide into the Timeline, backlog videos appear as they fill in, progress bars move, storage ticks up, removals
show up with their reason, sync badges change.

`GET /api/events` → `Content-Type: text/event-stream`. Keep the connection open, send `Cache-Control: no-cache`, and
disable proxy buffering. Each message is:
```
id: 18422
event: video.state
data: {"video": { …Video… }}

```
| `event` | `data` | When |
|---|---|---|
| `hello` | `{"version": "0.2.0", "server_time": "…"}` | First message after connecting. |
| `status` | the full [Status](#status-top-bar-sidebar-dashboard-strip) object | Right after `hello`, then whenever any field changes, at most once per second. |
| `video.added` | `{"video": Video}` | Tubarr listed a video: a new upload found by a check, a premiere scheduled, a backlog video queued by the fill, a video brought back by a retention change. |
| `video.state` | `{"video": Video}` | Anything about a listed video changed: state, stage, edits, `keep`, size, error, removal. |
| `video.progress` | `{"id", "stage", "progress", "bytes_done", "bytes_total", "speed_bps", "eta_seconds"}` | The running job, at most once per second. Cheap: no full object. |
| `video.removed` | `{"id", "channel_id"}` | A video left Tubarr's list entirely (its channel was deleted). A video that leaves Plex but stays listed is a `video.state` with `state: "removed"`. |
| `channel.added` | `{"channel": ChannelSummary}` | A channel was added (by hand, import, or subscription sync). |
| `channel.updated` | `{"channel": ChannelSummary}` | Any ChannelSummary field changed (status, counts, size, poster, title, removal countdown started/undone). |
| `channel.removed` | `{"id"}` | A channel was deleted for good (after its grace period, or immediately). |
| `storage` | the `storage` object from Status (usage + fill state) | Usage or fill state changed (a file landed, the roll removed one). |
| `sync` | `{"youtube": YouTubeConnection}` | The subscription sync started or finished, or the connection state changed. |
| `history` | `{"item": HistoryItem}` | A new Activity history line. |
| `ping` | `{}` | Every 15 s, to keep proxies from closing the stream. |

- `id:` is a monotonically increasing integer. Honouring `Last-Event-ID` on reconnect is optional: after any
  reconnect the UI refetches the open screen, so a gap is harmless.
- While the stream is down the UI polls `GET /api/status` every 2 s (15 s when the tab is hidden). That is only a fallback.

## How space is used: fill and roll

This is the **default for every channel** . Per-channel settings are overrides on top of it.

1. **New uploads always come first.** A new upload is downloaded as soon as the pacing allows.
2. **The backlog fills round-robin across all channels, going back in time**, until the library reaches the
   **fill target** (`fill_target_bytes`, default 2.9 TB of the 3 TB quota). Backlog downloads run in the overnight
   window. The point the fill has reached is the **reach date**: roughly, everything uploaded since then is in Plex.
3. **Once full, it rolls.** Each new video pushes out, in order: **watched videos, oldest watched first**, then **the
   oldest backlog video from the biggest channel**.
4. **Never removed automatically:** videos you marked "Keep forever" (`keep`), everything from channels set to
   Keep forever, **each channel's newest 3** (`protect_newest`), and **anything that has disappeared from YouTube**
   (deleted, made private, or its channel terminated: see `youtube_gone`). Only "Delete now" removes those.
5. **New uploads arrive within minutes.** Channel feeds are checked every `check_interval_minutes` (default 5), so a
   new upload normally lands in Plex a few minutes after it's published (`status.feed` reports how long it usually takes).

Per-channel overrides (`retention`, see [Retention](#retention-per-channel-overrides)): only the last N videos, only
the last N days, only since a date, keep forever (whole back catalog, never rolled out), or new uploads only (no
backlog). A channel with an override still counts toward the target.

## States

### Channel `status`
| Value | Badge | Meaning |
|---|---|---|
| `monitored` | Monitored | Normal: checked every `check_interval_minutes`; new uploads get queued. |
| `syncing` | Syncing | First scan after being added, or a check in progress. `sync_progress` may say how far. |
| `pending_removal` | Pending removal | Deleted at `removal.delete_at` (3-day grace). Undo = `POST /api/channels/{id}/restore`. Nothing new downloads meanwhile. |
| `error` | Error | The last check failed. `status_detail` says why in one sentence. Tubarr keeps retrying on its normal schedule. |
| `gone` | Gone from YouTube | The channel itself no longer exists on YouTube (terminated or deleted). Nothing new can arrive; everything already downloaded is **kept forever** and never rolled out. `gone` says when and why. It can still be removed by hand. |

`removal.reason`: `unsubscribed` (it left the YouTube subscriptions) or `manual` (Remove channel was pressed).

### Video `state`
The Timeline chip text is in the middle column.

| Value | UI chip | Notes |
|---|---|---|
| `downloaded` | **In Plex** | `size_bytes`, `resolution`, `sponsorblock_cut_seconds`, `watched`, `protected` |
| `downloading` | **Downloading 42%** / Cutting sponsors / Building artwork / Adding to Plex | `stage`, `progress` |
| `queued` | **Queued** | `size_bytes` = estimate (duration × bitrate) or `null` |
| `waiting_sponsorblock` | **Waiting for SponsorBlock** | New upload held back until community segments exist, at most `sponsorblock_wait_hours`. `wait_until` = when Tubarr stops waiting. |
| `upcoming` | **Premieres in 5h** | A scheduled premiere or stream; `published_at` = scheduled start. Queued automatically once available. |
| `skipped_short` | **Skipped (Short)** | Always skipped; can never be downloaded. |
| `skipped_live` | **Skipped (livestream)** | A stream replay while `include_live` is off. "Download anyway" works. |
| `skipped_too_long` | **Skipped (too long)** | Longer than `max_duration_minutes`. "Download anyway" works. |
| `failed` | **Failed** + reason + Retry | `error` object below. |
| `removed` | **Removed · watched** / Removed · making room / Removed · outside window / Removed · deleted | `removed_reason`, `removed_at`. "Download again" works. |

`stage` (while `downloading`), in order: `download` → `sponsorblock` → `artwork` → `plex`. UI labels: Downloading,
Cutting sponsors, Building artwork, Adding to Plex. `progress` = progress of the current stage (bytes for `download`);
`null` = indeterminate.

`removed_reason`:
| Value | Meaning |
|---|---|
| `watched` | The roll removed it: watched videos go first, oldest watched first. |
| `making_room` | The roll removed it: the oldest backlog video from the biggest channel. |
| `retention` | It fell outside the channel's own override (last N videos / days / since a date). |
| `manual` | Deleted in the UI ("Delete now", or "Don't download" on a queued one). |
| `unavailable` | Removed from YouTube before Tubarr could keep it. |

`protected` (on `downloaded` videos): `null`, or why the roll will never remove it: `gone` (it disappeared from
YouTube, see `youtube_gone`), `keep` (your "Keep forever"), `channel` (the channel is set to Keep forever or is
gone), `newest` (one of the channel's newest `protect_newest`). If several apply, the first in that order.

`youtube_gone` (on any listed video): `null`, or `{"at": "2025-06-20T14:00:00Z", "reason": "deleted"}` when the video
disappeared from YouTube after Tubarr listed it. `reason`: `deleted` (removed by the uploader), `private` (made
private), `terminated` (the whole channel is gone), `unavailable` (anything else, e.g. region-blocked or a copyright
claim). A downloaded one stays in Plex forever: the UI shows **"Removed from YouTube · kept"** with the date and reason.
(A video that vanished *before* it was downloaded is `removed` with `removed_reason: "unavailable"` instead.)

`error` on `failed` videos:
```json
{ "code": "http_403", "message": "YouTube refused the download (HTTP 403) three times.", "attempts": 3,
  "last_attempt_at": "2025-06-26T10:12:00Z", "next_retry_at": null, "retryable": true }
```
`code`: `members_only`, `age_restricted`, `unavailable`, `geo_blocked`, `http_403`, `network`, `ffmpeg`, `sponsorblock`,
`disk_full`, `plex`, `unknown` (any other string is shown generically). `retryable: false` hides Retry.

### Retention (per-channel overrides)
`settings.retention` on a channel. `null` = the default, fill and roll.
```json
null                                     // Default: fill and roll
{ "mode": "count", "count": 25 }         // Only the newest 25 videos
{ "mode": "days", "days": 90 }           // Only the last 90 days
{ "mode": "since", "since": "2025-01-01" } // Only videos uploaded on or after a date
{ "mode": "forever" }                    // Keep forever: the whole back catalog, never rolled out
{ "mode": "new_only" }                   // New uploads only: no backlog (new ones still roll like the default)
```
Counts and windows only include videos that pass the other rules (not Shorts, not skipped streams, not over the
length limit). Videos that fall outside a `count`/`days`/`since` window leave Plex with `removed_reason: "retention"`,
unless protected. In `effective_settings`, a default channel shows `{"mode": "fill"}`.

### Fill state
`storage.fill` in Status and Storage:
```json
{ "state": "filling", "target_bytes": 1900000000000, "reach_date": "2025-02-18",
  "last_removed": null, "next_removal": { "video": Video, "reason": "watched" } }
```
- `state`: `filling` (below the target, the backlog is still going back in time) or `rolling` (at the target: each
  arrival pushes the next removal out).
- `reach_date`: how far back the fill has got (roughly everything uploaded since then is in Plex); `null` if unknown.
- `last_removed`: `null` or `{"video": Video, "reason": "watched" | "making_room", "at": "…"}`: the roll's latest removal.
- `next_removal`: `null` or `{"video": Video, "reason": "watched" | "making_room"}`: what goes first when room is
  needed. While `filling`, it's what *would* go first once full.

### Other enums
- Downloader `state`: `downloading` · `idle` (queue empty) · `paused` · `waiting` (everything left waits for the overnight window).
- Queue item `reason`: `new_upload` · `backfill` · `retry` · `manual`. `waits_for`: `null` · `overnight` · `paused` · `sponsorblock`.
- YouTube connection `state`: `not_connected` · `signing_in` · `connected` · `needs_signin`.
- Plex server `state`: `connected` · `unreachable` · `unauthorized`. Library `state`: `ready` · `scanning` · `missing`.
- SponsorBlock categories offered for cutting: `sponsor` (Sponsor), `selfpromo` (Self-promotion), `interaction`
  (Like/subscribe reminders), `outro` (End cards), `preview` (Previews/recaps), `filler` (Tangents), `music_offtopic`
  (Non-music). Defaults: `sponsor`, `selfpromo`, `interaction`. **Intros are never cut** (`intro` is not accepted);
  per the backend design they are kept as chapters.

## Objects

### ChannelSummary (cards, table rows, events)
```json
{
  "id": "UCxxxxxxxxxxxxxxxxxxxxx1",
  "title": "Field Notes Science",
  "handle": "@FieldNotesScience",
  "url": "https://www.youtube.com/@FieldNotesScience",
  "subscribers": 1234000,
  "status": "monitored",
  "status_detail": null,
  "removal": null,
  "sync_progress": null,
  "video_count": 50,
  "size_bytes": 23875629056,
  "queued_count": 1,
  "failed_count": 0,
  "retention_mode": "fill",
  "keep_forever": false,
  "gone": null,
  "topic": "science",
  "topic_source": "auto",
  "subscribed": true,
  "added_at": "2025-06-09T02:10:00Z",
  "last_checked_at": "2025-06-26T12:40:00Z",
  "last_upload_at": "2025-06-24T15:00:00Z",
  "last_download_at": "2025-06-24T16:02:00Z",
  "poster_url": "/api/channels/UCxxxxxxxxxxxxxxxxxxxxx1/poster.jpg?v=1727350000"
}
```
- `title`: the display name (your edit if there is one, else YouTube's).
- `video_count` / `size_bytes`: videos in Plex (on disk).
- `retention_mode`: the effective mode (`fill`, `count`, `days`, `since`, `forever`, `new_only`). `keep_forever` = `retention_mode == "forever"`.
- `removal`: `null`, or `{"reason": "unsubscribed", "requested_at": "…", "delete_at": "…"}`.
- `gone`: `null`, or `{"at": "…", "reason": "terminated" | "deleted"}` when `status` is `gone`.
- `topic`: one [topic id](#topics-and-series-plex-organization); `topic_source`: `auto` (Tubarr's guess) or `user` (chosen by hand).
- `sync_progress`: `null`, or `{"done": 120, "total": 480}` while `syncing` (either may be `null`).
- `last_download_at`: newest file that landed in Plex (the "Recently updated" sort).
- `subscribed`: still in the YouTube subscriptions list (a hand-added channel can be `false` and still monitored).

### ChannelDetail (`GET /api/channels/{id}`)
Everything in ChannelSummary, plus:
```json
{
  "youtube_title": "Field Notes Science",
  "about": "YouTube's About text (plain text, may have newlines).",
  "summary": "The show summary Plex shows (your edit, or the About text).",
  "genre": "Science & Technology",
  "locked_fields": ["summary"],
  "banner_url": "/api/channels/UC…/banner.jpg?v=…",
  "youtube_video_count": 412,
  "plex_url": "http://plex.local:32400/web/index.html#!/server/<machineId>/details?key=%2Flibrary%2Fmetadata%2F12345",
  "kept_while_unsubscribed": false,
  "settings": {
    "retention": null, "max_duration_minutes": null, "quality": null, "include_live": null, "sponsorblock": null
  },
  "effective_settings": {
    "retention": { "mode": "fill" }, "max_duration_minutes": 180, "quality": "1080p",
    "include_live": false, "sponsorblock": ["sponsor", "selfpromo", "interaction"]
  },
  "stats": {
    "watched_count": 12,
    "skipped_count": 9,
    "kept_count": 2,
    "protected_count": 5,
    "sponsorblock_saved_seconds": 5820,
    "oldest_upload_date": "2025-03-02",
    "newest_upload_date": "2025-06-24"
  }
}
```
- `locked_fields`: which of `title`, `summary`, `genre`, `poster` you edited. Edits are pushed to Plex and
  locked there, so Plex never overwrites them. Reverting a field (PATCH it to `null`) unlocks it.
- **Settings inheritance:** in `settings`, `null` = use the default (Settings → Downloads). `effective_settings`
  is what actually applies.

| Setting | Type | Values the UI offers |
|---|---|---|
| `retention` | override or `null` | see [Retention](#retention-per-channel-overrides) |
| `max_duration_minutes` | int or `null` | `0` = no limit; 20, 30, 60, 90, 120, 180 |
| `quality` | `"1080p"` / `"720p"` / `null` | |
| `include_live` | bool or `null` | livestream replays; default off |
| `sponsorblock` | array or `null` | categories to cut; `[]` = cut nothing |

### Video (the one video shape: Timeline, channel page, activity, history, storage)
```json
{
  "id": "aB3dE5fG7hI",
  "channel_id": "UCxxxxxxxxxxxxxxxxxxxxx1",
  "channel_title": "Field Notes Science",
  "title": "Why Lightning Takes the Weirdest Path",
  "upload_date": "2025-06-24",
  "published_at": "2025-06-24T15:00:00Z",
  "added_at": "2025-06-24T15:22:00Z",
  "activity_at": "2025-06-24T15:31:00Z",
  "backfill": false,
  "episode": "S2025E062401",
  "duration_seconds": 1342,
  "state": "downloaded",
  "stage": null,
  "progress": null,
  "wait_until": null,
  "size_bytes": 512753664,
  "resolution": "1080p",
  "sponsorblock_cut_seconds": 102,
  "trim": null,
  "watched": false,
  "keep": false,
  "protected": "newest",
  "youtube_gone": null,
  "series": null,
  "edited": false,
  "error": null,
  "removed_reason": null,
  "removed_at": null,
  "thumbnail_url": "/api/videos/aB3dE5fG7hI/thumb.jpg?v=…",
  "youtube_url": "https://www.youtube.com/watch?v=aB3dE5fG7hI",
  "plex_url": null
}
```
- `title`: the display title (your edit if any). `edited`: any field was edited (the UI shows a lock).
- `added_at`: when Tubarr first listed the video. `activity_at`: the video's latest milestone, the Timeline's default
  order: listed (found or queued by the fill) → landed in Plex → failed → removed → disappeared from YouTube. So a
  backlog video that lands tonight shows at the top tomorrow morning, and a removal (or a video vanishing from
  YouTube) shows at the top with its reason.
- `backfill`: `true` for backlog (an older upload fetched by the fill or a window), `false` for a new upload.
- `episode`: the Plex code (`naming.episode_code`), `null` until assigned (Shorts never get one).
- `size_bytes`: on disk for `downloaded`; the duration × bitrate estimate for `queued`/`waiting_sponsorblock`/`downloading`; `null` otherwise.
- `sponsorblock_cut_seconds`: seconds removed; `0` = checked, nothing cut; `null` = not processed yet.
  The UI shows "SponsorBlock cut 1:42" when it's > 0.
- `series`: `null`, or `{"id": "PLx…", "title": "Harbor Lines: Season 1", "part": 4}` when the video belongs to a playlist
  that is shown as a series in Plex (`enabled` series only). The Timeline shows a chip "Harbor Lines: Season 1 · Part 4".
- `trim`: `null`, or `{"state": "trimmed" | "queued", "removed_seconds": 102}` when the optional Trimarr add-on has
  trimmed (or is about to trim) this file after it landed. Filled from Trimarr; always `null` when it isn't installed.
  The UI shows "Trimmed −1:42".

### VideoDetail (`GET /api/videos/{id}`)
Video, plus `{"youtube_title": "…", "summary": "…", "description": "YouTube's description", "locked_fields": ["title"]}`.
`summary` is the episode summary Plex shows (your edit, or the description).

## Endpoints

### Status (top bar, sidebar, dashboard strip)
`GET /api/status` (also pushed as the `status` event)
```json
{
  "version": "0.2.0",
  "server_time": "2025-06-26T13:05:00Z",
  "timezone": "UTC",
  "storage": {
    "used_bytes": 1200000000000, "cap_bytes": 2000000000000, "target_bytes": 1900000000000,
    "fill": { "state": "filling", "target_bytes": 1900000000000, "reach_date": "2025-02-18", "last_removed": null,
              "next_removal": { "video": Video, "reason": "watched" } }
  },
  "youtube": null,
  "downloader": {
    "state": "downloading",
    "paused": false,
    "current": { "video_id": "aB3dE5fG7hI", "channel_id": "UC…", "channel_title": "Slow Train Journal", "title": "…",
                 "thumbnail_url": null, "stage": "download", "progress": 0.42, "speed_bps": 6082000, "eta_seconds": 97 },
    "queue_count": 14,
    "waiting_count": 9,
    "failed_count": 3,
    "overnight": { "active": false, "start": "01:00", "end": "07:00" }
  },
  "today": { "added": 23, "bytes": 9876543210, "backfill": 310, "removed": 0 },
  "feed": { "interval_minutes": 5, "last_check_at": "2025-06-26T13:03:00Z", "next_check_at": "2025-06-26T13:08:00Z",
            "typical_minutes_to_plex": 9 },
  "counts": { "channels": 118, "videos": 3902, "pending_removal": 2, "channel_errors": 1, "gone_channels": 1, "gone_videos": 9 }
}
```
- `storage.target_bytes` = the fill target. `waiting_count` = queue items waiting for the overnight window.
- `today` (since local midnight): `added` = new uploads that landed in Plex, `bytes` = their size, `backfill` = backlog
  videos that landed, `removed` = videos the roll or a window removed.
- `feed`: the new-upload check. `last_check_at` = when every channel's feed was last read (the rolling check);
  `typical_minutes_to_plex` = median minutes from publish time to "in Plex" over the last 7 days of new uploads
  (`null` until there's data). The dashboard shows "Checking every 5 min · last check 1:03 PM · usually in Plex ~9 min".

### Timeline (the home screen)
`GET /api/timeline` → `{"videos": [Video], "next": "<cursor>" or null, "total": 812}`

| Query | Meaning |
|---|---|
| `limit` | page size, default 60, max 200 |
| `cursor` | the `next` value from the previous page |
| `sort` | `activity` (default: by `activity_at`, newest first) or `published` (by upload time, newest first) |
| `channel` | a channel ID (repeat the parameter for several) |
| `state` | comma list of raw states, e.g. `downloaded` or `queued,downloading,waiting_sponsorblock` |
| `kept` | `1` = only videos with `keep: true` |
| `gone` | `1` = only videos with `youtube_gone` set (removed from YouTube, kept in Plex) |
| `backfill` | `1` = only backlog videos, `0` = only new uploads |
| `from`, `to` | inclusive `YYYY-MM-DD` bounds on the sort date (the local date of `activity_at`, or `upload_date`) |
| `q` | case-insensitive text match on title and channel title |

Every video Tubarr lists (all states, including skipped and removed). `total` = count matching the filters. The UI
groups rows by day itself and pins `upcoming` premieres in a "Coming up" group on top.

### Channels
| Method & path | Body | Returns |
|---|---|---|
| `GET /api/channels` | | `{"channels": [ChannelSummary]}`, every channel (no paging) |
| `GET /api/channels/{id}` | | `ChannelDetail` |
| `GET /api/channels/{id}/videos` | | `{"videos": [Video], "total": 83}`, newest upload first, every listed video |
| `POST /api/lookup` | `{"q": "…"}` | `{"channel": ChannelPreview, "already_added": false}`, or 404 `not_found`. Signed-in session only (not the API key); a new channel is looked up live on YouTube, at most 6 a minute (429 `rate_limited`) |
| `POST /api/channels` | `{"q": "…", "settings": {partial settings}}` | `201` + `ChannelDetail` (status `syncing`); 409 if already added |
| `PATCH /api/channels/{id}` | `{"settings": {partial}, "meta": {partial}}` | `ChannelDetail` |
| `GET /api/channels/{id}/posters` | | `{"current": "gen-2", "variants": [PosterVariant]}` |
| `POST /api/channels/{id}/poster` | JSON `{"variant": "gen-3"}`, or `multipart/form-data` with a `file` (JPEG/PNG/WebP, ≤ 10 MB) | `ChannelDetail` |
| `POST /api/channels/{id}/refresh` | | `202`: check for new uploads now |
| `POST /api/channels/{id}/repolish` | | `202`: rebuild poster, banner, season posters and episode metadata, push to Plex |
| `DELETE /api/channels/{id}` | `{"immediate": false}` (optional) | `ChannelDetail` with status `pending_removal`, `removal.delete_at` = now + 3 days. With `"immediate": true`: deleted now, `{"ok": true, "deleted": true}` |
| `POST /api/channels/{id}/restore` | | `ChannelDetail` (undo a pending removal) |
| `POST /api/channels/refresh` | | `202`: check every channel |

- `q` (lookup and add) accepts `https://www.youtube.com/@handle`, `youtube.com/@handle/videos`, `@handle`, `handle`,
  `https://www.youtube.com/channel/UC…`, `UC…`, or a video URL (use its channel).
- `ChannelPreview` = `{ "id", "title", "handle", "url", "subscribers", "about", "avatar_url", "banner_url", "youtube_video_count" }`
  (any may be `null` except `id` and `title`).
- `meta` fields: `title` (display name), `summary`, `genre` (free text; the UI suggests YouTube's categories),
  `topic` (a topic id; pushed to Plex as the channel's collection, sets `topic_source: "user"`).
  A string sets and locks the field in Plex; `null` reverts it to YouTube's value and unlocks it.
- `settings` fields: see the table above; `null` = the default. Changing `retention` applies at once: videos that fall
  outside leave Plex (unless protected) and videos that come inside are queued as backlog (events follow).
- Restoring a channel whose `removal.reason` was `unsubscribed` sets `kept_while_unsubscribed: true`, so the next sync
  doesn't flag it again. Removing: stop downloading at once; after the grace period delete the files and the show from Plex.
- `PosterVariant` = `{"id": "gen-1", "label": "Tubarr classic", "url": "…"}`. Offer a few generated styles (the backend's
  poster renderer), YouTube's avatar-based one, and your upload (`"id": "upload"`) if there is one. `current` is
  the chosen variant's id. Choosing one sets `poster` in `locked_fields` and pushes it to Plex.

### Videos
| Method & path | Body | Returns |
|---|---|---|
| `GET /api/videos/{id}` | | `VideoDetail` |
| `PATCH /api/videos/{id}` | `{"title": "…", "summary": "…", "keep": true}` (any subset) | `VideoDetail` |
| `GET /api/videos/{id}/thumbnails` | | `{"current": "youtube", "variants": [{"id", "label", "url"}]}` |
| `POST /api/videos/{id}/thumbnail` | JSON `{"variant": "frame-50"}`, or `multipart/form-data` with a `file` | `VideoDetail` |
| `POST /api/videos/{id}/download` | `{"next": false, "now": false}` (optional) | `Video` (state `queued`) |
| `DELETE /api/videos/{id}` | | `Video` (state `removed`, `removed_reason: "manual"`) |
| `POST /api/videos/bulk` | `{"ids": ["…"], "action": "keep"}` | `{"done": 12, "skipped": [{"id": "…", "reason": "Shorts are always skipped."}]}` |

- PATCH `title`/`summary`: a string sets and locks it in Plex; `null` reverts to YouTube's. `keep` toggles "Keep forever".
- Thumbnail variants: YouTube's original (`"youtube"`), a few frames (`"frame-25"`, `"frame-50"`, `"frame-75"`: 25/50/75%
  through the video; only for downloaded videos), and `"upload"` if you uploaded one.
- `download` = Retry (failed), Download again (removed), Download anyway (`skipped_live`, `skipped_too_long`), or skip
  the SponsorBlock wait (`waiting_sponsorblock`). `"next": true` ("Download first", priority 2) puts it at the front of
  the queue at the normal pace. `"now": true` ("Download now", priority 3) makes it the very next download:
  the worker starts it within seconds, skipping the pace gap and the daily cap; still one at a time, so if a download
  is running it starts right after that one. The Video then has `download_now: true` (UI: "Starting…") until it
  starts. A Short → 409.
- Downloader `paused` (bool) = the "Pause downloads" flag is set (`POST /api/downloader/pause` / `resume`); with
  nothing running, `state` is then `paused`. A download that is already running finishes.
- `DELETE` = "Delete now": deletes the file and removes the episode from Plex, protected or not. On a queued video it
  means "don't download".
- `bulk` `action`: `keep`, `unkeep`, `delete`, `download`. Each id is handled like the single call; the ones that
  can't be done are listed in `skipped` with a reason. Events follow for every change.

### Space estimate (live, while you drag a control)
`POST /api/estimate`. It changes nothing; it answers "what would this cost?".

For one channel's override:
```json
{ "channel_id": "UC…", "retention": { "mode": "days", "days": 365 } }
```
For the fill target (Settings → Downloads):
```json
{ "channel_id": null, "fill_target_bytes": 2748779069440 }
```
Optional in both: `quality`, `max_duration_minutes`, `include_live` (for a channel: that channel; for `null`: every
channel using the default). A missing field means "as it is now"; `"retention": null` means "the default (fill)".
Response:
```json
{
  "scope": "channel",
  "channels_affected": 1,
  "current_bytes": 21474836480,
  "projected_bytes": 34359738368,
  "add": { "count": 30, "bytes": 12884901888 },
  "remove": { "count": 0, "bytes": 0 },
  "kept_count": 80,
  "oldest_date": "2024-11-02",
  "reach_date": "2025-02-18",
  "total": { "used_bytes": 1200000000000, "projected_bytes": 2044289259110,
             "cap_bytes": 2000000000000, "target_bytes": 1900000000000 },
  "estimated": true
}
```
- `current_bytes` = what the scope uses now; `projected_bytes` = what it would use once applied and settled (for fill,
  at the target). `add` = videos that would download; `remove` = videos that would leave Plex (never protected ones).
- `kept_count` / `oldest_date`: how many videos the scope would hold and the oldest upload date among them.
- `reach_date`: for the fill (a default channel, or a `fill_target_bytes` question): how far back the fill reaches.
- Downloaded sizes are real; the rest are duration × bitrate for the chosen quality (`estimated: true` whenever an
  estimate was used). This needs each channel's upload list with durations, which the channel scan already reads.
- Must answer in well under 200 ms: the UI calls it (debounced ~150 ms) while a slider moves.

### Activity
`GET /api/activity`
```json
{
  "downloader": { …same object as in Status… },
  "current": {
    "video": Video,
    "stage": "download",
    "stages": ["download", "sponsorblock", "artwork", "plex"],
    "progress": 0.42,
    "bytes_done": 410861568,
    "bytes_total": 978321408,
    "speed_bps": 6082000,
    "eta_seconds": 97,
    "resolution": "1080p",
    "started_at": "2025-06-26T13:03:10Z",
    "sponsorblock_cut_seconds": null
  },
  "queue": [ { "position": 1, "video": Video, "reason": "new_upload", "waits_for": null, "estimated_bytes": 734003200, "added_at": "…" } ],
  "failed": [ { "video": Video, "error": { …error… }, "failed_at": "…" } ],
  "history": [ HistoryItem ]
}
```
- `current` is `null` when nothing runs. `stages` lists only the stages this job goes through (no segments means no
  `sponsorblock`). `bytes_total`, `speed_bps`, `eta_seconds` may be `null`.
- `queue`: every queued video in run order. `history`: newest first, the latest 50.

`HistoryItem`: `{"id": "h_1842", "at": "…", "event": "downloaded", "video": Video, "size_bytes": 612368384, "sponsorblock_cut_seconds": 64, "detail": null}`.
`event`: `downloaded` · `failed` (`detail` = error message) · `skipped` (`detail` = `short`, `live` or `too_long`) ·
`removed` (`detail` = a `removed_reason`).

| Method & path | Returns |
|---|---|
| `GET /api/history?before=<iso>&limit=50` | `{"history": [HistoryItem], "next_before": "…" or null}` |
| `POST /api/downloader/pause` | downloader object; the current job stops at a safe point and returns to the queue front |
| `POST /api/downloader/resume` | downloader object |
| `POST /api/downloader/retry-failed` | `{"queued": 3}` (retryable ones only) |

### Storage
`GET /api/storage`
```json
{
  "path": "/youtube",
  "used_bytes": 1200000000000, "cap_bytes": 2000000000000, "target_bytes": 1900000000000, "free_bytes": 800000000000,
  "fill": { …Fill state… },
  "video_count": 3902, "backfill_count": 3410, "watched_count": 310, "watched_bytes": 118111600640,
  "kept_count": 42, "protected_count": 392, "sponsorblock_saved_seconds": 812345,
  "preserved": { "count": 9, "bytes": 3328599654 },
  "by_channel": [ { "channel_id": "UC…", "title": "Pocket Gadget Daily", "size_bytes": 64424509440, "video_count": 50, "poster_url": null, "retention_mode": "count" } ],
  "next_to_remove": [ { "video": Video, "size_bytes": 512753664, "reason": "watched", "watched_at": "2025-06-12T01:10:00Z" } ],
  "recently_removed": [ { "video": Video, "size_bytes": 512753664, "reason": "making_room", "at": "…" } ],
  "daily": [ { "date": "2025-05-28", "new_videos": 22, "backfill_videos": 190, "removed_videos": 0, "bytes": 96636764160 } ],
  "projection": { "avg_bytes_per_day": 9663676416, "days_to_target": 97 }
}
```
- `preserved`: videos kept because they disappeared from YouTube (`youtube_gone`), including everything from `gone`
  channels. The Storage page shows them as their own slice, apart from the rolling library.
- `by_channel`: the 12 biggest, largest first.
- `next_to_remove`: the next 10 the roll would remove, in order (watched oldest-watched first, then the oldest backlog
  from the biggest channel, repeatedly). Never protected videos. While `filling`, nothing is removed yet; this is
  what would go first.
- `recently_removed`: the roll's last 10 removals, newest first (empty while it has never been full).
- `daily`: exactly 30 entries, oldest first, zero days included. `new_videos` = new uploads that landed,
  `backfill_videos` = backlog videos that landed, `removed_videos` = removed by the roll or a window, `bytes` = landed bytes.
- `projection.avg_bytes_per_day`: last 7 days of growth. `days_to_target`: `null` if not growing or already rolling.

### Import (Google Takeout subscriptions.csv, or a pasted list of channels)
| Method & path | Body | Returns |
|---|---|---|
| `POST /api/import/preview` | `{"text": "…", "html": "…" or null}` | `ImportPreview` |
| `POST /api/import/apply` | `{"import_id": "imp_8f3a", "add": ["@handle"], "remove": ["UC…"]}` | `{"added": 3, "flagged_for_removal": 2}` |

- `text` = select-all + copy of `youtube.com/feed/channels`, or a Takeout `subscriptions.csv` (the UI also reads a
  dropped `.csv`/`.txt` file into `text`). `html` = the clipboard's HTML flavour when the browser offers it (it has
  the exact `/@handle` links). Parse whichever is better.
```json
{ "import_id": "imp_8f3a", "found": 121,
  "new": [ { "handle": "@HarborLightsVlog", "title": "Harbor Lights Vlog", "subscribers": 1210000, "channel_id": null } ],
  "missing": [ { "channel_id": "UC…", "title": "Patch Notes Weekly", "handle": "@PatchNotesWeekly" } ],
  "unchanged": 116, "unrecognized_lines": 0 }
```
- `new`: in the paste, not in Tubarr. `missing`: in Tubarr and subscribed, but not in the paste. Channels already
  `pending_removal` or `kept_while_unsubscribed` are never listed as missing.
- `apply` adds the chosen handles (defaults) and flags the chosen IDs for removal (3-day grace). You can untick
  rows, so `add`/`remove` may be subsets. An `import_id` expires after 1 hour (404).

### Plex
| Method & path | Returns |
|---|---|
| `GET /api/plex` | `PlexStatus` (may be cached up to 60 s) |
| `POST /api/plex/test` | `PlexStatus` (fresh check) |
| `POST /api/plex/scan` | `202`: scan the YouTube library |
| `POST /api/plex/repolish` | `202`: re-polish every channel |

```json
{
  "state": "connected", "url": "http://plex.local:32400", "server_name": "Home Server", "version": "1.43.3.10896",
  "checked_at": "2025-06-26T13:04:00Z", "message": null,
  "library": {
    "state": "ready", "name": "YouTube", "section_id": 14, "path": "/data/youtube",
    "agent": "Plex Personal Media", "scanner": "Plex TV Series",
    "show_count": 40, "episode_count": 1500, "last_scan_at": "2025-06-26T13:01:00Z",
    "shared_with_friends": [], "home_users_with_access": 2
  }
}
```
- `library` is `null` when the server isn't reachable; `message` explains a non-`connected` state.
- `shared_with_friends`: Plex **friends** (not Home users) who can see the library, by display name. The UI shows
  it so you know who else can see these videos.

### Settings
| Method & path | Body | Returns |
|---|---|---|
| `GET /api/settings` | | `{"downloads": DownloadSettings, "notifications": NotificationSettings}` |
| `PATCH /api/settings` | any subset, e.g. `{"downloads": {"quality": "720p"}}` | the full settings object |
| `POST /api/notifications/test` | `{"discord_webhook_url": "…"}` (optional: tests the unsaved value) | `{"ok": true}` or 400/502 |

`DownloadSettings` (the defaults every channel inherits):
```json
{
  "fill_target_bytes": 1900000000000,
  "protect_newest": 3,
  "quality": "1080p",
  "max_duration_minutes": 180,
  "include_live": false,
  "skip_shorts": true,
  "sponsorblock": ["sponsor", "selfpromo", "interaction"],
  "sponsorblock_wait_hours": 6,
  "check_interval_minutes": 5,
  "pacing": {
    "concurrency": 1, "overnight_start": "01:00", "overnight_end": "07:00",
    "daytime": "new_only", "day_rate_limit_bps": 6291456, "night_rate_limit_bps": null
  }
}
```
- `fill_target_bytes`: where the fill stops and the roll starts; the UI offers 2.0 TB to 3.0 TB (default 2.9 TB, must
  stay below `cap_bytes`). `protect_newest`: each channel's newest N are never rolled out (default 3; 0 to 10).
- `skip_shorts` and `pacing.concurrency` are fixed (`true` / `1`); the UI shows them locked; a PATCH changing them → 400.
- `pacing.daytime`: what may download outside the overnight window: `new_only` (backlog waits for night), `all`, `none`.
- `sponsorblock_wait_hours`: hold new uploads up to this long for SponsorBlock segments (`0` = don't wait).
- Rate limits: bytes/s, `null` = unlimited. `check_interval_minutes` (how often channel feeds are checked for new
  uploads): 5 (default), 10, 15, 30 or 60.
- Changing a default affects every channel whose own setting is `null`.

`NotificationSettings` (GET):
```json
{ "configured": true, "hint": "webhook …5678",
  "events": { "download_failed": true, "channel_changes": true, "signin_needed": true,
              "storage_warning": true, "daily_summary": true, "each_download": false } }
```
The webhook link is a secret and **write-only**: GET never returns it, only `configured` and (session only, not the
API key) a short `hint`. `PATCH` with `"discord_webhook_url": "https://discord.com/api/webhooks/<id>/<token>"` sets
it, `""` removes it, and leaving the key out keeps the saved one. Only discord.com / discordapp.com webhook links are
accepted (400 otherwise), also by `POST /api/notifications/test`. `channel_changes` = added, flagged for removal, removed. `storage_warning`
= the library started rolling, or usage passed the target.

### Sign-in and accounts

#### Open routes (no sign-in)
- `GET /health` -> `{"ok": true}`
- Static files under `/` (index.html, css, js, img, vendor). `web/mock/` is only in the source tree (offline preview from disk), not in the image.
- `GET /api/auth/state` -> 
  `{"setup_required": bool, "setup_code_required": bool, "authenticated": bool, "user": str|null, "csrf": str|null, "via": "session"|"api_key"|null}`
  - `setup_required` = no admin account exists yet.
  - `csrf` = the CSRF token for this session (same value as the non-HttpOnly cookie `tubarr_csrf`), null when not logged in.
- `POST /api/auth/setup` `{"username", "password", "setup_code"}` -> 200 same shape as auth/state (now authenticated; Set-Cookie). Only works while `setup_required`; otherwise 409 `conflict`. Wrong/missing setup code -> 403 `forbidden`. Password rules: at least 10 characters -> else 400 `bad_request`. The setup code is printed in the container log on first start (`docker logs tubarr`).
- `POST /api/auth/login` `{"username", "password"}` -> 200 auth/state shape + Set-Cookie. Wrong -> 401 `unauthorized` ("Wrong username or password."). Locked out (5 failures from this client address in 15 minutes) -> 429 `rate_limited` with message "Too many attempts. Try again in N minutes." and an extra `"retry_after": seconds` inside `error`. Failures for the same username or from many addresses never lock out; they only delay the answer (a few seconds at most). Over HTTPS the cookies are named `__Host-tubarr_session` / `__Host-tubarr_csrf`. Bodies over 64 KB on `/api/auth/*` get 413 `too_large` (1 MB elsewhere, 5 MB for the import).

#### Everything else needs sign-in
- Unauthenticated -> 401 `{"error":{"code":"unauthorized","message":"Please sign in."}}`. The UI must then show the login screen (never fall back to mock data on 401).
- Every non-GET request authenticated by session cookie must send header `X-CSRF-Token: <csrf>` (read the `tubarr_csrf` cookie, or keep the `csrf` value from /api/auth/state). Missing/wrong -> 403 `{"error":{"code":"csrf",...}}`. On a `csrf` error the UI should refetch /api/auth/state once and retry.
- Scripts can instead send `X-Api-Key: <key>` (no CSRF needed). Never in the URL.

#### Account routes
- `POST /api/auth/logout` -> `{"ok": true}` (cookie cleared).
- `POST /api/auth/password` `{"current_password", "new_password"}` -> `{"ok": true}`. Signs out every OTHER session (this one gets a fresh cookie + new csrf: refetch /api/auth/state after). Wrong current -> 403 `forbidden`; weak new -> 400.
- `GET /api/auth/apikey` -> `{"exists": bool, "created_at": iso|null, "hint": "…1a2b"|null}`
- `POST /api/auth/apikey` -> `{"api_key": "<plain key, shown ONCE>", "created_at": iso, "hint": "…1a2b"}` (creates or regenerates; the old key stops working).
- `DELETE /api/auth/apikey` -> `{"ok": true}` (revoked).

### First-run setup (Plex first)
- `GET /api/setup` ->
  ```
  {"done": bool,
   "plex": {"configured": bool, "url": str|null, "token_set": bool, "library": str|null, "plex_root": str|null,
            "state": "connected"|"unauthorized"|"unreachable"|"not_configured", "server_name": str|null, "message": str|null},
   "channels": int, "subscriptions_pending": int, "media_path": "/youtube"}
  ```
  The token itself is NEVER returned anywhere.
- `POST /api/setup/plex/pin` -> `{"pin_id": int, "code": "ABCD", "auth_url": "https://app.plex.tv/auth#?...", "link_url": "https://plex.tv/link", "expires_at": iso}`.

- `GET /api/setup/plex/pin/{pin_id}` -> `{"state": "waiting"|"authorized"|"expired", "servers": [{"name": str, "owned": bool, "connections": [{"uri": str, "local": bool, "relay": bool}]}]}` (servers only when authorized; token saved server-side, encrypted).
- `POST /api/setup/plex` `{"url": "http://host:32400", "token": "..."?}` -> the `plex` object above plus `"libraries"` (list, below). `token` is optional and write-only: omit it to keep the saved one (after the PIN flow, just send the chosen server url). 400 on a bad URL.
- `GET /api/setup/plex/libraries` -> `{"libraries": [{"key": "7", "title": "YouTube", "agent": "...", "agent_label": "...", "paths": ["/data/youtube"]}]}` (TV-show libraries only).
- `POST /api/setup/plex/library` `{"title": "YouTube", "plex_root": "/data/youtube"}` -> `plex` object. `plex_root` = the SAME media folder as Plex sees it (pre-fill it with the library's first path; explain: "Tubarr saves to /youtube inside its container; this is that folder's path as Plex sees it").
- `POST /api/setup/plex/clear` -> `plex` object (Plex disconnected; token deleted).
- `POST /api/setup/complete` -> `{"done": true}`. Called at the end of the wizard, also from "Skip" paths.
- Subscriptions import reuses the existing routes:
  - `POST /api/import/preview` `{"text": "<csv file contents or pasted lines>"}` -> existing shape (`import_id`, `found`, `new`[{handle, channel_id, title}], `missing`, `unchanged`, `unrecognized_lines`). Accepts Google Takeout `subscriptions.csv` (header `Channel Id,Channel Url,Channel Title`), channel URLs (`youtube.com/@x`, `/channel/UC…`, `/c/x`, `/user/x`), `@handles` and UC ids, one per line.
  - `POST /api/import/apply` `{"import_id", "add": [handle-or-channel-id...], "remove": []}` -> `{"added": n, "flagged_for_removal": n, "queued": true}`. Channels are looked up in the background at a gentle pace (a few seconds apart); the UI should say so.
  - The CSV file upload is client-side: `<input type=file accept=".csv,text/csv,text/plain">` read with FileReader -> send its text to preview.

### Network (proxies for YouTube traffic)
Lines = "direct" (no proxy, always exists, id "direct", name "Direct") plus 0..N proxies.
- `GET /api/network` ->
  ```
  {"mode": "direct"|"single"|"failover"|"rotate",
   "proxies": [{"id": "p_ab12cd", "name": "Backup line", "masked": "socks5h://***@host:1080", "scheme": "socks5h"}],
   "order": ["direct", "p_ab12cd", ...],      // failover priority / rotate order; first = preferred
   "selected": "p_ab12cd"|null,              // for mode "single"
   "fail_threshold": 3,                       // failover: consecutive connection failures before switching
   "cooldown_minutes": 360,                   // failover: wait before trying the preferred line again
   "rotate_hours": 6,                         // rotate: take turns this often (only between downloads)
   "check_preferred": true,                   // failover: test the preferred line (one lookup) before returning to it
   "status": {"active": "direct"|"p_…", "active_name": "Direct", "ip": "203.0.113.5"|null, "latency_ms": 180|null,
              "since": iso|null, "reason": "…"|null,
              "last_switch": {"at": iso, "from_name": "Direct", "to_name": "Backup line", "reason": "3 connection failures in a row"}|null,
              "next_rotate_at": iso|null, "preferred_retry_at": iso|null, "updated_at": iso|null, "stale": bool}}
  ```
- `PATCH /api/network` with any of `mode, selected, order, fail_threshold (1..20), cooldown_minutes (5..10080), rotate_hours (0.5..168), check_preferred` -> GET shape. 400 on bad values (e.g. mode single without a selected proxy).
- `POST /api/network/proxies` `{"name", "url"}` -> GET shape (new proxy appended to `order`). URL schemes: http, https, socks5, socks5h (optional user:pass@). Stored encrypted; the URL is never returned (only `masked`).
- `PATCH /api/network/proxies/{id}` `{"name"?, "url"?}` -> GET shape (url write-only; omit to keep).
- `DELETE /api/network/proxies/{id}` -> GET shape.
- `POST /api/network/proxies/{id}/test` (id may be "direct") -> `{"ok": true, "ip": "203.0.113.5", "latency_ms": 240}`; failure -> 502 `{"error":{"code":"proxy_failed","message":"…"}}`. Fetches the public IP through that line (api.ipify.org).

### Settings additions
- `GET /api/settings` `downloads` gains: `"max_height": 2160|1440|1080|720` and `"allow_av1": bool`. `downloads.quality` now mirrors max_height ("2160p" etc).
- `PATCH /api/settings` accepts `downloads.max_height` (one of 2160, 1440, 1080, 720) and `downloads.allow_av1` (bool). `fill_target_bytes` range is 10 GB .. 1 PB.
- `GET /api/status`: `youtube` is always `null` (there is no in-app YouTube sign-in or subscription sync in this release).

### Security notes
- **API key is READ-ONLY**: requests authenticated with `X-Api-Key` may only use GET. `GET /api/network` with an API key returns only `{"status": {...}}` (no proxies). /api/auth/apikey, /api/audit and all changes need the admin session + CSRF.
- **`GET /api/network`** also returns `"never_direct": bool` and `"never_direct_auto": bool` (true = not set explicitly; on automatically when any proxy exists), and `status.paused: bool` (+ `status.reason` explains). `PATCH /api/network` accepts `never_direct` (bool).
- **Proxy test** can return 429 `rate_limited` (5 tests/min per user) and 502 `proxy_failed` with one generic message; every failure takes the full 10 s.
- **API key view of `GET /api/network`**: `status` without `ip`.
- **never-direct**: with `never_direct_auto`, the guard stays on after the last proxy is deleted (downloads pause, 503 `network_paused` where a live YouTube request is needed) until `mode` is set to `direct`.
- **Security log**: `GET /api/audit?limit=100` -> `{"entries": [{"at": iso, "who": "admin"|"system", "action": "auth.login"|"auth.login_failed"|"network.proxy_added"|..., "detail": "…"}]}` (newest first).

## Topics and series (Plex organization)

### Topics
Every channel has exactly one **topic**. Tubarr guesses it (`topic_source: "auto"`); you can change it on the
channel page (`PATCH /api/channels/{id}` with `{"meta": {"topic": "space"}}`). Each topic is a Plex collection in the
YouTube library, so Plex can browse by topic.

| `id` | Label |
|---|---|
| `cars` | Cars & Builds |
| `tech` | Tech & PCs |
| `science` | Science & Engineering |
| `space` | Space |
| `aviation` | Aviation |
| `gaming` | Gaming |
| `history` | History & Stories |
| `makers` | Makers & DIY |
| `music` | Music |
| `other` | Other |

`GET /api/topics` → `{"topics": [{"id": "science", "label": "Science & Engineering", "channel_count": 18}]}`, in the
order above. (The UI has the same list built in, so this is only needed for the counts.)

### Series
Tubarr looks at each channel's playlists and detects the ones that are **real series** (numbered parts of one story,
e.g. a playthrough or a restoration build). A detected series can be shown in Plex as its own series (you
decides, per playlist); everything else is listed as ignored, with the reason.

| Method & path | Body | Returns |
|---|---|---|
| `GET /api/channels/{id}/series` | | `{"series": [Series], "ignored": [IgnoredPlaylist]}` |
| `PATCH /api/channels/{id}/series/{playlist_id}` | `{"enabled": true, "order": "playlist"}` (either) | the `Series` |

```json
{
  "series": [
    { "id": "PLa1b2…", "title": "Harbor Lines: Season 1", "episode_count": 14, "in_plex_count": 14,
      "enabled": true, "order": "playlist", "first_date": "2025-04-02", "last_date": "2025-06-21",
      "url": "https://www.youtube.com/playlist?list=PLa1b2…" }
  ],
  "ignored": [
    { "id": "PLz9…", "title": "Best of 2025", "video_count": 22, "reason": "compilation",
      "url": "https://www.youtube.com/playlist?list=PLz9…" }
  ]
}
```
- `enabled`: "Show as a series in Plex". Default `true` for detected series. Turning it off puts the episodes back in
  the channel's normal year seasons.
- `order`: `playlist` (the playlist's own order) or `upload` (by upload date). Part numbers follow it.
- `episode_count` = videos in the playlist that pass the channel's rules; `in_plex_count` = of those, how many are in Plex.
- Ignored `reason`: `not_numbered` (titles don't look like parts), `mixed_channels` (it collects other channels'
  videos), `compilation` (a best-of or a mix of unrelated videos), `too_small` (fewer than 3 videos), `shorts`
  (mostly Shorts), `live` (stream archive). The UI shows them as short sentences. PATCHing an ignored playlist with
  `{"enabled": true}` promotes it to a series anyway ("Use as a series anyway").
- Changes are pushed to Plex; a `video.state` event follows for every video whose `series` changed.

## Trimarr (ad trimmer, proxied)

Trimarr runs as its own container, installed and started together with Tubarr (see `trimarr/README.md`). It trims sponsor segments out of videos **already in Plex** using SponsorBlock data. It ships **off**.
The UI keeps it visually secondary.

The UI never talks to Trimarr directly: the Tubarr backend proxies it under **`/api/trimarr/…`** (same origin, no
CORS), with Trimarr's base URL in its own config (`TRIMARR_URL`, default `http://trimarr:8791`; Trimarr starts with
Tubarr). With `TRIMARR_URL=off`, every `/api/trimarr/…` call returns 404 `not_found`; if Trimarr is down, 502. The UI then hides the per-video actions and shows "Trimarr isn't
installed" / "can't reach it". **All Trimarr routes the UI uses live in `web/js/trimarr.js`**, so matching the final
Trimarr API means editing that one file (or mapping these routes in the proxy).

| Method & path (as the UI calls them) | Body | Returns |
|---|---|---|
| `GET /api/trimarr/status` | | `{"enabled": false, "trimmed_videos": 214, "removed_seconds": 51234, "queue": 0, "channels_enabled": 3, "last_run_at": "…", "version": "…"}` |
| `PATCH /api/trimarr/settings` | `{"enabled": true}` | the status object (global on/off) |
| `GET /api/trimarr/channels/{channel_id}` | | `{"channel_id": "UC…", "enabled": false}` |
| `PATCH /api/trimarr/channels/{channel_id}` | `{"enabled": true}` | `{"channel_id": "UC…", "enabled": true}` |
| `POST /api/trimarr/videos/{video_id}/trim` | | `{"video_id": "…", "state": "trimmed" | "queued", "removed_seconds": 102}` |
| `POST /api/trimarr/videos/{video_id}/undo` | | `{"video_id": "…", "state": "original", "removed_seconds": 0}` |
| `GET /api/trimarr/report?channel=UC…` | | dry run: `{"videos": 312, "seconds": 33120, "bytes_saved": 9663676416, "channels": 3, "dry_run": true}` (no `channel` = every channel it's enabled for, or all if none) |

After a trim or undo, Tubarr should push a `video.state` event with the video's new `trim` value.

Tubarr -> Trimarr: every request carries `X-Trimarr-Token` (the link token Tubarr creates automatically in the shared
`trimarr-link` volume, or `TRIMARR_TOKEN` when set: 16+ characters, the same for both containers), goes direct (no proxy environment variables) and never follows a redirect. Without a usable token the
proxy answers 503 `not_configured` without calling Trimarr. Trimarr itself answers 401 without the right token, 421
for a Host it doesn't know, and sends no CORS headers. Trimarr doesn't talk to Plex: Tubarr refreshes the item in
Plex after each trim or undo (it polls Trimarr's `GET /api/videos?state=trimmed,undone&swapped_since=<epoch>`).

## Serving the UI
- Serve `tubarr/app/web/` as static files at `/` (index `index.html`). Routing is hash-based (`#/timeline`,
  `#/channels`, `#/channel/UC…`, `#/activity`, `#/storage`, `#/settings/youtube`), so no server-side fallback is needed.
- `web/mock/` is only loaded when the API is unreachable; serving it is harmless.
- Send `Cache-Control: no-cache` for `index.html`, `/js/*` and `/css/*` so updates show up at once.
- Start-up: `?mock=1` or opening the file from disk forces the mock; otherwise the UI calls `GET /api/status` and
  uses the mock only if that fails or takes over 2.5 s. If the API drops later, the UI shows a "Can't reach Tubarr"
  banner and retries; it never swaps to mock data mid-session.
