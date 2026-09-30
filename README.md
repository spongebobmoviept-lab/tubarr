<div align="center">

<img src="web/img/favicon.svg" width="88" alt="Tubarr logo">

# Tubarr

**Your YouTube subscriptions, as a polished Plex library.**
Channels become shows, playlists become seasons, and new uploads land in Plex minutes after they're published, downloaded at a calm, human pace.

[![License: MIT](https://img.shields.io/badge/license-MIT-blue.svg)](LICENSE)
[![Image on GHCR](https://img.shields.io/badge/image-ghcr.io%2Fspongebobmoviept-lab%2Ftubarr-2ea44f?logo=docker&logoColor=white)](https://github.com/spongebobmoviept-lab/tubarr/pkgs/container/tubarr)
[![Built for Plex](https://img.shields.io/badge/built%20for-Plex-e5a00d?logo=plex&logoColor=white)](#plex-setup)

</div>

---

Tubarr is a self-hosted "arr" for YouTube. You give it the channels you follow; it keeps a Plex TV library of them up to date: every channel is a show with real artwork, episodes carry the creator's own titles, descriptions, chapters and English subtitles, and the library fills to the size you choose and then rolls, newest in and oldest out. It has a fast, friendly web UI for everything, and it works without Plex too.

## Features

- **Built for Plex.** Sign in with Plex in the first-run setup, pick a library, done. Tubarr writes NFO files and artwork next to every video, scans only what changed, and adds labels and topic collections.
- **Channels as shows.** Each channel gets a poster, a background and a summary. Playlists that are real series (Part 1, 2, 3...) become their own seasons, and everything else is grouped by year.
- **New uploads first.** Every channel's feed is checked about every 5 minutes, and fresh videos jump the queue.
- **A human pace.** One download at a time, a random pause between downloads, and a daily cap for the backlog. It's built to never look like a bulk scraper.
- **Fill and roll.** Set a size (say 1 TB). Tubarr fills it with the newest videos, then swaps the oldest out for new ones. Videos you keep forever, each channel's newest few, and anything being watched are never removed.
- **Keeps what disappears.** If YouTube takes a video down (removed, private, copyright), Tubarr notices, labels it and never deletes it.
- **Quality you choose.** Up to 4K; VP9 or H.264; AV1 off by default, so files play everywhere without transcoding. SDR is preferred, audio is AAC, files are MKV.
- **English subtitles.** The creator's own subtitles, or YouTube's auto-captions cleaned into a proper SRT.
- **Proxies with failover.** Optional proxies for YouTube traffic with Direct, One proxy, Failover and Rotate modes, and a leak guard that pauses rather than fall back to your own connection.
- **Secure by default.** Admin sign-in, an argon2id password hash, CSRF protection, a read-only API key, and encrypted credentials. It runs as a non-root user with a read-only root filesystem.
- **Optional ad trimming.** The Trimarr add-on removes SponsorBlock sponsor segments from videos already in your library (off until you turn it on, and undoable).
- **No AI rewriting.** Titles and descriptions are the creator's own, cleaned by simple rules only.

## Screenshots

| Timeline | Channels |
|---|---|
| ![Timeline](docs/screenshots/timeline.png) | ![Channels](docs/screenshots/channels.png) |
| **Activity** | **Settings** |
| ![Activity](docs/screenshots/activity.png) | ![Settings](docs/screenshots/settings.png) |

<sub>Screenshots show the UI's offline preview with made-up channels.</sub>

## How it works

```
 your subscriptions            Tubarr                                          Plex
 (Takeout CSV, links,   ──►  queue: new uploads first, then the backlog  ──►  "YouTube" library
  @handles)                  newest-published first                           ├─ Channel A (show)
                               │                                              │   ├─ Series: "Build log" (season 1)
                               ▼                                              │   └─ 2026 (season)
                             download at a human pace                         └─ Channel B (show)
                             (one at a time, random pauses, daily cap,
                              optional proxy)
                               │
                               ▼
                             finalize: one lossless merge (video + audio + chapters), thumbnail,
                             subtitles, NFO; publish with an atomic rename; Plex scans that folder
                               │
                               ▼
                             fill target reached? roll: newest in, oldest deletable out
                             (YouTube is asked first: a taken-down video is kept forever)
```

1. **Subscriptions.** You import them once (Google Takeout `subscriptions.csv`, or paste channel links/@handles) and add more later with **Add channel**. Tubarr looks each one up in the background, a few seconds apart.
2. **Queue.** New uploads are found through each channel's RSS feed and go to the front. The backlog is filled newest-published first across all channels. Shorts, livestream replays and members-only videos are skipped.
3. **Download.** yt-dlp picks the best format within your rules, in one session per video, with a PO-token helper (the `bgutil-provider` sidecar) as YouTube now expects. Downloads start one at a time with a random pause between them, and backlog downloads have a daily cap.
4. **Library.** Each finished video gets a thumbnail, subtitles and an NFO next to it, and is moved into place atomically. Channels are shows, real series playlists are seasons, and everything else sits in year seasons.
5. **Rolling.** At the fill target, a newer video only comes in if the oldest deletable one goes out.

## Quick start

You need Docker with Compose, and (recommended) a Plex Media Server that can see the same media folder.

No need to clone the repo or build anything: two files are enough.

```bash
mkdir tubarr && cd tubarr
curl -fsSLO https://raw.githubusercontent.com/spongebobmoviept-lab/tubarr/v0.1.0/docker-compose.yml
curl -fsSL -o .env https://raw.githubusercontent.com/spongebobmoviept-lab/tubarr/v0.1.0/.env.example
# edit .env: YOUTUBE_DIR (where videos go), PUID/PGID, TZ
mkdir -p data && sudo chown -R 1000:1000 data "$YOUTUBE_DIR"   # match PUID:PGID
docker compose up -d          # downloads the ready-made image, nothing to build
docker compose logs tubarr | grep "Setup code"
```

Images for amd64 and arm64 (e.g. Raspberry Pi 4/5) are published at `ghcr.io/spongebobmoviept-lab/tubarr`. To build from source instead, uncomment `build:` in `docker-compose.yml` (it builds straight from this GitHub repo) and run `docker compose up -d --build`. Portainer/Dockge users can paste `docker-compose.yml` as a stack and set `YOUTUBE_DIR` in its environment.

By default the web UI is published on `127.0.0.1:9194`, this host only (see [Remote access](#remote-access) before
opening it to your network). Open `http://localhost:9194` (or an SSH tunnel to it) and follow the setup:

1. **Create the admin account.** Choose a username and password, and enter the one-time setup code from the log. It proves you're the one running the container.
2. **Connect Plex (recommended).** Press **Sign in with Plex** and approve Tubarr in the Plex window (or enter the code at plex.tv/link). Then pick your server.
3. **Pick the library.** Choose the TV library that points at your `YOUTUBE_DIR` (see [Plex setup](#plex-setup)). If Plex mounts the folder at a different path than `/youtube`, enter that path.
4. **Add your subscriptions.** Upload Google Takeout's `subscriptions.csv` (takeout.google.com → YouTube and YouTube Music → subscriptions), or paste channel links or @handles, one per line.

That's it. The first videos show up in Plex within the hour, and the backlog fills in slowly and steadily.

### Not using Plex?

Tubarr still saves every video with standard NFO info files and artwork, which **Jellyfin and Emby read as-is** (the same way Sonarr/Radarr libraries work there). Just skip the Plex step. Without Plex, the "is someone watching this?" check and Plex labels are skipped; everything else works the same.

## Plex setup

Create one library for Tubarr (the setup can use an existing one):

| Plex setting | Value |
|---|---|
| Library type | **TV Shows** |
| Folder | the same folder as `YOUTUBE_DIR`, as Plex sees it |
| Scanner | Plex TV Series |
| Agent | **Plex NFO** (local files only) where your Plex version offers it; otherwise **Personal Media Shows** (Plex's local-media agent) |
| Advanced → Use local assets | On |
| Advanced → Seasons | "Hide" if you'd rather see every video on the channel page (optional; Tubarr's seasons still exist in the files) |
| Advanced → Episode sorting | Newest first |

Tubarr writes `tvshow.nfo`, `season.nfo`-style metadata, `poster.jpg`, `fanart.jpg` and one `.nfo`, `.jpg` and `.en.srt` per episode. Nothing is matched online, so no YouTube channel is ever mistaken for a TV series. After each download, Tubarr asks Plex to scan only that channel's folder.

## Configuration

Set these in `.env` (see [.env.example](.env.example) for the full, commented list). Download settings can also be changed live in **Settings → Downloads**; a value saved there wins over the environment.

| Variable | Default | What it does |
|---|---|---|
| `YOUTUBE_DIR` | (required) | Host folder for the library (mounted at `/youtube`). |
| `TUBARR_DATA_DIR` | `./data` | Host folder for Tubarr's database, settings, encrypted credentials and logs (mounted at `/data`). |
| `PUID` / `PGID` | `1000` / `1000` | The user and group the containers run as. Both folders must be writable by them. |
| `TZ` | `UTC` | Time zone for upload dates and schedules. |
| `TUBARR_PORT` | `9194` | Host port for the web UI (published on 127.0.0.1 by default; see [Remote access](#remote-access)). |
| `TUBARR_FILL_TARGET_GB` | `1000` | Fill the library to this size (decimal GB), then roll. |
| `TUBARR_MAX_HEIGHT` | `2160` | Best quality: `2160` (4K), `1440`, `1080` or `720`. |
| `TUBARR_ALLOW_AV1` | `0` | `1` allows AV1 when it's the best format (many TVs, sticks and GPUs can't decode it). |
| `TUBARR_DAILY_CAP` | `40` | Backlog downloads per 24 h. New uploads aren't held by it. |
| `TUBARR_GAP_MIN_MINUTES` / `TUBARR_GAP_MAX_MINUTES` | `10` / `20` | The random pause between downloads. |
| `TUBARR_CONCURRENCY` | `1` | Downloads at once. Keep `1` unless you know better. |
| `TUBARR_PROTECT_NEWEST` | `3` | Never roll out each channel's newest N videos. |
| `TUBARR_RATELIMIT` | `0` | Per-download speed limit in bytes/s (`0` = none). |
| `TUBARR_POLL_MIN` | `5` | Minutes between new-upload checks. |
| `TUBARR_MIN_FREE_GB` | `30` | Don't start a download below this much free disk space. |
| `TUBARR_YTDLP_AUTOUPDATE` | `0` | `1` = fetch the newest yt-dlp from PyPI at every start (see [Updating](#updating)). |
| `TUBARR_SESSION_DAYS` | `30` | How long a sign-in lasts. |
| `TUBARR_COOKIE_SECURE` | `auto` | Secure cookies over HTTPS (auto-detected, also behind a proxy sending `X-Forwarded-Proto`). |
| `TUBARR_TRUSTED_PROXIES` | (empty) | Your reverse proxy's address(es)/CIDRs. Only these may set `X-Forwarded-For`/`-Proto` (see [Remote access](#remote-access)). |
| `TUBARR_ALLOW_ROOT` | (empty) | The container refuses to run as root; `1` overrides that (not recommended). |
| `TUBARR_ADMIN_USER` / `TUBARR_ADMIN_PASSWORD_HASH` | (empty) | Unattended installs: seed the admin from an argon2id **hash** (see [Security](#security)). |
| `PLEX_URL` / `PLEX_SECTION` / `PLEX_ROOT` | (setup) | Normally set by the setup. `PLEX_ROOT` = the media folder as Plex sees it. The Plex **token** is never an env var. |
| `TRIMARR_URL` | (empty) | `http://trimarr:8791` when you run the Trimarr add-on. |
| `TRIMARR_TOKEN` | (empty) | Shared secret for Tubarr ↔ Trimarr (16+ characters, e.g. `openssl rand -hex 32`). Required for Trimarr. |

## Proxies and VPNs

YouTube rate-limits and sometimes flags addresses that download a lot. **The best protection is the default slow pace.** If you want downloads to leave through another connection, open **Settings → Network**:

- **Proxies:** add any number, each with a name and an address like `socks5h://user:pass@host:1080`. Supported schemes are `socks5h` (recommended: DNS goes through the proxy), `socks5`, `http` and `https`. The address is stored encrypted and never shown again in full. **Test** fetches your public IP through that proxy and shows it with the latency.
- **Modes:**
  - **Direct:** no proxy.
  - **One proxy:** always use the chosen one.
  - **Failover:** try the lines in order. Move to the next after N connection failures in a row or a YouTube bot check, and return to the preferred line after a cooldown (optionally after one test lookup through it).
  - **Rotate:** take turns every few hours, only between downloads.
- **Never fall back to Direct** (on by default once you add a proxy): if every allowed proxy is down, Tubarr **pauses** downloads and shows an alert instead of quietly using your own connection. It stays on even after you delete your last proxy (downloads pause); choose the **Direct** mode yourself to go back to your own connection.
- **Test** answers only "worked, with this IP and latency" or one generic failure, always after the same wait, and at most 5 times a minute, so it can't be used to probe what's behind a proxy.
- Only YouTube traffic uses the proxy (plus the PO-token helper's own YouTube requests: the yt-dlp plugin hands it the same proxy). Plex, Trimarr and local traffic always go direct, and ignore `HTTP(S)_PROXY` environment variables. Switching is capped (at most 6 per hour, at least 10 minutes on a line) and never speeds anything up: the pace and daily cap stay the same.

**VPN:** run Tubarr's network through a VPN container such as [gluetun](https://github.com/qdm12/gluetun) (`network_mode: "service:gluetun"`). A commented, digest-pinned example is in `docker-compose.yml`. Give the internal `pot` and `trim` networks fixed subnets (the commented `ipam` lines) and list them, plus your LAN, in gluetun's `FIREWALL_OUTBOUND_SUBNETS`, or Tubarr can't reach its helpers and Plex. Note that commercial VPN addresses are often already flagged by YouTube; a residential proxy or a second connection of your own usually works better. Either way, keep the pace slow.

## Security

- **Sign-in required.** Everything except the sign-in page, static files and `/health` needs the admin session. There's no default password: the first-run setup creates the account and requires the one-time setup code from the container log.
- **Tubarr never stores your password**, only a one-way argon2id hash. Changing the password signs out every session.
- **Sessions** are HttpOnly, SameSite=Lax cookies, signed with a per-install key and marked Secure over HTTPS (with the `__Host-` name prefix, so another site on a sibling subdomain can't plant one). Every change needs a CSRF token. Request bodies are size-capped.
- **Sign-in throttling.** 5 failed attempts from one address lock **that address** out for 15 minutes. Failures for the same username or across all addresses never lock anyone out; they only slow sign-in down (a few seconds at most), so an attacker can't lock you out of your own server. Behind a reverse proxy every client shares the proxy's address: set `TUBARR_TRUSTED_PROXIES` so Tubarr sees the real client (see [Remote access](#remote-access)). A password change uses the same per-address limit.
- **API key** (Settings → Account): for scripts and monitors, sent as the `X-Api-Key` header, never in a URL. It is **read-only** (GET requests only) and can't see proxy details, your public IP, the webhook, the account, the security log or the channel lookup. It's shown once; Tubarr keeps only a SHA-256 hash of it.
- **Credentials at rest.** The Plex token, proxy addresses and the Discord webhook link are encrypted (Fernet) with a random per-install key, `/data/secret.key` (mode 600). They are never sent back to the browser (the webhook field is write-only) and are scrubbed from logs. The Plex token is the chosen server's own token (not your plex.tv account token), and it's deleted if you point Tubarr at a different Plex address; Plex requests never follow redirects.
- **Security log.** Settings → Account lists sign-ins, password and API key changes, Plex changes, and every network change and automatic line switch (never credentials).
- **Containers.** Tubarr, Trimarr and the PO-token helper run as non-root users (Tubarr refuses to start as root) with a read-only root filesystem, all capabilities dropped, `no-new-privileges` and memory/process limits. No Docker socket, no privileged mode, no host networking. The data folder is kept at mode 700. Each helper sits on its own internal network with Tubarr: the PO-token helper and Trimarr can't reach each other, and neither publishes a port. Trimarr only answers requests that carry the shared `TRIMARR_TOKEN`.
- **Supply chain.** Python dependencies are pinned with sha256 hashes and installed wheels-only (`--require-hashes`); base images are pinned by digest; poster fonts are vendored in `docker/fonts/`; GitHub Actions are pinned to commit SHAs. Release images are published only after CI passes and carry a signed build-provenance attestation (`gh attestation verify oci://ghcr.io/spongebobmoviept-lab/tubarr:<version> --owner spongebobmoviept-lab`).

**Forgot the password?**
```bash
docker exec -it tubarr python -m tubarr.resetpw
```
**Unattended install** (no browser step): make a hash and put it in `.env` as `TUBARR_ADMIN_PASSWORD_HASH='...'`:
```bash
docker run --rm -it tubarr:local python -m tubarr.hashpw
```

### Backups

Back up the **whole data folder together** (`TUBARR_DATA_DIR`): the database, `settings.json`, `auth.json`, `credentials.json` and `secret.key` belong together. `secret.key` decrypts `credentials.json` (Plex token, proxies, webhook) and signs sign-in sessions, so treat the backup as a secret (store it encrypted, not in a shared drive). If the key is lost or doesn't match the credentials file, Tubarr logs a warning and you enter the Plex sign-in, proxies and webhook again; nothing else is lost. The video library itself can always be re-downloaded.

### Remote access

The compose file publishes the web UI on `127.0.0.1:9194` (this host only). For your LAN, switch to the commented `"${TUBARR_PORT:-9194}:9194"` line.

> **Docker-published ports bypass the host firewall** (ufw and firewalld don't filter them), so a LAN-published port is reachable by every device that can reach the host. **Never port-forward 9194 on your router.**

For access from outside, put Tubarr behind a reverse proxy with HTTPS, for example Caddy (`reverse_proxy tubarr:9194`), Traefik or nginx, ideally on a **dedicated hostname** (e.g. `tubarr.example.com`, not a path on a site shared with other apps: cookies and the browser's same-origin rules are per host), or use a VPN such as WireGuard or Tailscale. Set `TUBARR_TRUSTED_PROXIES` to the proxy's address (e.g. `172.18.0.5` or `172.18.0.0/16`) so sign-in throttling and the security log see the real client address; without it, `X-Forwarded-For` is ignored and every remote client shares the proxy's address. Cookies become Secure (and `__Host-` prefixed) automatically when the connection is HTTPS.

## Trimarr (optional ad trimmer)

Trimarr is a separate, small container that removes SponsorBlock-labelled **sponsor** and **self-promotion** segments from videos already in your library. It works in place (same file, so the Plex item and watch state stay), cuts on keyframes without re-encoding, verifies the result before replacing anything, shifts subtitles and chapters to match, and keeps the original for undo (7 days by default). Only SponsorBlock segments that are locked, or have at least one vote and are at least 24 hours old, are cut. Trimarr holds no Plex credentials: Tubarr refreshes Plex after each trim or undo with its own token.

```bash
# in .env: TRIMARR_URL=http://trimarr:8791 and TRIMARR_TOKEN=<openssl rand -hex 32>
mkdir -p trimarr-data && sudo chown 1000:1000 trimarr-data
docker compose --profile trimarr up -d
```

Then switch it on in Tubarr: **Settings → Trim ads** (it ships off). See [trimarr/README.md](trimarr/README.md).

## Updating

- **Tubarr:** change the image tag in `docker-compose.yml` to the new release (or re-download the file), then `docker compose pull && docker compose up -d`.
- **yt-dlp** has to keep up with YouTube. The image ships a pinned, hash-verified yt-dlp; update it by pulling a newer Tubarr image. Optional: `TUBARR_YTDLP_AUTOUPDATE=1` fetches the newest yt-dlp release from PyPI at every container start (wheels only, into an in-memory folder, never the data folder, so nothing downloaded survives a restart). That code isn't hash-pinned: turning it on means trusting PyPI and yt-dlp's release process at every start, which is why it's off by default. If PyPI can't be reached, the image's version is used. A `.pylib` folder in the data folder from an older version is no longer used and can be deleted. yt-dlp only loads plugins from the image itself, never from the data folder.
- **PO-token helper:** it's pinned by digest in `docker-compose.yml`. When you update it, also bump `bgutil-ytdlp-pot-provider` in `requirements.txt` to the matching version and rebuild.

## FAQ

**Age-restricted videos?** Skipped (they need a signed-in, age-verified account). They don't count as a bot check and don't pause anything.

**YouTube says "Sign in to confirm you're not a bot".** That's a rate-limit signal. Tubarr backs off on its own (slower, one at a time) and recovers after a few good downloads. Keep the pace slow: the daily cap and pause are the real fix. A proxy (Settings → Network) helps if your address stays flagged.

**Can I download faster?** You can raise `TUBARR_CONCURRENCY`, shorten the pause and raise the daily cap, but bursts are exactly what gets addresses flagged. The defaults are deliberately calm.

**Does it need a YouTube account?** No. Tubarr downloads anonymously, and it never asks for your Google password or cookies.

**Members-only videos, Shorts, livestreams?** Skipped. Premieres are downloaded once they've aired.

**What gets deleted when the library is full?** The oldest video that may be deleted, one at a time. Never deleted: videos marked "Keep forever", each channel's newest few, anything playing right now, and anything that's gone from YouTube. Before any automatic delete, Tubarr checks with YouTube that the video is still up.

**Where are the logs?** `docker compose logs tubarr`, plus `data/logs/worker.log`. Secrets are scrubbed from both.

**How do I read the API?** See [docs/API.md](docs/API.md). Use an API key from Settings → Account in the `X-Api-Key` header.

## Please be kind to creators

Tubarr is for **personal archiving and offline viewing** of channels you already follow. Respect creators and YouTube's terms: don't redistribute what you download, and keep supporting the people you watch (watch on YouTube, memberships, sponsors, merch). Tubarr keeps videos exactly as uploaded, sponsor reads included, unless you choose to use Trimarr.

## Development

```bash
python -m compileall -q tubarr trimarr tests                           # syntax
for f in $(find web -name '*.js'); do node --check "$f"; done          # UI syntax
docker build -t tubarr:local .
docker run --rm --network none -e TUBARR_DATA=/tmp/d -e TUBARR_ROOT=/tmp/y -v "$PWD/tests:/app/tests:ro" \
  --entrypoint python tubarr:local -m unittest discover -s tests -v
docker build -t trimarr:local ./trimarr
docker run --rm --network none -e TRIMARR_TOKEN=test-token-0123456789abcdef -v "$PWD/trimarr/tests:/app/tests:ro" \
  --entrypoint python trimarr:local -m unittest discover -s tests -v
```

Dependencies: edit `requirements.in` (or `trimarr/requirements.in`) and regenerate the hash-pinned `requirements.txt` with the command in its header (always on Linux, inside the pinned Python image).

The UI is plain JavaScript with no build step. Open `web/index.html` from disk to see it with made-up data (offline preview mode; the mock files in `web/mock/` aren't part of the image, and a running server always shows live data). The API is documented in [docs/API.md](docs/API.md), and the security design in [SECURITY.md](SECURITY.md).

## License

[MIT](LICENSE) © Tubarr contributors. Tubarr uses [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FFmpeg](https://ffmpeg.org), [bgutil-ytdlp-pot-provider](https://github.com/Brainicism/bgutil-ytdlp-pot-provider) (GPL-3.0, run as a separate container) and [SponsorBlock](https://sponsor.ajay.app) data (Trimarr). Poster fonts are Montserrat, Inter and Bebas Neue (SIL Open Font License). Tubarr is not affiliated with YouTube, Google or Plex.
