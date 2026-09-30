# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and versions follow [Semantic Versioning](https://semver.org/).

## [0.1.0] - 2026-09-30

First public release.

### Added
- YouTube subscriptions -> Plex TV library: channels as shows with generated posters and backgrounds, real series
  playlists as seasons, year seasons for everything else, NFO files and artwork next to every video.
- New uploads found through each channel's RSS feed about every 5 minutes; they jump the queue.
- Human-pace downloading: one at a time, a random pause between downloads, a daily cap for the backlog.
- Fill-and-roll retention with a configurable fill target; "keep forever", protected newest videos, and a live
  YouTube check before every automatic delete (taken-down videos are kept forever).
- Quality rules: up to 4K (configurable), VP9/H.264, AV1 off by default, SDR preferred, AAC audio, MKV.
- English subtitles (the creator's, or YouTube's auto-captions cleaned into SRT) and YouTube chapters.
- Web UI: timeline, channels, activity, storage, settings; live updates; offline preview mode.
- First-run setup: admin account (with a one-time setup code), Sign in with Plex (PIN flow), library pick,
  subscriptions import from Google Takeout `subscriptions.csv` or pasted links/@handles.
- Works without Plex (NFO + artwork are read by Jellyfin and Emby as-is).
- Network: optional proxies for YouTube traffic with Direct / One proxy / Failover / Rotate modes, a proxy test,
  a never-fall-back-to-Direct leak guard, and a status chip.
- Security: argon2id password hash, signed server-side sessions (`__Host-` cookies over HTTPS), CSRF protection,
  per-client sign-in lockout (per-username and global limits only slow down), request body caps, read-only API key
  (stored as a hash), encrypted Plex server token, proxy credentials and Discord webhook, log redaction, security log.
- Docker: multi-arch image, refuses to run as root, read-only root filesystem, dropped capabilities, memory/process
  limits, web UI on 127.0.0.1 by default; PO-token helper and Trimarr on separate internal networks; hash-pinned
  dependencies, digest-pinned base images, vendored fonts; optional yt-dlp auto-update at start (off by default).
- Optional Trimarr add-on: SponsorBlock sponsor/self-promotion trimming in place, verified, undoable.
