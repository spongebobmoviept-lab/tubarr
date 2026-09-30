"""Small maintenance commands.
  python -m tubarr.tool refresh-art <channel_id|all>   regenerate poster / background / season posters (no YouTube request)
  python -m tubarr.tool add-subs <video_id>            fetch English subtitles that appeared after download
  python -m tubarr.tool status                         channels and videos in the DB"""
import json
import os
import sys

from . import db, pipeline


def refresh_art(c, cid):
    rows = c.execute("SELECT * FROM channels" + ("" if cid == "all" else " WHERE id=?"), () if cid == "all" else (cid,)).fetchall()
    for ch in rows:
        pipeline._ART.pop(ch["id"], None)
        a = pipeline._art_for(ch)
        d = pipeline.show_dir(ch["folder"])
        from . import art
        art.save_jpg(a.poster(), os.path.join(d, "poster.jpg"))
        art.save_jpg(a.background(), os.path.join(d, "fanart.jpg"))
        years = [r[0] for r in c.execute("SELECT DISTINCT season FROM videos WHERE channel_id=? AND state='done'", (ch["id"],))]
        for y in years:
            pipeline.ensure_season(c, ch, y, force=True)
        db.upsert(c, "channels", ch["id"], art_updated_at=db.now())
        print("art refreshed:", ch["title"], "seasons", years)


def main():
    c = db.connect()
    cmd, arg = sys.argv[1], (sys.argv[2] if len(sys.argv) > 2 else None)
    if cmd == "refresh-art":
        refresh_art(c, arg or "all")
    elif cmd == "add-subs":
        print("cues:", pipeline.add_subtitles(c, arg))
    elif cmd == "status":
        for ch in c.execute("SELECT id, title, folder, source, category FROM channels"):
            print(dict(ch))
        for v in c.execute("SELECT id, channel_id, state, season, episode, title, reason FROM videos ORDER BY updated_at"):
            print(dict(v))


if __name__ == "__main__":
    main()
