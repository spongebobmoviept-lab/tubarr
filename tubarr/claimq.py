"""The download queue's order and eligibility, in ONE place: worker.claim() takes the first row, and the web UI
(Activity, "next up") shows the same rows in the same order, so they can't drift apart.

"Download now" (priority 3, oldest press first; worker.claim() starts it without the pace gap or daily cap, still
one at a time), then "download first" (priority 2), then newest published first, over queued / upgrade videos with
fewer than 3 attempts, from channels that aren't gone, disabled or pending removal, within the channel's own length
limit, keep count and retention window (requests made by hand, priority >= 1 / 2, skip those limits).
"""
from datetime import datetime

QUEUE_SQL = """SELECT v.* FROM videos v JOIN channels ch ON ch.id=v.channel_id
                            WHERE v.state IN ('queued','upgrade') AND COALESCE(v.attempts,0) < 3 AND ch.gone_at IS NULL
                            AND COALESCE(ch.enabled,1)=1 AND ch.pending_removal_at IS NULL
                            AND (COALESCE(v.priority,0) >= 2 OR ch.max_duration IS NULL OR ch.max_duration <= 0
                                 OR COALESCE(v.duration,0) <= ch.max_duration)
                            AND (COALESCE(v.priority,0) >= 1 OR (
                                 (ch.keep_count IS NULL OR COALESCE(v.rank,0) < ch.keep_count)
                                 AND COALESCE((CASE WHEN json_valid(ch.meta) THEN json_extract(ch.meta,'$.retention.mode') END),'fill') <> 'new_only'
                                 AND (COALESCE((CASE WHEN json_valid(ch.meta) THEN json_extract(ch.meta,'$.retention.mode') END),'') <> 'days' OR COALESCE(v.published_ts,0) >=
                                      CAST(strftime('%%s','now') AS INTEGER) - 86400*json_extract(ch.meta,'$.retention.days'))
                                 AND (COALESCE((CASE WHEN json_valid(ch.meta) THEN json_extract(ch.meta,'$.retention.mode') END),'') <> 'since' OR COALESCE(v.published_ts,0) >=
                                      CAST(strftime('%%s', json_extract(ch.meta,'$.retention.since')) AS INTEGER))))
                            %s
                            ORDER BY (COALESCE(v.priority,0) >= 3) DESC,     -- 'download now' (oldest press first)
                                     CASE WHEN COALESCE(v.priority,0) >= 3 THEN v.queued_at END ASC,
                                     (COALESCE(v.priority,0) >= 2) DESC, COALESCE(v.published_ts, 0) DESC, v.queued_at ASC   -- 'download first' jumps the line
                            %s"""


def eligible(c, reserved=False, limit=None):
    """Queue rows in claim order. reserved = the new-upload slot (priority >= 1 only)."""
    return c.execute(QUEUE_SQL % ("AND COALESCE(v.priority,0) >= 1" if reserved else "",
                                  "LIMIT %d" % int(limit) if limit else "")).fetchall()


def oldest_in_library(c):
    """Upload date of the oldest video the roll could remove (not kept forever, not removed from YouTube)."""
    return c.execute("SELECT MIN(local_date) FROM videos WHERE state='done' AND COALESCE(keep_forever,0)=0 "
                     "AND removed_at IS NULL").fetchone()[0]


def blocked_when_full(v, oldest):
    """Full library: a video no newer than the oldest one we'd evict isn't downloaded (never evict a newer video to
    make room for an older one). Downloads pushed by hand (priority 2+) always go ahead."""
    vdate = datetime.fromtimestamp(v["published_ts"]).strftime("%Y-%m-%d") if v["published_ts"] else None
    return (v["priority"] or 0) < 2 and bool(oldest) and bool(vdate) and vdate <= oldest
