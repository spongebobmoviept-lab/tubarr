"""Plex organization pass for the YouTube library (safe to re-run; the worker runs it periodically).
  * Topic per channel = YouTube's OWN category for it (the most common category of its videos; no AI anywhere):
    stored in the DB and tvshow.nfo (genre), applied in Plex as the show's genre + a collection tag.
  * Topic collections get a generated poster (mosaic of the topic's channels) and are promoted to the YouTube
    library's Recommended tab. Library collectionMode=0: the main grid keeps showing EVERY channel.
  * Year seasons are titled "2026", "2025", ... (instead of "Season 2026").
usage: python -m tubarr.organize [--show-meta [--force] [channel ...]]"""
import io
import json
import logging
import os
import re
import sys
import time

import requests

from . import art, config, db, nfo, pipeline, plexhttp

log = logging.getLogger("tubarr.organize")
OLD_TOPICS = {"Cars & Builds", "Tech & PCs", "Science & Engineering", "Space", "Aviation", "History & Stories",
              "Makers & DIY"}                          # the earlier AI-assigned topic collections (removed when empty)
TOPICS = []                                            # topics are now YouTube's own category names
PH = lambda: {"X-Plex-Token": config.PLEX_TOKEN, "Accept": "application/json"}

RULES = [  # (topic, regex over "title | category | about")
    ("Music", r"\b(music|lofi|lo-fi|beats|chill|mix(es)?|playlist|songs?|relax)\b"),
    ("Aviation", r"\b(pilot|aviation|aircraft|airline|flight|plane|boeing|airbus|cockpit)\b"),
    ("Space", r"\b(space|astro\w*|nasa|rocket|planet|galax\w*|cosmos|universe|star(s)?)\b"),
    ("Gaming", r"\b(gaming|gamer|game(s|play)?|rust|minecraft|factorio|esports|let'?s play|twitch|streamer)\b"),
    ("Cars & Builds", r"\b(car|cars|truck|engine|motor\w*|turbo|race|racing|drift|garage|horsepower|hp|jdm|mechanic|auto(motive)?|vehicle|dirt ?bike|restor\w*)\b"),
    ("Tech & PCs", r"\b(pc|pcs|gpu|cpu|computer|tech|hardware|linux|network\w*|server|homelab|phone|iphone|apple|laptop|electronics|repair)\b"),
    ("History & Stories", r"\b(history|historic\w*|ancient|war|empire|stories|story|mystery|mysteries|crime|documentar\w*|scary|disaster)\b"),
    ("Makers & DIY", r"\b(diy|maker|build(s|ing)?|fabricat\w*|weld\w*|workshop|woodwork\w*|machin\w*|3d print\w*|plumb\w*|electrician|craft)\b"),
    ("Science & Engineering", r"\b(science|engineer\w*|physics|chemistry|experiment\w*|lab|research|education|math)\b"),
]
CATEGORY = {"Autos & Vehicles": "Cars & Builds", "Gaming": "Gaming", "Music": "Music"}


def rule_topic(ch):
    text = "%s | %s | %s" % (ch["title"], ch["category"] or "", (ch["description"] or "")[:600])
    if ch["category"] in CATEGORY:
        return CATEGORY[ch["category"]]
    for topic, pat in RULES:
        if re.search(pat, text, re.I):
            return topic
    return "Other"


def section():
    r = plexhttp.get(config.PLEX_URL + "/library/sections", headers=PH(), timeout=20).json()
    return next(s for s in r["MediaContainer"]["Directory"] if s["title"] == config.PLEX_SECTION)


def plex_shows(key):
    """{show folder (Tubarr path): (ratingKey, show dict)} for the YouTube section."""
    out = {}
    r = plexhttp.get("%s/library/sections/%s/all" % (config.PLEX_URL, key), params={"type": 4}, headers=PH(), timeout=60).json()
    for ep in r["MediaContainer"].get("Metadata", []):
        for m in ep.get("Media", []):
            for p in m.get("Part", []):
                f = p.get("file", "")
                if f.startswith(config.PLEX_ROOT):
                    folder = os.path.dirname(os.path.dirname(config.ROOT + f[len(config.PLEX_ROOT):]))
                    out[folder] = ep.get("grandparentRatingKey")
    return out


def _meta(ch):
    try:
        m = json.loads(ch["meta"] or "{}")
        return m if isinstance(m, dict) else {}
    except Exception:
        return {}


def show_meta(c, key, shows, by_folder, force_bg=False):
    """Summary (your edit, else the channel summary; locked only when edited in Tubarr) and background art."""
    r = plexhttp.get("%s/library/sections/%s/all" % (config.PLEX_URL, key), params={"type": 2}, headers=PH(), timeout=60).json()
    plex = {str(s["ratingKey"]): s for s in r["MediaContainer"].get("Metadata", [])}
    n_sum = n_bg = 0
    for folder, rk in shows.items():
        ch = by_folder.get(folder)
        if not ch or not rk:
            continue
        m = _meta(ch)
        edits = m.get("edits") or {}
        want = (edits.get("summary") or ch["summary"] or "").strip()
        if want and ((plex.get(str(rk)) or {}).get("summary") or "").strip() != want:
            plexhttp.put("%s/library/metadata/%s" % (config.PLEX_URL, rk), headers=PH(), timeout=20,
                         params={"type": 2, "id": rk, "summary.value": want, "summary.locked": 1 if "summary" in edits else 0})
            n_sum += 1
        rows = c.execute("SELECT id, path FROM videos WHERE channel_id=? AND state='done' AND path IS NOT NULL "
                         "ORDER BY local_date DESC, published_at DESC LIMIT 4", (ch["id"],)).fetchall()
        thumbs = [os.path.splitext(x["path"])[0] + ".jpg" for x in rows if os.path.exists(os.path.splitext(x["path"])[0] + ".jpg")]
        sig = ",".join(x["id"] for x in rows)
        if not thumbs or (sig == m.get("bg_sig") and not force_bg):
            continue
        im = art.thumbs_background(thumbs)
        if im is None:
            continue
        d = pipeline.show_dir(ch["folder"])
        tmp = os.path.join(d, ".fanart.jpg.part")
        im.save(tmp, "JPEG", quality=90, optimize=True)
        os.replace(tmp, os.path.join(d, "fanart.jpg"))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=90)
        plexhttp.post("%s/library/metadata/%s/arts" % (config.PLEX_URL, rk), headers=PH(), timeout=60, data=buf.getvalue())
        plexhttp.put("%s/library/metadata/%s" % (config.PLEX_URL, rk), headers=PH(), timeout=20,
                     params={"type": 2, "id": rk, "art.locked": 1})
        c.execute("UPDATE channels SET meta=json_set(CASE WHEN json_valid(meta) THEN meta ELSE '{}' END, '$.bg_sig', ?) "
                  "WHERE id=?", (sig, ch["id"]))
        n_bg += 1
    log.info("show summaries pushed: %d, backgrounds from recent thumbnails: %d", n_sum, n_bg)


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    if "--show-meta" in sys.argv:                      # summaries + backgrounds only
        c = db.connect()
        sec = section()
        key = sec["key"]
        only = [a for a in sys.argv[1:] if not a.startswith("--")]
        chans = [ch for ch in c.execute("SELECT * FROM channels").fetchall() if not only or ch["title"] in only]
        by_folder = {pipeline.show_dir(ch["folder"]): ch for ch in chans}
        show_meta(c, key, {f: rk for f, rk in plex_shows(key).items() if f in by_folder}, by_folder,
                  force_bg="--force" in sys.argv)
        return
    run(db.connect(), topics_redo="--topics-redo" in sys.argv, use_llm="--no-llm" not in sys.argv)


def youtube_category(c, ch):
    """The channel's most common YouTube category over its downloaded videos (ties: the channel's own category)."""
    counts = {}
    for (rep,) in c.execute("SELECT report FROM videos WHERE channel_id=? AND state='done'", (ch["id"],)):
        try:
            cat = json.loads(rep or "{}").get("category")
        except Exception:
            cat = None
        if cat:
            counts[cat] = counts.get(cat, 0) + 1
    if counts:
        best = max(counts.values())
        tops = [k for k, v in counts.items() if v == best]
        return ch["category"] if ch["category"] in tops else sorted(tops)[0]
    return ch["category"] or "Other"


def run(c, topics_redo=False, use_llm=False):
    chans = c.execute("SELECT * FROM channels ORDER BY csv_order").fetchall()
    # ---- 1. topics: YouTube's own category, deterministic (no AI); updated whenever it changes
    changed = 0
    for ch in chans:
        t = youtube_category(c, ch)
        if t != ch["topic"]:
            db.upsert(c, "channels", ch["id"], topic=t)
            changed += 1
    if changed:
        log.info("topics (YouTube categories) updated: %d channels", changed)
    chans = c.execute("SELECT * FROM channels ORDER BY csv_order").fetchall()
    for ch in chans:                                   # genre = topic in tvshow.nfo (a fresh scan keeps it)
        d = pipeline.show_dir(ch["folder"])
        if os.path.isdir(d):
            nfo.tvshow(os.path.join(d, "tvshow.nfo"), ch["id"], ch["title"], ch["summary"], genre=ch["topic"], handle=ch["handle"])
    # ---- 2. Plex: library prefs, show genre + collection tag, season titles
    if not config.plex_enabled():
        return
    sec = section()
    key, agent = sec["key"], sec["agent"]
    plexhttp.put("%s/library/sections/%s" % (config.PLEX_URL, key), headers=PH(), timeout=20,
                 params={"agent": agent, "prefs[collectionMode]": 0})
    shows = plex_shows(key)
    by_folder = {pipeline.show_dir(ch["folder"]): ch for ch in chans}
    tagged = 0
    for folder, rk in shows.items():
        ch = by_folder.get(folder)
        if not ch or not rk:
            continue
        plexhttp.put("%s/library/metadata/%s" % (config.PLEX_URL, rk), headers=PH(), timeout=20,
                     params={"type": 2, "id": rk, "collection[0].tag.tag": ch["topic"], "collection.locked": 1,
                             "genre[0].tag.tag": ch["topic"], "genre.locked": 1})
        db.upsert(c, "channels", ch["id"], plex_rating_key=str(rk))
        tagged += 1
        seasons = plexhttp.get("%s/library/metadata/%s/children" % (config.PLEX_URL, rk), headers=PH(), timeout=20).json()
        for s in seasons["MediaContainer"].get("Metadata", []):
            idx = s.get("index")
            if isinstance(idx, int) and 1900 < idx < 2200 and s.get("title") != str(idx):
                plexhttp.put("%s/library/sections/%s/all" % (config.PLEX_URL, key), headers=PH(), timeout=20,
                             params={"type": 3, "id": s["ratingKey"], "title.value": str(idx), "title.locked": 1})
    log.info("shows tagged with topic collection + genre: %d", tagged)
    # ---- 2b. every show gets its summary (the NFO is only read when Plex first creates the show) and a background
    #          made from its own newest episode thumbnails (regenerated when the newest 4 change)
    try:
        show_meta(c, key, shows, by_folder)
    except Exception as e:
        log.warning("show summaries/backgrounds: %s", e)
    # ---- 3. collections: poster, summary, promote to the YouTube library's Recommended tab
    time.sleep(3)
    cols = plexhttp.get("%s/library/sections/%s/collections" % (config.PLEX_URL, key), headers=PH(), timeout=20).json()
    managed = {}
    try:
        mh = plexhttp.get("%s/hubs/sections/%s/manage" % (config.PLEX_URL, key), headers=PH(), timeout=20).json()
        managed = {h.get("identifier"): h for h in mh["MediaContainer"].get("Hub", [])}
    except Exception:
        pass
    topics_now = {ch["topic"] for ch in chans if ch["topic"]}
    for col in cols["MediaContainer"].get("Metadata", []):
        topic = col.get("title")
        if topic not in topics_now:
            if topic in OLD_TOPICS:                        # an old AI-era topic collection: remove it
                plexhttp.delete("%s/library/metadata/%s" % (config.PLEX_URL, col["ratingKey"]), headers=PH(), timeout=20)
                log.info("collection %s removed (old topic)", topic)
            continue
        members = [ch for ch in chans if ch["topic"] == topic and pipeline.show_dir(ch["folder"]) in shows]
        avatars = []
        for ch in members[:9]:
            try:
                avatars.append(art.fetch(ch["avatar_url"]))
            except Exception:
                pass
        im = art.topic_poster(topic, avatars, len(members))
        buf = io.BytesIO()
        im.save(buf, "JPEG", quality=92)
        plexhttp.post("%s/library/metadata/%s/posters" % (config.PLEX_URL, col["ratingKey"]), headers=PH(), timeout=60,
                      data=buf.getvalue())
        plexhttp.put("%s/library/metadata/%s" % (config.PLEX_URL, col["ratingKey"]), headers=PH(), timeout=20,
                     params={"type": 18, "id": col["ratingKey"], "thumb.locked": 1,
                             "summary.value": "%s on YouTube: %s." % (topic, ", ".join(ch["title"] for ch in members[:12]) +
                                                                     (" and more" if len(members) > 12 else "")),
                             "summary.locked": 1})
        ident = "custom.collection.%s.%s" % (key, col["ratingKey"])
        if ident not in managed:
            plexhttp.post("%s/hubs/sections/%s/manage" % (config.PLEX_URL, key), headers=PH(), timeout=20,
                          params={"metadataItemId": col["ratingKey"], "promotedToRecommended": 1,
                                  "promotedToOwnHome": 0, "promotedToSharedHome": 0})
        log.info("collection %s: %d channels, poster + promoted to Recommended", topic, len(members))


if __name__ == "__main__":
    main()
