# Security

## Reporting a vulnerability

Please report security problems privately through GitHub's **Security → Report a vulnerability** on this
repository, not in a public issue. Include steps to reproduce. We'll acknowledge the report and fix confirmed issues
as quickly as we can.

## Design

Tubarr is a single-admin, self-hosted app. It's meant to run on your own network, or behind a reverse proxy with
HTTPS on a dedicated hostname for remote access (see the README's "Remote access"). Docker-published ports bypass
the host firewall: never port-forward Tubarr's port.

| Protection | Where it lives |
|---|---|
| Sign-in gate for every `/api/` route (except sign-in itself), CSRF check on every change, read-only API key (GET only; never the account, security log or channel lookup), security headers (CSP with an image allowlist, frame-deny, nosniff), HSTS over HTTPS | `tubarr/webapp/security.py`: `guard()`, `SESSION_ONLY_PREFIXES`, `CSP` |
| Request body caps: 64 KB on `/api/auth/*`, 5 MB on the import, 1 MB elsewhere; Content-Length checked up front, chunked bodies counted as they arrive (413) | `tubarr/webapp/security.py`: `BodyLimit`, `body_limit()` |
| Admin password: argon2id hash only (inputs over 1024 characters are refused unhashed); the first-run setup code; seeding from `TUBARR_ADMIN_PASSWORD_HASH` | `tubarr/auth.py`: `create_admin()`, `setup_code()`, `seed_from_env()`, `set_password()` |
| Sign-in throttling: 5 failures lock that client address for 15 minutes; failures per username and globally only add a capped delay (never a lock), so nobody can lock the admin out; the password change uses the same per-client lock | `tubarr/auth.py`: `verify_login()`, `_check_client()`, `_delay()`, `change_password()` |
| Client address: the TCP peer, or X-Forwarded-For only from `TUBARR_TRUSTED_PROXIES` (uvicorn `proxy_headers` / `forwarded_allow_ips`; off by default) | `tubarr/webapp/__main__.py`; `security.py`: `client_addr()`, `trusted_proxies()` |
| Constant-time comparisons on UTF-8 bytes (setup code, username, CSRF token, API key), so non-ASCII input is a normal 401/403 | `tubarr/auth.py`: `_eq()` |
| Sessions: signed with a per-install key, listed server-side (sign-out and password change really end them, including open live-update streams), CSRF token per session | `tubarr/auth.py`: `new_session()`, `read_session()`, `end_session()`, `csrf_ok()`; `tubarr/webapp/events.py`: `stream()` |
| Cookie flags: HttpOnly, SameSite=Lax, Secure and the `__Host-` prefix over HTTPS | `tubarr/webapp/security.py`: `set_session_cookies()`, `cookie_name()`, `is_https()` |
| API key: random, shown once, stored as SHA-256, compared in constant time, GET-only; its view of `/api/network` has no public IP and of `/api/settings` no webhook hint | `tubarr/auth.py`: `new_api_key()`, `check_api_key()`; `net.py`: `public()`; `views.py`: `settings()` |
| Secrets at rest: per-install key file (mode 600), Fernet encryption, atomic writes (mode 600) for credentials, `settings.json` and `auth.json`; a key that doesn't match the credentials file is logged once, clearly | `tubarr/vault.py`: `install_key()`, `put()`, `get()`, `_write_private()`; `tubarr/settings.py`: `save()` |
| Nothing secret is sent to the browser (Plex token never; proxies masked only; the Discord webhook is write-only, only "configured" + a short hint) | `tubarr/webapp/setupflow.py`: `state()`; `tubarr/net.py`: `public()`, `_masked()`; `tubarr/webapp/views.py`: `_notifications()` |
| Log and error-text redaction (URL credentials, tokens, cookies, API keys, passwords, Discord webhook tokens, every stored secret value including each proxy's username, password and URL, in the web app and the worker) | `tubarr/redact.py`: `Filter`, `install()`, `remember_proxy()`, `text()`; `tubarr/db.py`: `upsert()` |
| Security log (who, when, what; never credentials); failed sign-ins and lockouts are folded to one line a minute, and 5 old generations are kept, so a flood can't push history out | `tubarr/audit.py`: `write()`, `_fold()`, `_rotate()` |
| Proxy URL validation: strict parsing, allowed schemes, host/port rules, no control or shell characters, no path/query/fragment, no extra `@`, length caps; the parsed components are stored and the URL is rebuilt from them | `tubarr/net.py`: `parse_proxy_url()`, `_store_proxy()`, `_url()` |
| Proxies are only used for YouTube traffic, passed as options (yt-dlp `proxy`, requests `proxies=`), never on a command line; environment proxy variables are ignored for YouTube; non-YouTube hosts are refused, also after a redirect (every hop re-checked) | `tubarr/net.py`: `ytdlp_proxy()`, `yt_session()`, `yt_get()`, `_YT_HOSTS`; `tubarr/ytdl.py`: `_params()` |
| yt-dlp: YouTube extractors only (`allowed_extractors`, no generic extractor); plugins only from the image's site-packages (never from HOME=/data); images fetched with a 15 MB cap and Pillow's pixel limit at 50 MP | `tubarr/ytdl.py`: `_params()`, `_pin_plugin_dirs()`; `tubarr/art.py`: `fetch()` |
| Local traffic never proxied and never redirected: Plex and plex.tv (`trust_env=False`, redirects refused), Trimarr (same); the PO-token plugin talks to its sidecar directly | `tubarr/plexhttp.py`; `tubarr/webapp/addons.py`: `_call()`, `_plex()` |
| Plex setup: plex.tv PIN flow; the account token stays in memory only, and only the chosen server's own token is stored (encrypted); changing the address to another origin wipes the token; the address is just scheme://host:port | `tubarr/webapp/setupflow.py`: `start_pin()`, `poll_pin()`, `save_plex()`, `_check_url()` |
| Discord notifications: only `https://discord.com/api/webhooks/<id>/<token>` (and discordapp.com) links, no redirects, no environment proxies, also for the test button | `tubarr/notify.py`: `WEBHOOK_RE`, `valid()`, `post()`; `tubarr/actions.py`: `test_notification()` |
| Channel lookup (live YouTube request): signed-in session only, POST (CSRF-checked), at most 6 a minute | `tubarr/webapp/app.py`: `api_lookup()`, `_lookup_rate_ok()` |
| Proxy test: one fixed URL (`https://api.ipify.org`), no redirects, 10 s timeout, 5 per minute per user; only ok/IP/latency returned; every failure has the same message and takes the full 10 s (no port-scan or timing oracle) | `tubarr/net.py`: `test_line()`, `_TEST_URL`, `_test_rate_ok()`; route `api_test_proxy()` in `tubarr/webapp/app.py` |
| Line switching can't be abused: at most 6 switches per hour, at least 10 minutes on a line, only between downloads, the pace and daily cap unchanged | `tubarr/net.py`: `tick()`, `_may_switch()`, `_switch()`; `tubarr/worker.py`: `settings_watcher()` |
| Never-direct leak guard: when every allowed proxy is down, or the last proxy is deleted, YouTube traffic pauses (with an alert) instead of using the real connection; only an explicit switch to Direct mode ends it | `tubarr/net.py`: `never_direct()`, `_candidates()`, `active_line()`, `NetworkPaused`; `tubarr/worker.py`: `download_worker()` |
| Hostile YouTube data: video ids validated before they become paths; show folders unique per channel; deleting a channel never removes a folder another channel uses; publishing never overwrites another video's file; NFO text stripped of XML-invalid characters; subtitle entities can't reintroduce markup; age-restricted videos are skipped, not treated as a bot check; a bot check re-queues the video with a delay | `tubarr/naming.py`: `check_video_id()`; `tubarr/pipeline.py`: `unique_folder()`, `publish()`, `download_stage()`; `tubarr/actions.py`: `_folder_is_own()`; `tubarr/nfo.py`: `xml_text()`; `tubarr/subtitles.py`: `_clean()`; `tubarr/ytdl.py`: `is_bot_check()`, `skip_reason()` |
| Trimarr: no published port, its own internal network with Tubarr; every call except `GET /health` needs the shared `X-Trimarr-Token` (`TRIMARR_TOKEN`, 16+ characters; fails closed without it); Host allowlist (DNS rebinding); no CORS; no Plex credentials (Tubarr refreshes Plex after a trim); only locked or voted, day-old SponsorBlock segments are cut | `trimarr/trimarr/api.py`; `trimarr/trimarr/planner.py`; `tubarr/webapp/addons.py` |
| Container: refuses to run as root (unless `TUBARR_ALLOW_ROOT=1`), `/data` mode 700, read-only root filesystem, all capabilities dropped, `no-new-privileges`, memory/process limits, no Docker socket, no privileged mode; the web UI published on 127.0.0.1 by default; the PO-token helper runs read-only as non-root; the helpers can't reach each other | `Dockerfile`, `docker/entrypoint.sh`, `docker-compose.yml` |
| Supply chain: hash-pinned, wheels-only Python dependencies; base images pinned by digest; vendored fonts; GitHub Actions pinned to commit SHAs; Dependabot; release images only after CI passes, with a signed build-provenance attestation; `PYTHONNOUSERSITE=1` | `requirements.in`/`.txt`, `trimarr/requirements.*`, `Dockerfile`, `docker/fonts/`, `.github/` |

Tests covering the validator (injection strings), the proxy test endpoint (ignores any target), sign-in and lockout, CSRF,
API key rules, body caps, non-ASCII input, secrets at rest, redaction, the Plex token scope and redirects, the
webhook, hostile YouTube data and the never-direct guard are in `tests/`; Trimarr's (token, Host, CORS, SponsorBlock
trust rules, video ids) are in `trimarr/tests/`.

## Known limits

- One admin account; no roles.
- The web app trusts `X-Forwarded-Proto` only to **add** the cookie `Secure` flag (never to relax anything).
  `X-Forwarded-For` is only believed from `TUBARR_TRUSTED_PROXIES`. Behind a reverse proxy without that setting,
  every client shares the proxy's address, so 5 bad sign-ins through the proxy lock the proxy's address (you can
  still sign in from the LAN directly).
- Sign-in rate limits are kept in memory and reset when the web app restarts.
- `TUBARR_YTDLP_AUTOUPDATE=1` (off by default) installs the newest yt-dlp from PyPI at every start, into `/tmp`
  (never the data folder). That code isn't hash-pinned: you trust PyPI and yt-dlp's releases at each start.
- The PO-token helper (bgutil) accepts a proxy URL in each request and otherwise honours `HTTP(S)_PROXY`; it must stay
  reachable by Tubarr only (the compose networks do that).
- Trimarr can no longer skip a trim while a video is being watched in Plex (it has no Plex access); a stream in
  progress keeps reading the old, still-open file.

## Backups

Back up the whole data folder together: `tubarr.db`, `settings.json`, `auth.json`, `credentials.json` and
`secret.key`. The key decrypts the credentials and signs sessions, so the backup is a secret: store it encrypted. If
the key is lost or doesn't match, Tubarr logs a warning and you enter the Plex sign-in, proxies and webhook again;
nothing else is lost.
