"""The video's own (YouTube) chapters, shifted through the cut. A chapter that falls entirely inside a removed range
is dropped; one that is partly cut keeps what's left. The result is contiguous from 0 to the new end."""
from .planner import to_output

MIN_LEN = 1.0


def remap(chapters, pieces, new_duration):
    """chapters: [(start, end, title)] in source seconds -> [(start, end, title)] in output seconds."""
    kept = []
    for s, e, title in sorted(chapters):
        if not any(min(e, p["end"]) - max(s, p["start"]) > 1e-3 for p in pieces):
            continue                                            # entirely inside removed ranges
        first = next(p for p in pieces if min(e, p["end"]) - max(s, p["start"]) > 1e-3)
        kept.append([to_output(max(s, first["start"]), pieces), title])
    if not kept:
        return []
    kept[0][0] = 0.0
    out = []
    for i, (s, title) in enumerate(kept):
        e = kept[i + 1][0] if i + 1 < len(kept) else new_duration
        if e - s < MIN_LEN and out:                             # a sliver: give its time to the previous chapter
            out[-1][1] = e
            continue
        out.append([s, e, title])
    if len(out) > 1 and out[-1][1] - out[-1][0] < MIN_LEN:
        out[-2][1] = out[-1][1]
        out.pop()
    out[-1][1] = new_duration
    return [tuple(c) for c in out]
