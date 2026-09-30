"""Seasons inside each channel's show:
  * Series seasons 1..k: a channel's own REAL series playlists (see playlists.is_series). Season title = playlist
    name, episode number = position in the playlist (so Play / autoplay go Part 1 -> 2 -> 3). Season numbers are
    stable once given; a video in several series goes to the best one (the smallest numbered series containing it).
    Series moves happen right away: playlist positions don't change as more videos arrive.
  * Year seasons: the real year, titled "2026", episodes E1..En in upload order. While a channel's backlog is still
    filling, its year videos keep their provisional date-based numbers (S2026E092501); once the channel has settled
    (nothing in flight, and nothing queued or no new video for SETTLE_H hours) they're numbered 1..n once. After that
    numbers are stable: newer videos append (n+1...), pruning leaves gaps; only a video that lands in the MIDDLE of an
    already numbered year (rare: an older video downloaded later) renumbers that year again.
  * Every video appears exactly once under its channel. Moves are atomic renames on the same dataset (NFO first,
    sidecars, video last) under pipeline.FILE_LOCK (the pruner uses it too); Plex keeps watch state (the NFO
    uniqueid is the episode's identity, not the file name).
usage: python -m tubarr.seasons [--refresh-series] [--dry] [channel title ...]"""
import json
import logging
import math
import os
import re
import sys
import time
from datetime import date, datetime

import requests

from . import art, config, db, naming, nfo, pipeline, playlists, plexhttp

log = logging.getLogger("tubarr.seasons")
PH = lambda: {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}
SCHEMA = """CREATE TABLE IF NOT EXISTS series(channel_id TEXT, playlist_id TEXT, title TEXT, season_no INTEGER,
            entries TEXT, why TEXT, checked_at TEXT, PRIMARY KEY(channel_id, playlist_id));"""
SETTLE_H = 3


def _migrate(c):
    c.executescript(SCHEMA)
    if "numbers" not in {r[1] for r in c.execute("PRAGMA table_info(series)")}:
        c.execute("ALTER TABLE series ADD COLUMN numbers TEXT")
LIVE = re.compile(r"(?i)\blive(stream)?s?\b")


def _norm(t):
    t = re.sub(r"\(.*?\)|\[.*?\]", " ", t or "")
    return re.sub(r"[^a-z0-9]+", " ", t.lower()).strip()


def _words(t):
    return {w for w in _norm(t).split() if len(w) >= 3 or w.isdigit()}


def _title_hit(ptitle, title_words):
    """Is the playlist probably about some of our videos? (>= 75% of its name's words appear in one video title)"""
    w = _words(ptitle)
    if not w:
        return False
    need = max(1, math.ceil(0.75 * len(w)))
    return any(len(w & tw) >= need for tw in title_words)


def _num(x):
    try:
        return float(x)
    except (TypeError, ValueError):
        return 0.0


def _age_h(stamp):
    for fmt in ("%Y-%m-%dT%H:%M:%S%z", "%Y-%m-%dT%H:%M:%S"):
        try:
            t = datetime.strptime(stamp, fmt)
            return (time.time() - t.timestamp()) / 3600
        except (TypeError, ValueError):
            continue
    return 1e9


def refresh_series(c, ch):
    """Find the channel's real series playlists. Only playlists whose name matches one of its video titles are
    fetched (gentle: one request for the Playlists tab + one per matching playlist). Keeps existing season numbers."""
    _migrate(c)
    title_words = [set(_norm(r[0]).split()) for r in c.execute(
        "SELECT title FROM videos WHERE channel_id=? AND state NOT IN ('skipped','pruned','skipped_members_only')", (ch["id"],))]
    found = 0
    try:
        pls = playlists.channel_playlists(ch["id"])
    except Exception as e:
        if "does not have a playlists tab" not in str(e):
            raise
        pls = []                                        # no playlists at all: nothing to do until tomorrow
    for pid, ptitle, _n in pls:
        if LIVE.search(ptitle) or not _title_hit(ptitle, title_words):
            continue
        try:
            ents = playlists.playlist_entries(pid)
        except Exception as e:
            log.warning("playlist %s: %s", pid, e)
            continue
        ok, why = playlists.is_series(ch["id"], ptitle, ents)
        row = c.execute("SELECT season_no FROM series WHERE channel_id=? AND playlist_id=?", (ch["id"], pid)).fetchone()
        log.info("%s: playlist '%s' (%d videos): %s - %s", ch["title"], ptitle, len(ents), "SERIES" if ok else "no", why)
        if ok:
            sn = row[0] if row and row[0] else (c.execute("SELECT COALESCE(MAX(season_no),0)+1 FROM series WHERE channel_id=? "
                                                          "AND season_no IS NOT NULL", (ch["id"],)).fetchone()[0])
            ordered, numbers = playlists.logical_order(ents)
            c.execute("INSERT OR REPLACE INTO series(channel_id, playlist_id, title, season_no, entries, why, checked_at, numbers) "
                      "VALUES(?,?,?,?,?,?,?,?)", (ch["id"], pid, ptitle, sn, json.dumps([e["id"] for e in ordered]), why,
                                                  db.now(), json.dumps(numbers)))
            found += 1
        elif row:
            c.execute("UPDATE series SET entries=?, why=?, checked_at=? WHERE channel_id=? AND playlist_id=?",
                      (json.dumps([e["id"] for e in ents]), "no longer a series: " + why, db.now(), ch["id"], pid))
        time.sleep(1)
    db.upsert(c, "channels", ch["id"], series_checked_at=db.now())
    return found


def series_of(c, channel_id):
    _migrate(c)
    out = []
    for pid, title, sn, ents, why, nums in c.execute("SELECT playlist_id, title, season_no, entries, why, numbers FROM series "
                                                     "WHERE channel_id=? ORDER BY season_no", (channel_id,)):
        if why and why.startswith("no longer"):
            continue
        out.append({"pid": pid, "title": title, "season": sn, "entries": json.loads(ents or "[]"),
                    "numbers": json.loads(nums or "{}")})
    return out


def series_ep(s, vid):
    """Episode number inside a series season: the title's own number (Ep03 -> 3) when the whole playlist is
    numbered, else the position in the playlist (oldest part first)."""
    return s["numbers"].get(vid) or s["entries"].index(vid) + 1


def unmatched_series_titles(c, ch):
    """Done videos whose title matches a known series but that aren't in its stored entries yet (a new part was
    uploaded since the last refresh) -> the series list should be refreshed now rather than tomorrow."""
    ser = series_of(c, ch["id"])
    if not ser:
        return False
    known = set()
    for s in ser:
        known |= set(s["entries"])
    for (vid, title) in c.execute("SELECT id, title FROM videos WHERE channel_id=? AND state='done'", (ch["id"],)):
        if vid in known:
            continue
        tw = set(_norm(title).split())
        if any(_title_hit(s["title"], [tw]) for s in ser):
            return True
    return False


def settled(c, ch):
    """Has the channel's fill calmed down enough to number its year seasons 1..n?"""
    cid = (ch["id"],)
    if c.execute("SELECT 1 FROM videos WHERE channel_id=? AND state IN ('downloading','processing','upgrading')", cid).fetchone():
        return False
    if not c.execute("SELECT 1 FROM videos WHERE channel_id=? AND state IN ('queued','upgrade','retry') AND COALESCE(attempts,0) < 3",
                     cid).fetchone():
        return True
    last = c.execute("SELECT MAX(finished_at) FROM videos WHERE channel_id=? AND state='done'", cid).fetchone()[0]
    return not last or _age_h(last) >= SETTLE_H


def _prov_number(d, used):
    base = (d.month * 100 + d.day) * 100
    for i in range(1, 100):
        if base + i not in used:
            return base + i
    raise ValueError("more than 99 uploads on %s" % d)


def plan(c, ch, is_settled=None):
    """{video_id: (season, episode, folder_name, season_title)} for every done video of the channel."""
    ser = series_of(c, ch["id"])
    vids = c.execute("SELECT * FROM videos WHERE channel_id=? AND state='done' AND path IS NOT NULL", (ch["id"],)).fetchall()
    if is_settled is None:
        is_settled = settled(c, ch)
    out, years = {}, {}
    for v in vids:
        best = next((s for s in ser if v["id"] in s["entries"]), None)
        if best:
            out[v["id"]] = (best["season"], series_ep(best, v["id"]), naming.sanitize(best["title"], 90), best["title"])
        else:
            years.setdefault(int((v["local_date"] or "1970")[:4]), []).append(v)
    key = lambda v: (v["local_date"] or "", _num(v["published_at"]), v["id"])
    for y, vs in years.items():
        numbered = [v for v in vs if v["season"] == y and v["episode"] and v["episode"] < 10000]
        prov = [v for v in vs if not (v["season"] == y and v["episode"] and v["episode"] < 10000)]
        nums = {}
        if prov and is_settled:
            if numbered and min(map(key, prov)) > max(map(key, numbered)):      # newer videos: append
                n = max(v["episode"] for v in numbered)
                nums = {v["id"]: v["episode"] for v in numbered}
                for v in sorted(prov, key=key):
                    n += 1
                    nums[v["id"]] = n
            else:                                                              # first numbering, or an insertion
                nums = {v["id"]: i for i, v in enumerate(sorted(vs, key=key), 1)}
        else:
            nums = {v["id"]: v["episode"] for v in numbered}
            dated = [v for v in prov if v["season"] == y and (v["episode"] or 0) >= 10000]
            used = {v["episode"] for v in dated}
            for v in prov:
                if v in dated:
                    nums[v["id"]] = v["episode"]
                else:                          # back from a series season: a provisional date-based number
                    e = _prov_number(date.fromisoformat(v["local_date"]), used)
                    used.add(e)
                    nums[v["id"]] = e
        for v in vs:
            out[v["id"]] = (y, nums[v["id"]], naming.season_folder(y), str(y))
    return out


def code_for(season, ep):
    return ("S%02d" % season if season < 1900 else "S%04d" % season) + ("E%06d" % ep if ep >= 10000 else "E%02d" % ep)


SIDE = (".nfo", ".jpg", ".en.srt")


def _move(src_base, dst_dir, dst_base, video_ext):
    os.makedirs(dst_dir, exist_ok=True)
    for ext in SIDE + (video_ext,):     # sidecars first, video last; each rename is atomic
        s = src_base + ext
        if os.path.exists(s):
            os.replace(s, os.path.join(dst_dir, dst_base + ext))


def placement(c, ch, vid, d):
    """Where a video being published right now goes -> (season, episode, folder, season_title), or None for the
    provisional date-based number. Called by the finalizer under pipeline.FILE_LOCK."""
    for s in series_of(c, ch["id"]):
        if vid in s["entries"]:
            return s["season"], series_ep(s, vid), naming.sanitize(s["title"], 90), s["title"]
    y = d.year
    row = c.execute("SELECT season, episode FROM videos WHERE id=?", (vid,)).fetchone()
    if row and row["season"] == y and row["episode"] and row["episode"] < 10000:
        return y, row["episode"], naming.season_folder(y), str(y)                 # an upgrade keeps its number
    top = c.execute("SELECT MAX(episode), MAX(local_date) FROM videos WHERE channel_id=? AND state='done' AND season=? "
                    "AND episode < 10000 AND id<>?", (ch["id"], y, vid)).fetchone()
    if top[0] and d.isoformat() >= (top[1] or ""):
        return y, top[0] + 1, naming.season_folder(y), str(y)                    # newer than the numbered ones: append
    return None


def playing_paths():
    """Library paths being played in Plex right now (never moved while playing); None if Plex can't be asked."""
    if not config.plex_enabled():
        return set()
    try:
        r = plexhttp.get(config.PLEX_URL + "/status/sessions", headers=PH(), timeout=15).json()
    except Exception as e:
        log.warning("Plex sessions check failed (%s): no moves this round", e)
        return None
    out = set()
    for m in r["MediaContainer"].get("Metadata", []):
        for med in m.get("Media", []):
            for part in med.get("Part", []):
                f = part.get("file", "")
                if f.startswith(config.PLEX_ROOT):
                    out.add(config.ROOT + f[len(config.PLEX_ROOT):])
    return out


def apply(c, ch, dry=False):
    """Move files to their planned season/episode; returns [(title, old relative path, new relative path)]."""
    show_dir = pipeline.show_dir(ch["folder"])
    changes, sources = [], set()
    playing = set() if dry else playing_paths()
    if playing is None:
        return changes
    with pipeline.FILE_LOCK:                                     # plan + moves atomic w.r.t. finalize and prune
        moves = []
        for vid, (season, ep, folder, stitle) in plan(c, ch).items():
            v = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
            ext = os.path.splitext(v["path"])[1]
            new_base = "%s - %s - %s" % (ch["folder"], code_for(season, ep), naming.sanitize(v["title"], 140))
            new_path = os.path.join(show_dir, folder, new_base + ext)
            if new_path != v["path"] and v["path"] not in playing:
                moves.append((vid, season, ep, new_path))
        if dry:
            for vid, season, ep, new_path in moves:
                v = c.execute("SELECT title, path FROM videos WHERE id=?", (vid,)).fetchone()
                changes.append((v["title"], v["path"][len(show_dir) + 1:], new_path[len(show_dir) + 1:]))
            return changes
        # a target name can be another moving video's current name (same title, numbers swapped): park those first
        current = {r["path"]: r["id"] for r in c.execute("SELECT id, path FROM videos WHERE channel_id=? AND state='done'", (ch["id"],))}
        keep = []
        for vid, season, ep, new_path in moves:
            other = current.get(new_path)
            if other and other != vid:
                o = c.execute("SELECT path FROM videos WHERE id=?", (other,)).fetchone()["path"]
                if o in playing:
                    continue                                     # its occupant is playing: next round
                ob, oext = os.path.splitext(o)
                park = os.path.join(os.path.dirname(o), ".renumber-" + other)
                _move(ob, os.path.dirname(o), ".renumber-" + other, oext)
                db.upsert(c, "videos", other, path=park + oext)
                current.pop(o, None)
                current[park + oext] = other
            keep.append((vid, season, ep, new_path))
        for vid, season, ep, new_path in keep:
            v = c.execute("SELECT * FROM videos WHERE id=?", (vid,)).fetchone()
            if v["state"] != "done" or not v["path"] or not os.path.exists(v["path"]):
                continue                                         # pruned or changed meanwhile
            if os.path.exists(new_path):
                log.warning("%s: target exists, not moving %s -> %s", ch["title"], v["path"], new_path)
                continue
            rep = json.loads(v["report"] or "{}")
            old_base, ext = os.path.splitext(v["path"])
            if os.path.exists(old_base + ".nfo"):                # NFO first, with the new numbers
                nfo.episode(old_base + ".nfo", vid, v["title"], v["summary"] or rep.get("summary") or "", v["local_date"],
                            season, ep, ch["title"], v["out_duration"], ch["topic"] or rep.get("category"),
                            tags=["Removed from YouTube"] if v["removed_at"] else ())
            new_dir, new_base = os.path.dirname(new_path), os.path.splitext(os.path.basename(new_path))[0]
            _move(old_base, new_dir, new_base, ext)
            db.upsert(c, "videos", vid, path=new_path, season=season, episode=ep)
            sources.add(os.path.dirname(v["path"]))
            shown = v["path"][len(show_dir) + 1:]
            changes.append((v["title"], shown if not os.path.basename(shown).startswith(".renumber-") else "(renumbered)",
                            new_path[len(show_dir) + 1:]))
        for p in sources:                                        # season folders the moves left without videos
            if os.path.isdir(p) and p != show_dir and not any(f.endswith((".mkv", ".mp4")) for f in os.listdir(p)):
                for f in os.listdir(p):
                    os.remove(os.path.join(p, f))
                os.rmdir(p)
    return changes


def season_art(c, ch):
    """Series seasons get a poster with the series name; year seasons keep the year poster."""
    a = pipeline._art_for(ch)
    show_dir = pipeline.show_dir(ch["folder"])
    made = 0
    for s in series_of(c, ch["id"]):
        d = os.path.join(show_dir, naming.sanitize(s["title"], 90))
        if os.path.isdir(d) and not os.path.exists(os.path.join(d, "poster.jpg")):
            im = a.poster(series=s["title"])
            art.save_jpg(im, os.path.join(d, "poster.jpg"))
            art.save_jpg(im, os.path.join(show_dir, "Season%02d.jpg" % s["season"]))
            made += 1
    return made


def plex_titles(c, ch, wait=90):
    """After a partial scan: title every season ('2026' or the series name) and set the series posters."""
    if not config.plex_enabled():
        return
    key = next(s["key"] for s in plexhttp.get(config.PLEX_URL + "/library/sections", headers=PH(), timeout=20).json()
               ["MediaContainer"]["Directory"] if s["title"] == config.PLEX_SECTION)
    show_dir = pipeline.show_dir(ch["folder"])
    plexhttp.get("%s/library/sections/%s/refresh" % (config.PLEX_URL, key), headers=PH(), timeout=20,
                 params={"path": config.PLEX_ROOT + show_dir[len(config.ROOT):]})
    ser = {s["season"]: s["title"] for s in series_of(c, ch["id"])
           if os.path.isdir(os.path.join(show_dir, naming.sanitize(s["title"], 90)))}
    t0, rk = time.time(), None
    while time.time() - t0 < wait:
        time.sleep(5)
        r = plexhttp.get("%s/library/sections/%s/all" % (config.PLEX_URL, key), params={"type": 2, "title": ch["title"]},
                         headers=PH(), timeout=20).json()
        m = [x for x in r["MediaContainer"].get("Metadata", []) if x["title"] == ch["title"]]
        if m:
            rk = m[0]["ratingKey"]
            seasons = plexhttp.get("%s/library/metadata/%s/children" % (config.PLEX_URL, rk), headers=PH(), timeout=20).json()
            idx = {s.get("index") for s in seasons["MediaContainer"].get("Metadata", [])}
            if all(sn in idx for sn in ser):
                break
    if not rk:
        return
    seasons = plexhttp.get("%s/library/metadata/%s/children" % (config.PLEX_URL, rk), headers=PH(), timeout=20).json()
    for s in seasons["MediaContainer"].get("Metadata", []):
        i = s.get("index")
        want = ser.get(i) or (str(i) if isinstance(i, int) and 1900 < i < 2200 else None)
        if want and s.get("title") != want:
            plexhttp.put("%s/library/sections/%s/all" % (config.PLEX_URL, key), headers=PH(), timeout=20,
                         params={"type": 3, "id": s["ratingKey"], "title.value": want, "title.locked": 1})
        if i in ser:
            p = os.path.join(show_dir, "Season%02d.jpg" % i)
            if os.path.exists(p):
                plexhttp.post("%s/library/metadata/%s/posters" % (config.PLEX_URL, s["ratingKey"]), headers=PH(),
                              timeout=30, data=open(p, "rb").read())
                plexhttp.put("%s/library/metadata/%s" % (config.PLEX_URL, s["ratingKey"]), headers=PH(), timeout=20,
                             params={"type": 3, "id": s["ratingKey"], "thumb.locked": 1})


def organize_channel(c, ch, refresh=False, dry=False):
    """Series refresh when due (daily; after 6 h if a new video looks like part of a known series), then moves."""
    _migrate(c)
    age = _age_h(ch["series_checked_at"]) if ch["series_checked_at"] else 1e9
    if refresh or age >= 24 or (age >= 6 and unmatched_series_titles(c, ch)):
        try:
            n = refresh_series(c, ch)
            log.info("%s: %d series playlists", ch["title"], n)
        except Exception as e:
            log.warning("%s: playlists failed: %s", ch["title"], e)
    changes = apply(c, ch, dry=dry)
    if dry:
        return changes
    new_art = season_art(c, ch)             # also covers series seasons the finalizer created directly
    if changes or new_art:
        plex_titles(c, ch)
    return changes


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    c = db.connect()
    _migrate(c)
    titles = [a for a in sys.argv[1:] if not a.startswith("--")]
    rows = c.execute("SELECT * FROM channels WHERE gone_at IS NULL ORDER BY csv_order").fetchall()
    for ch in rows:
        if titles and ch["title"] not in titles:
            continue
        chg = organize_channel(c, c.execute("SELECT * FROM channels WHERE id=?", (ch["id"],)).fetchone(),
                               refresh="--refresh-series" in sys.argv, dry="--dry" in sys.argv)
        print("%s: settled=%s, %d changes" % (ch["title"], settled(c, ch), len(chg)))
        for t, a, b in chg:
            print("  %s\n      %s\n   -> %s" % (t, a, b))


if __name__ == "__main__":
    main()
