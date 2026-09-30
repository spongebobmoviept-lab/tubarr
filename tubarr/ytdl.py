"""yt-dlp wrapper: anonymous (no account), gentle pacing, format rules from Settings.

Formats: the best resolution up to Settings -> Downloads -> "Best quality" (default 2160p/4K); VP9 preferred, H.264
where it's the best on offer; AV1 only if "Allow AV1" is on (off by default: many TVs, streaming sticks and GPUs
can't decode it, and Tubarr never transcodes); SDR preferred over HDR; AAC audio preferred; MKV.

Every YoutubeDL instance is built by _params(), which sets:
  * `proxy` for the active network line (net.py): "" = direct (and environment proxy variables are ignored);
  * the PO-token provider sidecar (bgutil-ytdlp-pot-provider). YouTube's player API wants a proof-of-origin token
    from clients; the sidecar generates it the way yt-dlp's maintainers recommend. The plugin talks to the sidecar
    directly, never through the proxy;
  * allowed_extractors: YouTube's extractors only (no generic extractor: a hostile link or redirect can't make
    yt-dlp fetch and parse arbitrary pages).
yt-dlp plugins load only from the image's own site-packages (_pin_plugin_dirs): never from HOME=/data
(~/.config/yt-dlp/plugins, ~/.yt-dlp/plugins, ~/yt-dlp-plugins) or PYTHONPATH, so nothing written to the data
folder can become code.
"""
import logging
import os
import subprocess
import sysconfig
import time

import yt_dlp
from yt_dlp.networking import Request

from . import config, net, settings

log = logging.getLogger("tubarr.ytdl")


def _pin_plugin_dirs():
    """yt-dlp looks for plugin packages under each child of every plugin dir; the parent of the image's
    site-packages has site-packages/yt_dlp_plugins (the PO-token provider) and nothing writable."""
    try:
        from yt_dlp.globals import plugin_dirs
        plugin_dirs.value = [os.path.dirname(sysconfig.get_paths()["purelib"])]
    except Exception as e:                   # an older yt-dlp without plugin_dirs: its defaults stay
        log.warning("couldn't restrict yt-dlp's plugin folders: %s", e)


_pin_plugin_dirs()

MAX_HEIGHT = 2160
EN_SUBS = ["en", "en-US", "en-GB", "en-CA", "en-AU", "en-orig"]
NOAV1 = "[vcodec!^=av01][vcodec!^=av1]"


def quality():
    """(max_height, allow_av1) from Settings (live)."""
    s = settings.load()
    try:
        h = int(s.get("max_height") or MAX_HEIGHT)
    except (TypeError, ValueError):
        h = MAX_HEIGHT
    return max(144, min(h, 4320)), bool(s.get("allow_av1"))


def _no_av1():
    return "" if quality()[1] else NOAV1


# Used only for the metadata pass of the legacy Video class; PlainVideo builds its own spec from quality().
FORMAT = "bv[height<=2160]%(n)s+ba/b%(n)s" % {"n": NOAV1}


def _codec_rank(vcodec):
    v = (vcodec or "").lower()
    if v.startswith(("av01", "av1")):
        return 3 if quality()[1] else -1             # only when "Allow AV1" is on
    if v.startswith(("vp09", "vp9")):
        return 2
    if v.startswith(("avc1", "h264")):
        return 1
    return 0


def choose_formats(info, max_height=MAX_HEIGHT, prefer=("vp9", "h264"), hls=False):
    """(video_format, audio_format) as separate streams. Highest resolution <= max_height, then fps, then
    VP9 over H.264 (reverse with prefer=('h264','vp9')), SDR over HDR, bitrate. Never AV1.
    hls=False: direct https (DASH) streams; hls=True: YouTube's HLS variants (fallback when https gives 403)."""
    fm = info.get("formats") or []
    want = ("m3u8", "m3u8_native") if hls else ("https", "http")
    vids = [f for f in fm if f.get("vcodec") not in (None, "none") and f.get("acodec") in (None, "none")
            and (f.get("height") or 0) <= max_height and _codec_rank(f.get("vcodec")) > 0 and f.get("protocol") in want]
    if not vids:
        return None, None
    pref = {"vp9": 2, "h264": 1} if prefer[0] == "vp9" else {"vp9": 1, "h264": 2}

    def vkey(f):
        codec = "vp9" if _codec_rank(f.get("vcodec")) == 2 else "h264"
        sdr = (f.get("dynamic_range") or "SDR") == "SDR"
        return (f.get("height") or 0, round(f.get("fps") or 0), sdr, pref[codec],
                f.get("protocol") in ("https", "http"), f.get("tbr") or 0)
    v = max(vids, key=vkey)
    auds = [f for f in fm if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")
            and "drc" not in str(f.get("format_id")) and (f.get("protocol") in want or not hls)]

    def akey(f):
        return ((f.get("language_preference") or 0), (f.get("acodec") or "").startswith("mp4a"),
                f.get("protocol") in ("https", "http"), f.get("abr") or f.get("tbr") or 0)
    a = max(auds, key=akey) if auds else None
    return v, a


def est_size(f, duration):
    if not f:
        return 0
    return f.get("filesize") or f.get("filesize_approx") or int((f.get("tbr") or 0) * 1000 / 8 * (duration or 0))


class _Log:
    def debug(self, m):
        if not m.startswith("[debug] "):
            log.debug(m)

    def info(self, m):
        log.info(m)

    def warning(self, m):
        log.warning(m)

    def error(self, m):
        log.error(m)


class BotCheck(RuntimeError):
    """YouTube asked us to prove we're not a bot. Stop everything and report; never push through."""


class AgeRestricted(RuntimeError):
    """The video needs a signed-in, age-verified account. Not a bot check: the video is skipped, nothing pauses."""


def is_bot_check(msg):
    """Only YouTube's real bot-check / rate-limit answers ("Sign in to confirm you're not a bot", HTTP 429)."""
    m = str(msg or "").lower()
    return "not a bot" in m or "http error 429" in m


def is_age_restricted(msg):
    m = str(msg or "").lower().replace("\u2019", "'")
    return ("confirm your age" in m or "age-restricted" in m or "age restricted" in m
            or "inappropriate for some users" in m)


def _params(**kw):
    p = dict(
        quiet=True, noprogress=True, logger=_Log(),
        js_runtimes={"deno": {"path": "/usr/local/bin/deno"}},
        cachedir=os.path.join(config.DATA, "cache", "yt-dlp"),
        socket_timeout=30, retries=10, fragment_retries=10, extractor_retries=2,
        retry_sleep_functions={"http": lambda n: min(2 ** n, 30), "fragment": lambda n: min(2 ** n, 30)},
        sleep_interval_requests=0.75,          # yt-dlp's "-t sleep" preset pacing
        noplaylist=True, check_formats=False,
        allowed_extractors=["youtube.*"],      # YouTube's own extractors only; never the generic one
        extractor_args={"youtubepot-bgutilhttp": {"base_url": [config.BGUTIL_URL]}},
    )
    if "proxy" not in kw:
        p["proxy"] = net.ytdlp_proxy()        # raises net.NetworkPaused when no allowed line is usable
    extra_ea = kw.pop("extractor_args", None)
    if extra_ea:                               # merge, don't clobber -- some callers (channel_tab) set their own
        for k, v in extra_ea.items():
            p["extractor_args"][k] = v
    p.update(kw)
    return p


def _guard(e):
    msg = str(e)
    if is_age_restricted(msg):                 # checked first: "Sign in to confirm your age" is NOT a bot check
        raise AgeRestricted(msg) from e
    if is_bot_check(msg):
        raise BotCheck(msg) from e
    raise e


def channel_tab(ref, limit=30, tab="videos"):
    """Flat listing of a channel tab. `ref` is an @handle, a UC... id or a URL.
    Returns yt-dlp's playlist dict: channel metadata (id, title, description, thumbnails incl. avatar_uncropped /
    banner_uncropped, follower count) + `entries` (newest first; id, title, duration, approximate timestamp)."""
    if ref.startswith("http"):
        url = ref.rstrip("/") + "/" + tab
    elif ref.startswith("@"):
        url = "https://www.youtube.com/%s/%s" % (ref, tab)
    else:
        url = "https://www.youtube.com/channel/%s/%s" % (ref, tab)
    opts = _params(extract_flat="in_playlist", playlistend=limit, noplaylist=False,
                   extractor_args={"youtubetab": {"approximate_date": [""]}})
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            info = y.extract_info(url, download=False)
    except yt_dlp.utils.DownloadError as e:
        _guard(e)
    info["entries"] = list(info.get("entries") or [])
    return info


# yt-dlp does the choosing (it already skips formats it can't fetch without a PO token and prefers https).
# Video and audio are fetched as two separate files ("A,B") so Tubarr can merge them in ONE ffmpeg -c copy pass
# (with metadata + YouTube's own chapters) straight into the library: the disk is written only once.
PLAIN_SORT = ["hdr:sdr", "proto:https", "vcodec:vp9", "acodec:m4a"]


def plain_format(max_height):
    n = _no_av1()
    return "(bv*[height<=%d]%s/b%s/b),(ba[acodec^=mp4a]/ba/b)" % (max_height, n, n), ["res:%d" % max_height] + PLAIN_SORT


def fallback_format():
    n = _no_av1()
    return "(bv*%s/b%s),(ba/b)" % (n, n)                 # after a 403: plain best, AV1 rule unchanged


class PlainVideo:
    """One YoutubeDL SESSION per video: the same instance extracts and downloads (the googlevideo URLs belong to
    the session that fetched them - a second session gets HTTP 403). The video and audio streams land as separate
    files in the work folder; English manual subtitles become SRT. One retry in a fresh session on a 403."""

    def __init__(self, video_id, workdir, stage, hook=None, subtitles=True, max_height=MAX_HEIGHT):
        self.id, self.workdir, self.stage = video_id, workdir, stage
        self.url = "https://www.youtube.com/watch?v=" + video_id
        top = quality()[0]
        fmt, sort = plain_format(min(max_height or top, top))   # the channel's quality setting, capped by Settings
        self.opts = _params(
            format=fmt, format_sort=sort,
            paths={"home": workdir, "temp": workdir}, outtmpl={"default": "media.%(format_id)s.%(ext)s",
                                                               "subtitle": "subs.%(ext)s"},
            writesubtitles=False, writeautomaticsub=False,   # subtitles are fetched by the finalize stage
            ratelimit=config.RATE_LIMIT or None, concurrent_fragment_downloads=1,
            overwrites=False, progress_hooks=[hook] if hook else [])
        self.ydl = yt_dlp.YoutubeDL(self.opts)
        self.info = None
        self.fallback_used = False
        self.fresh_used = False

    def extract(self):
        try:
            self.info = self.ydl.extract_info(self.url, download=False)
        except yt_dlp.utils.DownloadError as e:
            _guard(e)
        return self.info

    def chosen(self):
        """(video_format, audio_format) yt-dlp selected (requested_downloads for 'A,B' selections)."""
        fm = self.info.get("requested_downloads") or self.info.get("requested_formats") or [self.info]
        flat = []
        for f in fm:
            flat += f.get("requested_formats") or [f]
        v = next((f for f in flat if f.get("vcodec") not in (None, "none")), None)
        a = next((f for f in flat if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")), None)
        return v, a

    def _clean(self):
        for d in (self.stage, self.workdir):
            for f in os.listdir(d):
                p = os.path.join(d, f)
                if os.path.isfile(p):
                    os.remove(p)

    def files(self):
        """(video_file, audio_file_or_None, [srt]) from the scratch dir, identified by probing."""
        import json as _j
        import subprocess as _sp
        vfile = afile = None
        for f in sorted(os.listdir(self.workdir)):
            if not f.startswith("media.") or f.endswith((".part", ".ytdl", ".srt", ".vtt")):
                continue
            p = os.path.join(self.workdir, f)
            out = _sp.run(["ffprobe", "-v", "error", "-show_streams", "-of", "json", p], capture_output=True, text=True)
            kinds = {s.get("codec_type") for s in _j.loads(out.stdout or "{}").get("streams", [])}
            if "video" in kinds and not vfile:
                vfile = p
            elif "audio" in kinds and not afile:
                afile = p
        subs = sorted(os.path.join(self.workdir, f) for f in os.listdir(self.workdir) if f.endswith(".srt"))
        return vfile, afile, subs

    def download(self):
        """-> (video_file, audio_file_or_None, [srt paths]) on the scratch."""
        try:
            self.ydl.process_ie_result(self.info, download=True)
        except yt_dlp.utils.DownloadError as e:
            msg = str(e)
            if "subtitle" in msg.lower() and self.opts.get("writesubtitles"):
                log.warning("%s: subtitles failed (%s); retrying without", self.id, msg[:120])
                self._clean()
                self.opts["writesubtitles"] = False
                self.ydl = yt_dlp.YoutubeDL(self.opts)
                self.extract()
                return self.download()
            transient = any(t in msg for t in ("more expected", "Connection reset", "timed out", "IncompleteRead",
                                               "Remote end closed", "Connection aborted"))
            if transient and not self.fresh_used:
                # the server kept dropping the connection even after yt-dlp's own retries: new URLs, same choice
                log.warning("%s: connection kept failing (%s); retrying once in a fresh session", self.id, msg[:120])
                self._clean()
                self.fresh_used = True
                self.ydl.close()
                self.ydl = yt_dlp.YoutubeDL(self.opts)
                try:
                    self.info = self.ydl.extract_info(self.url, download=True)
                except yt_dlp.utils.DownloadError as e2:
                    _guard(e2)
                vfile, afile, subs = self.files()
                if not vfile:
                    raise RuntimeError("download produced no video file: %s" % os.listdir(self.workdir))
                return vfile, afile, subs
            if "403" not in msg or self.fallback_used:
                _guard(e)
            log.warning("%s: 403 on the chosen format; retrying in a fresh session with plain best", self.id)
            self._clean()
            self.fallback_used = True
            opts = dict(self.opts, format=fallback_format())
            opts.pop("format_sort", None)
            self.ydl.close()
            self.ydl = yt_dlp.YoutubeDL(opts)
            try:
                self.info = self.ydl.extract_info(self.url, download=True)
            except yt_dlp.utils.DownloadError as e2:
                _guard(e2)
        vfile, afile, subs = self.files()
        if not vfile:
            raise RuntimeError("download produced no video file: %s" % os.listdir(self.workdir))
        return vfile, afile, subs

    # ---- streaming path: the video stream goes straight into ffmpeg, so the library pool is written ONCE
    CHUNK = 10 << 20                      # googlevideo serves ranged requests at full speed (same as yt-dlp)

    def selected(self):
        """(video_format, audio_format) the format spec picks, WITHOUT downloading (yt-dlp's own selector; chosen()
        only knows after a download)."""
        fmts = self.info.get("formats") or []
        sel = self.ydl.build_format_selector(self.opts["format"])
        ctx = {"formats": fmts,
               "has_merged_format": any("none" not in (f.get("acodec"), f.get("vcodec")) for f in fmts),
               "incomplete_formats": all(f.get("vcodec") == "none" for f in fmts) or all(f.get("acodec") == "none" for f in fmts)}
        flat = []
        for f in sel(ctx):
            flat += f.get("requested_formats") or [f]
        v = next((f for f in flat if f.get("vcodec") not in (None, "none")), None)
        a = next((f for f in flat if f.get("acodec") not in (None, "none") and f.get("vcodec") in (None, "none")), None)
        return v, a

    def streamable(self):
        """Separate plain-https video and audio formats (the normal YouTube case) -> can be stream-muxed."""
        try:
            vf, af = self.selected()
        except Exception as e:
            log.warning("%s: format preview failed (%s); normal download", self.id, e)
            return False
        ok = lambda f: bool(f) and f.get("protocol") in ("https", "http") and bool(f.get("url")) and not f.get("fragments")
        return ok(vf) and ok(af) and vf.get("format_id") != af.get("format_id")

    def _renew(self, fid):
        """Fresh session (new URLs) -> the same format again, or None."""
        try:
            self.ydl.close()
        except Exception:
            pass
        self.ydl = yt_dlp.YoutubeDL(self.opts)
        try:
            self.info = self.ydl.extract_info(self.url, download=False)
        except yt_dlp.utils.DownloadError as e:
            _guard(e)
        return next((f for f in self.info.get("formats") or [] if f.get("format_id") == fid), None)

    def fetch(self, fmt, write, hook=None, tries=10):
        """Chunked (10 MiB ranges) download of one format into write(bytes). After any error it resumes at the exact
        byte offset (nothing is written twice); a 403 or a run of failures gets ONE fresh session (new URLs, same
        format) and carries on from the same offset. Returns the byte count."""
        fid = fmt["format_id"]
        total = fmt.get("filesize") or None
        pos, fails, renewed, win = 0, 0, False, []
        while total is None or pos < total:
            end = pos + self.CHUNK - 1 if total is None else min(pos + self.CHUNK, total) - 1
            req = Request(fmt["url"], headers=dict(fmt.get("http_headers") or {}, Range="bytes=%d-%d" % (pos, end)))
            got = 0
            try:
                with self.ydl.urlopen(req) as r:
                    if total is None:
                        tail = (r.headers.get("Content-Range") or "").rsplit("/", 1)[-1]
                        total = int(tail) if tail.isdigit() else None
                    while True:
                        buf = r.read(1 << 20)
                        if not buf:
                            break
                        write(buf)
                        pos += len(buf)
                        got += len(buf)
                        if hook:
                            now = time.time()
                            win.append((now, pos))
                            while len(win) > 2 and now - win[0][0] > 5:
                                win.pop(0)
                            sp = (pos - win[0][1]) / max(now - win[0][0], 1e-3) if len(win) > 1 else 0
                            hook({"status": "downloading", "downloaded_bytes": pos, "total_bytes": total, "speed": sp,
                                  "info_dict": {"format_id": fid}})
                if got == 0:
                    if total is None:
                        break
                    raise IOError("empty response at byte %d of %d" % (pos, total))
                fails = 0
                if total is None and got < self.CHUNK:
                    break                                       # size unknown and a short range: that was the end
            except (BrokenPipeError, BotCheck):
                raise
            except Exception as e:
                msg = str(e)
                if is_bot_check(msg) and not is_age_restricted(msg):
                    raise BotCheck(msg) from e
                fails += 1
                forbidden = "403" in msg
                if (forbidden or fails > tries) and not renewed:
                    log.warning("%s: %s at byte %d of %s; fresh session, same format %s", self.id, msg[:120], pos, total, fid)
                    renewed, fails = True, 0
                    nf = self._renew(fid)
                    if not nf:
                        raise RuntimeError("format %s not offered after a fresh session (%s)" % (fid, msg[:200]))
                    fmt = nf
                    continue
                if forbidden or fails > tries:
                    raise RuntimeError("download failed at byte %d of %s: %s" % (pos, total, msg[:300]))
                time.sleep(min(2 ** fails, 30))
        if hook:
            hook({"status": "finished", "downloaded_bytes": pos, "total_bytes": total or pos, "speed": 0,
                  "info_dict": {"format_id": fid}})
        return pos

    def stream_mux(self, dest, meta, hook=None):
        """Audio to scratch (small), then the video stream piped straight into ONE ffmpeg -c copy mux (+ metadata and
        chapters from `meta`) writing `dest` in the library's .staging. Returns (video_format, audio_format)."""
        vf, af = self.selected()
        apath = os.path.join(self.workdir, "media.%s.%s" % (af["format_id"], af.get("ext") or "m4a"))
        with open(apath + ".part", "wb") as f:
            self.fetch(af, f.write, hook)
        os.replace(apath + ".part", apath)
        errp = os.path.join(self.workdir, "ffmpeg.err")
        with open(errp, "wb") as err:
            ff = subprocess.Popen(["ffmpeg", "-v", "error", "-y", "-i", "pipe:0", "-i", apath, "-i", meta,
                                   "-map", "0:v:0", "-map", "1:a:0", "-map_metadata", "2", "-map_chapters", "2",
                                   "-c", "copy", "-metadata:s:a:0", "language=eng", dest],
                                  stdin=subprocess.PIPE, stdout=subprocess.DEVNULL, stderr=err)
            try:
                self.fetch(vf, ff.stdin.write, hook)
            except BrokenPipeError:
                pass                                            # ffmpeg stopped early: its exit code says why
            except BaseException:
                ff.kill()
                ff.wait()
                raise
            finally:
                try:
                    ff.stdin.close()
                except Exception:
                    pass
            rc = ff.wait(timeout=900)
        if rc:
            raise RuntimeError("stream mux failed (rc %s): %s" % (rc, open(errp, errors="replace").read()[-400:]))
        return vf, af

    def close(self):
        try: self.ydl.close()
        except Exception: pass


class Video:
    """Separate video/audio download with Tubarr's own format choice (choose_formats); used by catalog probes."""

    def __init__(self, video_id, workdir, subtitles=True, max_height=MAX_HEIGHT):
        self.id = video_id
        self.workdir = workdir
        self.subtitles = subtitles
        self.max_height = max_height
        self.ydl = yt_dlp.YoutubeDL(_params(format=FORMAT))
        self.info = None
        self._v = self._a = None

    def extract(self):
        try:
            self.info = self.ydl.extract_info("https://www.youtube.com/watch?v=" + self.id, download=False)
        except yt_dlp.utils.DownloadError as e:
            _guard(e)
        self._v, self._a = choose_formats(self.info, self.max_height)
        return self.info

    def chosen(self):
        """(video_format, audio_format) picked by choose_formats(), or (None, None)."""
        return self._v, self._a

    def download(self, dest=None, hook=None, _hls=False):
        """Video and audio as SEPARATE files (no merge): source.<id>.<ext> for each. Returns (video, audio, subs).
        A 403 on the direct https streams is retried once with the HLS variants of the same formats."""
        dest = dest or self.workdir
        v, a = self._v, self._a
        fmt = v["format_id"] + ("," + a["format_id"] if a else "")
        opts = _params(format=fmt, outtmpl={"default": os.path.join(dest, "source.%(format_id)s.%(ext)s"),
                                            "subtitle": os.path.join(self.workdir, "subs.%(ext)s")},
                       ratelimit=config.RATE_LIMIT or None, concurrent_fragment_downloads=3 if _hls else 1,
                       writesubtitles=self.subtitles, writeautomaticsub=False, subtitleslangs=EN_SUBS,
                       subtitlesformat="vtt/best", sleep_interval_subtitles=5, overwrites=False, continuedl=False,
                       progress_hooks=[hook] if hook else [])
        try:
            with yt_dlp.YoutubeDL(opts) as y:
                y.process_ie_result(self.info, download=True)
        except yt_dlp.utils.DownloadError as e:
            if "subtitle" in str(e).lower() and self.subtitles:
                log.warning("subtitle download failed (%s); retrying without subtitles", e)
                self.subtitles = False
                return self.download(dest, hook, _hls)
            if "403" in str(e) and not _hls:
                hv, ha = choose_formats(self.info, self.max_height, hls=True)
                if hv:
                    log.warning("%s: https stream gave 403; retrying with HLS %s", self.id, hv.get("format_id"))
                    for f in os.listdir(dest):
                        if f.startswith("source."):
                            os.remove(os.path.join(dest, f))
                    self._v, self._a = hv, (ha or a)
                    return self.download(dest, hook, _hls=True)
            _guard(e)
        vfile = next((os.path.join(dest, f) for f in os.listdir(dest) if f.startswith("source.%s." % v["format_id"])
                      and not f.endswith((".part", ".ytdl"))), None)
        afile = a and next((os.path.join(dest, f) for f in os.listdir(dest) if f.startswith("source.%s." % a["format_id"])
                            and not f.endswith((".part", ".ytdl"))), None)
        subs = sorted(os.path.join(self.workdir, f) for f in os.listdir(self.workdir) if f.endswith(".vtt"))
        if not vfile or (a and not afile):
            raise RuntimeError("download incomplete: %s" % os.listdir(dest))
        return vfile, afile, subs

    def close(self):
        try: self.ydl.close()
        except Exception: pass


def fetch_subtitles(video_id, workdir):
    """Subtitles only (no media): for videos whose English subtitles appear after they were downloaded."""
    opts = _params(skip_download=True, writesubtitles=True, writeautomaticsub=False, subtitleslangs=EN_SUBS,
                   subtitlesformat="vtt/best", sleep_interval_subtitles=5,
                   outtmpl={"default": os.path.join(workdir, "source.%(ext)s")})
    try:
        with yt_dlp.YoutubeDL(opts) as y:
            y.extract_info("https://www.youtube.com/watch?v=" + video_id, download=True)
    except yt_dlp.utils.DownloadError as e:
        _guard(e)
    return sorted(os.path.join(workdir, f) for f in os.listdir(workdir) if f.endswith(".vtt"))


def english_manual_subs(info):
    subs = info.get("subtitles") or {}
    return [k for k in subs if k.split("-")[0] == "en" and k != "live_chat"]


def skip_reason(info, min_duration=61):
    """(state, text) when a video should not be downloaded, else None.
    States: skipped_members_only | waiting (upcoming premiere/stream, retried after its start) | skipped."""
    av = info.get("availability")
    if av == "subscriber_only":
        return "skipped_members_only", "members only"
    if av in ("premium_only", "needs_auth"):
        return "skipped_members_only", "needs a login (%s)" % av
    ls = info.get("live_status")
    if ls == "is_upcoming":
        return "waiting", "upcoming premiere/stream"
    if ls in ("is_live", "post_live"):
        return "skipped", "live (%s)" % ls
    if ls == "was_live" or info.get("was_live"):
        return "skipped", "livestream replay"
    if (info.get("age_limit") or 0) >= 18:
        return "skipped", "age-restricted"
    if info.get("media_type") == "short":
        return "skipped", "Short"
    if (info.get("duration") or 0) < min_duration:
        return "skipped", "too short (%ss; Shorts rule)" % info.get("duration")
    if av not in (None, "public", "unlisted"):
        return "skipped", "availability=%s" % av
    return None


