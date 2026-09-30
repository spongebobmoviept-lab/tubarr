"""Kodi-style NFO files for Plex's "Plex NFO Series" agent (local-only). Written before the video appears."""
import os
import re
import xml.etree.ElementTree as ET


def _write(root, path):
    ET.indent(root, space="  ")
    tmp = path + ".tmp"
    ET.ElementTree(root).write(tmp, encoding="utf-8", xml_declaration=True)
    os.replace(tmp, path)


# Characters XML 1.0 doesn't allow (control characters, lone surrogates, U+FFFE/FFFF): a title or description from
# YouTube containing one would otherwise make an NFO that Plex can't read.
_XML_BAD = re.compile("[^\x09\x0a\x0d\x20-\ud7ff\ue000-\ufffd\U00010000-\U0010ffff]")


def xml_text(s):
    return _XML_BAD.sub("", str(s))


def _add(parent, tag, text, **attrs):
    if text not in (None, ""):
        e = ET.SubElement(parent, tag, **{k: xml_text(v) for k, v in attrs.items()})
        e.text = xml_text(text)
        return e


def tvshow(path, channel_id, title, plot, genre=None, premiered=None, handle=None):
    r = ET.Element("tvshow")
    _add(r, "title", title)
    _add(r, "originaltitle", title)
    _add(r, "plot", plot)
    _add(r, "studio", "YouTube")
    _add(r, "genre", genre)
    _add(r, "premiered", premiered)
    _add(r, "uniqueid", channel_id, type="youtube", default="true")
    _add(r, "tag", handle)
    _write(r, path)


def season(path, year):
    r = ET.Element("season")
    _add(r, "title", "Season %d" % year)
    _add(r, "seasonnumber", year)
    _write(r, path)


def episode(path, video_id, title, plot, aired, season_no, episode_no, channel, runtime_s=None, genre=None, tags=()):
    r = ET.Element("episodedetails")
    _add(r, "title", title)
    _add(r, "showtitle", channel)
    _add(r, "season", season_no)
    _add(r, "episode", episode_no)
    _add(r, "aired", aired)
    _add(r, "premiered", aired)
    _add(r, "plot", plot)
    _add(r, "runtime", int(round(runtime_s / 60)) if runtime_s else None)
    _add(r, "studio", "YouTube")
    _add(r, "genre", genre)
    _add(r, "uniqueid", video_id, type="youtube", default="true")
    for t in tags or ():
        _add(r, "tag", t)
    _write(r, path)
