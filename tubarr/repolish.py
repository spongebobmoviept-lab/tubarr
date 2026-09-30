"""Re-polish pass: better text and art for what's already in the library. NEVER re-downloads video.
  python -m tubarr.repolish [--art] [--all] [--no-plex] [-- channel_id ...]
- Show summaries: the channel's About text, cleaned by rules only (NO AI; name-based fallback), tvshow.nfo rewritten
  (with the channel's YouTube category as genre).
- Episode summaries: the video's own description, cleaned by rules only (--revert-ai: only rows an old AI pass wrote; --all: every video); the
  episode .nfo is rewritten. Videos downloaded before descriptions were stored get one metadata-only lookup.
- --art: regenerate poster, background and season posters with the current design.
- Plex: every changed show / episode gets a metadata refresh, so the NFO agent re-reads the NFO and local art.
With the local-only "Plex NFO Series" agent the NFO files are the source of truth; fields are NOT locked, so a later
re-polish (or an edit in the Tubarr UI) can still update them."""
import json
import logging
import os
import re
import sys
import time

import requests
import yt_dlp

from . import art, config, db, nfo, pipeline, plexhttp, summarize, ytdl

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
log = logging.getLogger("tubarr.repolish")
PH = {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}


def plex_maps():
    """(show folder -> ratingKey, file path -> ratingKey) for the YouTube section, in Tubarr's own paths."""
    r = plexhttp.get(config.PLEX_URL + "/library/sections", headers=PH, timeout=20).json()
    key = next(s["key"] for s in r["MediaContainer"]["Directory"] if s["title"] == config.PLEX_SECTION)
    shows, eps = {}, {}
    r = plexhttp.get("%s/library/sections/%s/all" % (config.PLEX_URL, key), params={"type": 4}, headers=PH, timeout=60).json()
    for ep in r["MediaContainer"].get("Metadata", []):
        for m in ep.get("Media", []):
            for p in m.get("Part", []):
                f = p.get("file", "")
                if f.startswith(config.PLEX_ROOT):
                    local = config.ROOT + f[len(config.PLEX_ROOT):]
                    eps[local] = ep["ratingKey"]
                    shows[os.path.dirname(os.path.dirname(local))] = ep.get("grandparentRatingKey")
    return shows, eps


def refresh(rk):
    try:
        plexhttp.put("%s/library/metadata/%s/refresh" % (config.PLEX_URL, rk), headers=PH, timeout=30)
    except Exception as e:
        log.warning("refresh %s: %s", rk, e)


def description_of(vid):
    """Metadata-only lookup (no download) for videos processed before descriptions were stored."""
    try:
        with yt_dlp.YoutubeDL(ytdl._params(skip_download=True)) as y:
            info = y.extract_info("https://www.youtube.com/watch?v=" + vid, download=False, process=False)
        return info.get("description") or "", [c["title"] for c in (info.get("chapters") or [])], (info.get("categories") or [None])[0]
    except Exception as e:
        log.warning("description lookup %s: %s", vid, e)
        return "", [], None


def parse_args(argv):
    """-> (options, channel ids). Options come first; channel ids follow a "--" (a bare UC... id before it is accepted
    too). Anything else is ignored, so a channel title can never turn into an option."""
    opts, ids, rest = set(), [], False
    for a in argv:
        if rest:
            if re.fullmatch(r"UC[\w-]{22}", a):
                ids.append(a)
        elif a == "--":
            rest = True
        elif a.startswith("--"):
            opts.add(a)
        elif re.fullmatch(r"UC[\w-]{22}", a):
            ids.append(a)
    return opts, ids


def main():
    opts, args = parse_args(sys.argv[1:])
    if "--" in sys.argv[1:] and not args:
        log.warning("no valid channel id after --; nothing to do")          # never fall through to "every channel"
        return
    do_art, do_all, do_plex = "--art" in opts, "--all" in opts, "--no-plex" not in opts
    revert = "--revert-ai" in opts                      # only rows whose text came from the old LLM pass
    c = db.connect()
    chans = c.execute("SELECT * FROM channels" + (" WHERE id IN (%s)" % ",".join("?" * len(args)) if args else ""), args).fetchall()
    shows, eps = plex_maps() if do_plex else ({}, {})
    changed_shows, changed_eps = set(), set()
    for ch in chans:
        d = pipeline.show_dir(ch["folder"])
        if not os.path.isdir(d):
            continue
        vids = c.execute("SELECT * FROM videos WHERE channel_id=? AND state='done'", (ch["id"],)).fetchall()
        if not vids:                       # polished once the channel has videos (category known)
            continue
        cat = ch["category"] or next((json.loads(v["report"] or "{}").get("category") for v in vids
                                      if json.loads(v["report"] or "{}").get("category")), None)
        if cat and cat != ch["category"]:
            db.upsert(c, "channels", ch["id"], category=cat)
        edits = (json.loads(ch["meta"]) if ch["meta"] and ch["meta"].startswith("{") else {}).get("edits") or {}
        if revert and ch["summary_source"] != "llm" and not do_art:
            pass
        elif (do_all or revert or not ch["summary"]) and not edits.get("summary"):
            plot, src = summarize.show(ch["title"], ch["description"] or "")
            if not plot:
                plot, src = "%s on YouTube." % ch["title"], "name"
            db.upsert(c, "channels", ch["id"], summary=plot, summary_source=src)
            log.info("show %s: summary from %s", ch["title"], src)
        ch = c.execute("SELECT * FROM channels WHERE id=?", (ch["id"],)).fetchone()
        nfo.tvshow(os.path.join(d, "tvshow.nfo"), ch["id"], edits.get("title") or ch["title"], edits.get("summary") or ch["summary"],
                   genre=edits.get("genre") or ch["topic"] or ch["category"], handle=ch["handle"])
        changed_shows.add(d)
        if do_art:
            pipeline._ART.pop(ch["id"], None)
            a = pipeline._art_for(ch)
            art.save_jpg(a.poster(), os.path.join(d, "poster.jpg"))
            if '"bg_sig"' not in (ch["meta"] or ""):        # keep the thumbnail-mosaic background (organize.show_meta)
                art.save_jpg(a.background(), os.path.join(d, "fanart.jpg"))
            for y in sorted({v["season"] for v in vids if v["season"]}):
                pipeline.ensure_season(c, ch, y, force=True)
            db.upsert(c, "channels", ch["id"], art_updated_at=db.now())
            log.info("show %s: art regenerated", ch["title"])
        for v in vids:
            if "--subs" in opts and v["path"] and ("--subs-redo" in opts
                                                        or not os.path.exists(os.path.splitext(v["path"])[0] + ".en.srt")):
                try:
                    res = pipeline.add_subtitles(c, v["id"])
                    log.info("episode %s | %s: subtitles %s", ch["title"], v["title"], res)
                    if res:
                        changed_eps.add(v["path"])
                except Exception as e:
                    log.warning("subtitles %s: %s", v["id"], e)
            if not (do_all or (revert and v["summary_source"] == "llm")) or not v["path"]:
                continue
            rep = json.loads(v["report"] or "{}")
            if rep.get("edited_summary"):                   # text edited in Tubarr wins
                continue
            desc, chaps, vcat = (v["description"], [], None) if v["description"] else description_of(v["id"])
            s, src = summarize.episode(v["title"], ch["title"], desc, chaps)
            db.upsert(c, "videos", v["id"], summary=s, summary_source=src, description=desc or v["description"])
            base = os.path.splitext(v["path"])[0]
            with pipeline.FILE_LOCK:
                v2 = c.execute("SELECT path, season, episode FROM videos WHERE id=?", (v["id"],)).fetchone()
                base = os.path.splitext(v2["path"])[0]
                if os.path.exists(base + ".nfo"):
                    nfo.episode(base + ".nfo", v["id"], rep.get("edited_title") or v["title"], s, v["local_date"],
                                v2["season"], v2["episode"], ch["title"], v["out_duration"], ch["topic"] or ch["category"] or vcat,
                                tags=["Removed from YouTube"] if v["removed_at"] else ())
            changed_eps.add(v2["path"])
            log.info("episode %s | %s: summary from %s", ch["title"], v["title"], src)
    if do_plex:
        for d in changed_shows:
            if shows.get(d):
                refresh(shows[d])
        for p in changed_eps:
            if eps.get(p):
                refresh(eps[p])
        log.info("Plex refresh requested: %d shows, %d episodes", len([d for d in changed_shows if shows.get(d)]),
                 len([p for p in changed_eps if eps.get(p)]))


if __name__ == "__main__":
    main()
