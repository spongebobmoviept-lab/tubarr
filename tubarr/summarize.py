"""Episode / show summaries - no AI anywhere: nothing rewrites the creators' text. A summary is the creator's own YouTube description with deterministic cleanup only (textclean: links,
hashtags, timestamps, sponsor / social / credits clutter), else a short line built from the title.
`use_llm` is accepted for old callers and ignored."""
import logging

from . import textclean

log = logging.getLogger("tubarr.summary")


def title_line(title, channel):
    return "%s: %s." % (channel, title.rstrip(".!?")) if title else ""


def episode(title, channel, description, chapter_titles=(), use_llm=False):
    """-> (summary, source) with source 'rules' (the cleaned description) or 'title'."""
    rules = textclean.clean(description or "")
    if len(rules) >= 60:
        return rules, "rules"
    return title_line(title, channel), "title"


def show(channel, about, use_llm=False):
    """-> (summary, source) with source 'rules' (the cleaned About text) or 'name'."""
    rules = textclean.clean(about or "", max_chars=900)
    if rules:
        return rules, "rules"
    for line in (about or "").splitlines():                     # first link-free line, else the channel's name
        line = line.strip()
        if len(line) > 25 and "http" not in line and "@" not in line and "#" not in line:
            return line[:400], "rules"
    return "%s on YouTube." % channel, "name"
