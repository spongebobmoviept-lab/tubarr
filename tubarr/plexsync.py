"""Plex side of Tubarr: partial scan of a show folder, then push the final metadata with the fields LOCKED
(a refresh can't overwrite them), upload + lock the artwork, and set the show to newest-first.
The library uses the local-only "Plex NFO Series" agent, so a fresh scan already looks right from the NFO files
and local artwork; this pass makes it explicit and permanent.
usage: python -m tubarr.plexsync [channel_id ...]   (default: every channel with finished videos)"""
import json
import logging
import os
import sys
import time

from plexapi.server import PlexServer

from . import config, db, naming, plexhttp, textclean

log = logging.getLogger("tubarr.plex")


def plex_path(local):
    return config.PLEX_ROOT + local[len(config.ROOT):] if local.startswith(config.ROOT) else local


def connect():
    plex = PlexServer(config.PLEX_URL, config.PLEX_TOKEN, session=plexhttp.Session(), timeout=60)   # no redirects
    return plex, plex.library.section(config.PLEX_SECTION)


def find_show(sec, folder_local):
    p = plex_path(folder_local).rstrip("/")
    for show in sec.all():
        if any(loc.rstrip("/") == p for loc in show.locations):
            return show
    return None


def episode_map(show):
    out = {}
    for ep in show.episodes():
        for part in ep.iterParts():
            out[part.file] = ep
    return out


def wait_for(sec, folder_local, files_local, timeout=180):
    """Partial-scan the folder and wait until every file is an episode in Plex."""
    sec.update(path=plex_path(folder_local))
    want = {plex_path(f) for f in files_local}
    t0 = time.time()
    while time.time() - t0 < timeout:
        time.sleep(5)
        show = find_show(sec, folder_local)
        if show:
            show.reload()
            eps = episode_map(show)
            if want <= set(eps):
                return show, eps
    show = find_show(sec, folder_local)
    return show, (episode_map(show) if show else {})


def snapshot(item):
    item.reload()
    d = {"ratingKey": item.ratingKey, "title": item.title, "guid": item.guid, "summary": (item.summary or "")[:90],
         "thumb": bool(item.thumb), "art": bool(getattr(item, "art", None))}
    for k in ("index", "parentIndex", "originallyAvailableAt", "studio"):
        v = getattr(item, k, None)
        if v is not None:
            d[k] = str(v)[:10] if k == "originallyAvailableAt" else v
    if hasattr(item, "genres"):
        d["genres"] = [g.tag for g in item.genres]
    return d


def sync_channel(c, sec, chrow):
    d_show = os.path.join(config.ROOT, chrow["folder"])
    vids = c.execute("SELECT * FROM videos WHERE channel_id=? AND state='done' ORDER BY season, episode", (chrow["id"],)).fetchall()
    if not vids:
        return None
    show, eps = wait_for(sec, d_show, [v["path"] for v in vids])
    if not show:
        raise RuntimeError("Plex did not create the show for %s" % d_show)
    result = {"show_before": snapshot(show), "episodes_before": [], "episodes_after": []}
    plot = chrow["summary"] or textclean.clean(chrow["description"] or "", max_chars=900)
    show.batchEdits()
    show.editTitle(chrow["title"], locked=True)
    show.editSortTitle(chrow["title"][4:] if chrow["title"].lower().startswith("the ") else chrow["title"], locked=True)
    show.editSummary(plot, locked=True)
    show.editStudio("YouTube", locked=True)
    show.saveEdits()
    if chrow["category"]:
        show.addGenre([chrow["category"]], locked=True)
    show.uploadPoster(filepath=os.path.join(d_show, "poster.jpg")); show.lockPoster()
    show.uploadArt(filepath=os.path.join(d_show, "fanart.jpg")); show.lockArt()
    try:
        show.editAdvanced(episodeSort=1)                    # newest first
    except Exception as e:
        log.warning("episodeSort pref: %s", e)
    for season in show.seasons():
        p = os.path.join(d_show, "Season%04d.jpg" % season.index)
        if os.path.exists(p):
            season.uploadPoster(filepath=p); season.lockPoster()
    for v in vids:
        ep = eps.get(plex_path(v["path"]))
        if ep is None:
            log.warning("not in Plex yet: %s", v["path"])
            continue
        result["episodes_before"].append(snapshot(ep))
        rep = json.loads(v["report"] or "{}")
        ep.batchEdits()
        ep.editTitle(v["title"], locked=True)
        ep.editSummary(v["summary"] or rep.get("summary") or "", locked=True)
        ep.editOriginallyAvailable(v["local_date"], locked=True)
        ep.saveEdits()
        thumb = os.path.splitext(v["path"])[0] + ".jpg"
        if os.path.exists(thumb):
            ep.uploadPoster(filepath=thumb); ep.lockPoster()
        db.upsert(c, "videos", v["id"], plex_rating_key=str(ep.ratingKey))
        result["episodes_after"].append(snapshot(ep))
    db.upsert(c, "channels", chrow["id"], plex_rating_key=str(show.ratingKey))
    result["show_after"] = snapshot(show)
    try:
        result["show_after"]["episodeSort"] = show.preferences() and next(
            (p.value for p in show.preferences() if p.id == "episodeSort"), None)
    except Exception:
        pass
    return result


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    c = db.connect()
    plex, sec = connect()
    ids = sys.argv[1:] or [r["channel_id"] for r in c.execute("SELECT DISTINCT channel_id FROM videos WHERE state='done'")]
    for cid in ids:
        chrow = c.execute("SELECT * FROM channels WHERE id=?", (cid,)).fetchone()
        res = sync_channel(c, sec, chrow)
        print("SYNC " + json.dumps({"channel": chrow["title"], **(res or {})}, default=str), flush=True)


if __name__ == "__main__":
    main()
