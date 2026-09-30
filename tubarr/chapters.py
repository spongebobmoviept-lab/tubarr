"""Chapters in the OUTPUT timeline: YouTube's own chapters (description timestamps) + SponsorBlock intro/outro,
remapped through the cuts. Intro/outro become chapters named "Intro" and "Credits"."""

MARK_TITLES = {"intro": "Intro", "outro": "Credits"}


def to_output(t, ranges, clamp=True):
    """Source time -> output time. A time inside a removed part maps to where the content resumes (clamp=True)."""
    out = 0.0
    for a, b in ranges:
        if t < a:
            return out if clamp else None
        if t <= b:
            return out + (t - a)
        out += b - a
    return out if clamp else None


def output_duration(ranges):
    return sum(b - a for a, b in ranges)


def build(info, ranges, marks, sb_segments=(), min_len=1.0):
    """Returns [(start, end, title)] in output seconds, or [] if chapters would be pointless."""
    total = output_duration(ranges)
    src = [(c["start_time"], c.get("end_time"), (c.get("title") or "").strip()) for c in (info.get("chapters") or [])]
    if not src:   # fall back to community chapters from SponsorBlock
        src = [(s["segment"][0], s["segment"][1], (s.get("description") or "").strip())
               for s in sb_segments if s.get("actionType") == "chapter" and s.get("description")]
    src.sort()
    base = []   # (out_start, title) from YouTube chapters
    for s, e, t in src:
        base.append((to_output(s, ranges), t or "Chapter"))
    spans = []  # (out_start, out_end, title) for intro/outro
    for m in marks:
        s, e = to_output(m["start"], ranges), to_output(m["end"], ranges)
        if e - s >= min_len:
            spans.append((s, e, MARK_TITLES.get(m["category"], m["category"].title())))
    cuts = sorted({0.0, total} | {s for s, _ in base} | {x for s, e, _ in spans for x in (s, e)})
    out = []
    for a, b in zip(cuts, cuts[1:]):
        if b - a < 1e-3:
            continue
        mid = (a + b) / 2
        title = next((t for s, e, t in spans if s <= mid < e), None)
        if title is None:
            title = next((t for s, t in reversed(base) if s <= mid), None) or (info.get("title") or "Main")
        if out and out[-1][2] == title:
            out[-1] = (out[-1][0], b, title)
        else:
            out.append((a, b, title))
    # fold slivers (< min_len) into their neighbour
    clean = []
    for s, e, t in out:
        if clean and e - s < min_len:
            clean[-1] = (clean[-1][0], e, clean[-1][2])
        else:
            clean.append((s, e, t))
    return clean if len(clean) > 1 else []
