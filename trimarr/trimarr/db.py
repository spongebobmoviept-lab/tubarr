"""SQLite state in /data/trimarr.db (WAL, safe for the service and a one-off CLI container at the same time)."""
import json
import os
import sqlite3
import threading
import time

from . import config

SCHEMA = """
CREATE TABLE IF NOT EXISTS videos (
  video_id TEXT PRIMARY KEY,
  path TEXT,                 -- library path as this container sees it (/youtube/...)
  channel_id TEXT, channel TEXT, folder TEXT, title TEXT, upload_date TEXT,
  size INTEGER, mtime REAL, inode INTEGER, duration REAL,
  state TEXT,                -- new|waiting|clean|cuttable|suspicious|trimmed|failed|undone|gone
  detail TEXT,
  checked_at REAL, segments TEXT, plan TEXT, sb_seconds REAL, cut_seconds REAL,
  trimmed_at REAL, before_duration REAL, after_duration REAL, before_size INTEGER, after_size INTEGER,
  after_inode INTEGER, backup_path TEXT, backup_srts TEXT, backup_expires REAL,
  report TEXT, attempts INTEGER DEFAULT 0, updated_at REAL,
  swapped_at REAL            -- when the library file was last replaced (trim or undo): Tubarr refreshes Plex for it
);
CREATE INDEX IF NOT EXISTS videos_state ON videos(state);
CREATE INDEX IF NOT EXISTS videos_path ON videos(path);
CREATE TABLE IF NOT EXISTS events (
  id INTEGER PRIMARY KEY AUTOINCREMENT, at REAL, level TEXT, video_id TEXT, message TEXT
);
"""
MIGRATIONS = ("ALTER TABLE videos ADD COLUMN swapped_at REAL",)
JSON_COLS = ("segments", "plan", "report", "backup_srts")
_LOCK = threading.RLock()
_CONN = [None]


def conn():
    with _LOCK:
        if _CONN[0] is None:
            os.makedirs(config.DATA, exist_ok=True)
            c = sqlite3.connect(config.DB_PATH, timeout=60, isolation_level=None, check_same_thread=False)
            c.row_factory = sqlite3.Row
            c.execute("PRAGMA journal_mode=WAL")
            c.execute("PRAGMA busy_timeout=60000")
            c.executescript(SCHEMA)
            have = {r[1] for r in c.execute("PRAGMA table_info(videos)")}
            for m in MIGRATIONS:                                # older databases: add the new columns
                if m.split()[5] not in have:
                    c.execute(m)
            _CONN[0] = c
        return _CONN[0]


def _row(r):
    if r is None:
        return None
    d = dict(r)
    for k in JSON_COLS:
        if d.get(k):
            try:
                d[k] = json.loads(d[k])
            except ValueError:
                d[k] = None
    return d


def query(sql, args=()):
    with _LOCK:
        return [_row(r) for r in conn().execute(sql, args).fetchall()]


def one(sql, args=()):
    with _LOCK:
        return _row(conn().execute(sql, args).fetchone())


def execute(sql, args=()):
    with _LOCK:
        return conn().execute(sql, args)


def video(video_id):
    return one("SELECT * FROM videos WHERE video_id=?", (video_id,))


def upsert(video_id, **fields):
    fields["updated_at"] = time.time()
    for k in JSON_COLS:
        if k in fields and fields[k] is not None and not isinstance(fields[k], str):
            fields[k] = json.dumps(fields[k], separators=(",", ":"))
    with _LOCK:
        c = conn()
        if c.execute("SELECT 1 FROM videos WHERE video_id=?", (video_id,)).fetchone():
            c.execute("UPDATE videos SET %s WHERE video_id=?" % ",".join("%s=?" % k for k in fields),
                      list(fields.values()) + [video_id])
        else:
            fields["video_id"] = video_id
            c.execute("INSERT INTO videos (%s) VALUES (%s)" % (",".join(fields), ",".join("?" * len(fields))),
                      list(fields.values()))


def event(level, message, video_id=None):
    with _LOCK:
        conn().execute("INSERT INTO events (at, level, video_id, message) VALUES (?,?,?,?)",
                       (time.time(), level, video_id, message[:2000]))
        conn().execute("DELETE FROM events WHERE id < (SELECT MAX(id) - 5000 FROM events)")
