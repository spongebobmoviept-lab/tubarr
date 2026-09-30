"""SRT captions (<episode>.en.srt) shifted through the cut.

Each cue is mapped through the kept pieces: the part of a cue inside a kept piece moves to that piece's place in the
output, a cue entirely inside a removed range is dropped, a cue that straddles a cut is clipped to what's left.
Text and styling are copied verbatim; cues are renumbered.
"""
import re

_TIME = re.compile(r"(\d+):(\d{1,2}):(\d{1,2})[,.](\d{1,3})")


def _t(s):
    m = _TIME.search(s)
    if not m:
        raise ValueError(s)
    h, mi, se, ms = m.groups()
    return int(h) * 3600 + int(mi) * 60 + int(se) + int(ms.ljust(3, "0")) / 1000.0


def _fmt(t):
    ms = max(0, int(round(t * 1000)))
    return "%02d:%02d:%02d,%03d" % (ms // 3600000, ms // 60000 % 60, ms // 1000 % 60, ms % 1000)


def parse(text):
    """[(start, end, body)] from SRT text (tolerant of CRLF, BOM, missing indexes and '.' millisecond separators)."""
    cues = []
    for block in re.split(r"\n[ \t]*\n", text.replace("\r\n", "\n").replace("\r", "\n").lstrip("﻿")):
        lines = block.strip("\n").split("\n")
        for i, ln in enumerate(lines):
            if "-->" in ln:
                a, b = ln.split("-->", 1)
                try:
                    cues.append((_t(a), _t(b), "\n".join(lines[i + 1:]).rstrip()))
                except ValueError:
                    pass
                break
    return cues


def render(cues):
    return "".join("%d\n%s --> %s\n%s\n\n" % (i, _fmt(s), _fmt(e), body) for i, (s, e, body) in enumerate(cues, 1))


def shift(cues, pieces, min_len=0.3):
    out, offset = [], []
    o = 0.0
    for p in pieces:
        offset.append(o)
        o += p["end"] - p["start"]
    for s, e, body in cues:
        for p, off in zip(pieces, offset):
            ss, ee = max(s, p["start"]), min(e, p["end"])
            if ee - ss >= min_len or (ee > ss and ss == s and ee == e):
                out.append((off + ss - p["start"], off + ee - p["start"], body))
    out.sort(key=lambda c: c[0])
    return out


def shift_file(src_path, dst_path, pieces):
    """Writes the shifted captions; returns (cues_before, cues_after)."""
    with open(src_path, encoding="utf-8-sig", errors="replace") as f:
        cues = parse(f.read())
    new = shift(cues, pieces)
    with open(dst_path, "w", encoding="utf-8", newline="\n") as f:
        f.write(render(new))
    return len(cues), len(new)
