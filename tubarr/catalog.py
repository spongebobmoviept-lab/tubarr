"""Catalog / backfill estimate for the subscription list (read-only, gentle).
Per channel: one flat /videos listing (newest 30 non-Short uploads with durations; Shorts and live replays are not
in that tab) + the UULF RSS feed (long-form uploads only, exact publish dates -> upload rate). Optional format
samples (one video's real 1080p H.264 + AAC size) give MB per minute.
usage: python -m tubarr.catalog /data/import/subs.csv /data/catalog.json [--sample @h1,@h2,...]"""
import csv
import json
import logging
import random
import statistics
import sys
import time
import xml.etree.ElementTree as ET


from . import config, ytdl

log = logging.getLogger("tubarr.catalog")
NS = {"a": "http://www.w3.org/2005/Atom", "yt": "http://www.youtube.com/xml/schemas/2015",
      "media": "http://search.yahoo.com/mrss/"}


def rss(playlist_id=None, channel_id=None):
    q = {"playlist_id": playlist_id} if playlist_id else {"channel_id": channel_id}
    from . import net
    r = net.yt_get("https://www.youtube.com/feeds/videos.xml", params=q, timeout=20)
    if r.status_code != 200:
        return None, r.status_code
    root = ET.fromstring(r.content)
    out = []
    for e in root.findall("a:entry", NS):
        link = e.find("a:link", NS)
        out.append({"id": e.findtext("yt:videoId", namespaces=NS), "title": e.findtext("a:title", namespaces=NS),
                    "published": e.findtext("a:published", namespaces=NS), "link": link.get("href") if link is not None else None})
    return out, 200


def sample_size(video_id):
    """Real size of the format Tubarr would pick (bytes, duration s) without downloading."""
    v = ytdl.Video(video_id, config.SCRATCH)
    try:
        info = v.extract()
        vf, af = v.chosen()
        size = sum((f or {}).get("filesize") or (f or {}).get("filesize_approx") or 0 for f in (vf, af))
        return {"video_id": video_id, "size": size, "duration": info.get("duration"), "vf": vf and vf.get("format_id"),
                "vcodec": vf and vf.get("vcodec"), "fps": vf and vf.get("fps"), "live_status": info.get("live_status")}
    finally:
        v.close()


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    src, dst = sys.argv[1], sys.argv[2]
    sample = []
    if "--sample" in sys.argv:
        sample = sys.argv[sys.argv.index("--sample") + 1].split(",")
    try:
        out = json.load(open(dst))
    except Exception:
        out = {"channels": {}, "samples": {}}
    rows = list(csv.DictReader(open(src, encoding="utf-8-sig")))
    for n, r in enumerate(rows, 1):
        handle = (r.get("handle") or "").strip()
        if not handle or handle in out["channels"]:
            continue
        rec = {"title": (r.get("title") or "").strip(), "handle": handle, "subscribers": r.get("subscribers")}
        try:
            ch = ytdl.channel_tab(handle, limit=30)
            ents = ch["entries"]
            rec.update(channel_id=ch.get("channel_id"), yt_title=ch.get("channel"),
                       videos=[{"id": e.get("id"), "duration": e.get("duration"), "ts": e.get("timestamp"),
                                "title": (e.get("title") or "")[:80], "live": e.get("live_status")} for e in ents])
            time.sleep(random.uniform(2, 4))
            feed, code = rss(playlist_id="UULF" + ch["channel_id"][2:])
            rec["uulf_status"] = code
            rec["uulf"] = feed or []
        except ytdl.BotCheck as e:
            log.error("bot check at %s - stopping: %s", handle, e)
            out["stopped"] = "bot check at %s" % handle
            break
        except Exception as e:
            rec["error"] = str(e)[:300]
            log.warning("%s: %s", handle, e)
        out["channels"][handle] = rec
        json.dump(out, open(dst, "w"), indent=1)
        log.info("[%d/%d] %s: %d uploads listed, %d in UULF feed", n, len(rows), handle, len(rec.get("videos", [])), len(rec.get("uulf", [])))
        time.sleep(random.uniform(3, 6))
    for handle in sample:
        rec = out["channels"].get(handle)
        if not rec or handle in out["samples"] or not rec.get("videos"):
            continue
        vid = next((v["id"] for v in rec["videos"] if (v.get("duration") or 0) >= 120), None)
        if not vid:
            continue
        try:
            out["samples"][handle] = sample_size(vid)
            log.info("sample %s: %s", handle, out["samples"][handle])
        except ytdl.BotCheck as e:
            out["stopped"] = "bot check sampling %s" % handle
            break
        except Exception as e:
            out["samples"][handle] = {"error": str(e)[:300]}
        json.dump(out, open(dst, "w"), indent=1)
        time.sleep(random.uniform(8, 14))
    json.dump(out, open(dst, "w"), indent=1)
    print("catalog done:", len(out["channels"]), "channels,", len(out["samples"]), "samples", out.get("stopped", ""))


if __name__ == "__main__":
    main()
