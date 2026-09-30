"""SQLite store. Channels (with where they came from), videos with a state machine, and an event log.
Video states: discovered -> queued -> downloading -> processing -> done | skipped | failed
(plus 'pruned' when retention removes a file, and 'recut' when a later SponsorBlock pass re-processes it)."""
import json
import os
import sqlite3
import time

from . import config, redact

SCHEMA = """
CREATE TABLE IF NOT EXISTS channels(
  id TEXT PRIMARY KEY, handle TEXT, title TEXT, folder TEXT,
  source TEXT NOT NULL DEFAULT 'manual',          -- 'youtube-sub' | 'manual' | 'csv-import'
  added_at TEXT, enabled INTEGER NOT NULL DEFAULT 1, pending_removal_at TEXT,
  keep_count INTEGER, max_duration INTEGER, skip_lives INTEGER NOT NULL DEFAULT 1,
  description TEXT, subscribers INTEGER, category TEXT, avatar_url TEXT, banner_url TEXT,
  art_updated_at TEXT, plex_rating_key TEXT, meta TEXT);
CREATE TABLE IF NOT EXISTS videos(
  id TEXT PRIMARY KEY, channel_id TEXT NOT NULL, title TEXT, published_at TEXT, local_date TEXT,
  duration REAL, state TEXT NOT NULL DEFAULT 'discovered', reason TEXT,
  season INTEGER, episode INTEGER, path TEXT, size INTEGER, out_duration REAL,
  video_format TEXT, audio_format TEXT, sb_segments INTEGER, sb_cut_seconds REAL, sb_checked_at TEXT,
  plex_rating_key TEXT, watched INTEGER NOT NULL DEFAULT 0, created_at TEXT, updated_at TEXT, report TEXT);
CREATE INDEX IF NOT EXISTS videos_channel ON videos(channel_id, state);
CREATE TABLE IF NOT EXISTS events(ts TEXT, level TEXT, channel_id TEXT, video_id TEXT, message TEXT);
"""


def now():
    return time.strftime("%Y-%m-%dT%H:%M:%S%z")


MIGRATIONS = {  # table -> columns added after the first schema (added in place, never dropped)
    "videos": {"description": "TEXT", "summary": "TEXT", "summary_source": "TEXT", "rank": "INTEGER",
               "queued_at": "TEXT", "started_at": "TEXT", "finished_at": "TEXT", "attempts": "INTEGER DEFAULT 0",
               "height": "INTEGER", "vcodec": "TEXT", "keep_forever": "INTEGER DEFAULT 0", "priority": "INTEGER DEFAULT 0",
               "retry_at": "INTEGER", "removed_at": "TEXT", "removed_reason": "TEXT", "published_ts": "INTEGER",
               "transient_fails": "INTEGER DEFAULT 0"},
    "channels": {"summary": "TEXT", "summary_source": "TEXT", "csv_order": "INTEGER", "last_checked": "TEXT",
                 "gone_at": "TEXT", "gone_reason": "TEXT", "removed_checked_at": "TEXT", "topic": "TEXT",
                 "organized_at": "TEXT", "series_checked_at": "TEXT"},
}
SCHEMA += """
CREATE TABLE IF NOT EXISTS subs(handle TEXT PRIMARY KEY, title TEXT, csv_order INTEGER, channel_id TEXT,
  state TEXT DEFAULT 'new', error TEXT, checked_at TEXT);
CREATE TABLE IF NOT EXISTS removed_channels(channel_id TEXT PRIMARY KEY, handle TEXT, title TEXT, removed_at TEXT,
  reason TEXT);
"""


def connect():
    os.makedirs(config.DATA, exist_ok=True)
    # autocommit: every statement is its own short transaction, so no code path can leave the write lock held
    # (many worker threads share this DB; a forgotten commit once stalled them all with "database is locked")
    c = sqlite3.connect(os.path.join(config.DATA, "tubarr.db"), timeout=60, isolation_level=None,
                        check_same_thread=False)
    c.row_factory = sqlite3.Row
    c.execute("PRAGMA journal_mode=WAL")
    c.executescript(SCHEMA)
    for table, cols in MIGRATIONS.items():
        have = {r["name"] for r in c.execute("PRAGMA table_info(%s)" % table)}
        for col, typ in cols.items():
            if col not in have:
                c.execute("ALTER TABLE %s ADD COLUMN %s %s" % (table, col, typ))
    c.commit()
    return c


_FREE_TEXT = ("reason", "error", "removed_reason", "gone_reason")


def upsert(c, table, key, **fields):
    fields = {k: (json.dumps(v) if isinstance(v, (dict, list)) else v) for k, v in fields.items()}
    for k in _FREE_TEXT:                  # error texts can quote URLs (a proxy with credentials): scrub them
        if isinstance(fields.get(k), str):
            fields[k] = redact.text(fields[k])
    row = c.execute("SELECT 1 FROM %s WHERE id=?" % table, (key,)).fetchone()
    if row:
        if fields:
            c.execute("UPDATE %s SET %s WHERE id=?" % (table, ",".join("%s=?" % k for k in fields)), list(fields.values()) + [key])
    else:
        fields["id"] = key
        c.execute("INSERT INTO %s(%s) VALUES(%s)" % (table, ",".join(fields), ",".join("?" * len(fields))), list(fields.values()))
    c.commit()


def event(c, level, message, channel_id=None, video_id=None):
    c.execute("INSERT INTO events VALUES(?,?,?,?,?)", (now(), level, channel_id, video_id, redact.text(message)))
    c.commit()


def allocate_episode(c, channel_id, video_id, d):
    """Same-day index for an upload date: stable once assigned (files are never renumbered)."""
    row = c.execute("SELECT season, episode FROM videos WHERE id=? AND episode IS NOT NULL", (video_id,)).fetchone()
    if row:
        return row["episode"] % 100
    base = (d.month * 100 + d.day) * 100
    used = [r[0] for r in c.execute("SELECT episode FROM videos WHERE channel_id=? AND season=? AND episode BETWEEN ? AND ?",
                                    (channel_id, d.year, base + 1, base + 99))]
    return (max(used) - base + 1) if used else 1
