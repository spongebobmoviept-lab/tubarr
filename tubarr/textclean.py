"""Turn a YouTube description into a clean Plex summary (rules-based, no AI).
Removes: links, sponsor plugs, timestamp lists, hashtags, social/merch/gear lines, credits and reference lists."""
import re

URL = re.compile(r"(https?://\S+|www\.\S+|\b[\w-]+(?:\.[\w-]+)*\.(?:com|net|org|io|co|gg|tv|me|ly|link|shop|store|us|uk|ca|au|de|app|dev|to|fm|be|gl|page|xyz|info)(?:/\S*)?)", re.I)
TIMESTAMP = re.compile(r"^\s*[\(\[]?(?:\d{1,2}:)?\d{1,2}:\d{2}[\)\]]?(?:\s*[-–—:|]\s*|\s+)")
HASHTAG = re.compile(r"(?<![\w&])#[^\s#]+")
SEPARATOR = re.compile(r"^[\W_]{3,}$")
MENTION = re.compile(r"(?<!\w)@[\w.-]+")
SPONSOR = re.compile(r"\b(sponsor(?:ed|s|ing)?|promo ?code|use (?:my |our )?code|coupon|discount|\d+ ?% off|free trial|"
                     r"affiliate|paid promotion|partner(?:ed)? with|thanks to .{1,40} for (?:supporting|sponsoring)|"
                     r"(?:head|go) (?:to|over to) \S+ (?:and|to)|sign up (?:at|for|with)|download .{1,30} (?:for free|today)|"
                     r"get \d+ ?(?:%|months?|days?|free)|link in (?:the )?description|check out .{1,40} (?:at|here|below))\b", re.I)
JUNK_LINE = re.compile(r"\b(instagram|insta|twitter|tiktok|facebook|discord|twitch|patreon|snapchat|threads|bluesky|linkedin|"
                       r"reddit|x\.com|subscribe|merch|merchandise|newsletter|podcast|spotify|apple podcasts|"
                       r"business (?:inquiries|enquiries|email|contact)|contact (?:me|us)|e-?mail|donate|paypal|venmo|cash ?app|"
                       r"ko-?fi|buy me a coffee|follow (?:me|us)|social(?:s| media)|become a (?:member|patron)|"
                       r"join (?:this|the|our) (?:channel|discord|community)|membership|support (?:the|this|our|my) (?:channel|work)|"
                       r"music (?:by|from|used|credits?)|epidemic sound|artlist|musicbed|licensed|stock footage|"
                       r"(?:filmed|shot|recorded) (?:on|with)|my (?:gear|camera|setup|kit)|gear (?:i use|used|list)|"
                       r"(?:written|edited|directed|produced|animated|narrated|filmed|hosted|researched|scored|mixed|illustrated) by|"
                       r"(?:animation|editing|music|thumbnail|camera|sound|script|research|production|additional|special) (?:by|design|thanks)|"
                       r"thumbnail by|video by|executive producer|producer|editor|writer|animator|"
                       r"research(?:ed)? by|fact-?check(?:ed|ing)?|consultants?|advisors?|"
                       r"thanks? (?:to|for|out|you)|thank you|huge thanks|big thanks|special thanks|shout-?outs?|"
                       r"support (?:us|me)|consider supporting|supporters?|patrons?|"
                       r"amazon|affiliate|commission|disclosure|as an amazon associate|shop (?:now|here)|store)\b", re.I)
THANKS = re.compile(r"^\s*(?:a |an |our |my )?(?:huge |big |special |massive |many )?(?:thanks|thank you|shout-?out)", re.I)
SECTION = re.compile(r"^\s*[\W_]*\s*(chapters?|timestamps?|time ?codes?|references?|sources?|citations?|further reading|"
                     r"links?|useful links|resources|credits?|music|socials?|follow|connect|support|gear|equipment|"
                     r"special thanks|thanks to|patreon|merch|shop|sponsors?|contents?|in this video|related videos?|"
                     r"playlists?|watch next|more videos|about (?:me|us))\b.{0,40}:?\s*[\W_]*\s*$", re.I)


def _line_ok(line):
    s = line.strip()
    if not s or SEPARATOR.match(s):
        return False
    if TIMESTAMP.match(s):
        return False
    if URL.search(s):
        return False
    if HASHTAG.sub("", s).strip() == "":
        return False
    if JUNK_LINE.search(s) and len(s) < 200:
        return False
    return True


def clean(text, max_chars=1200):
    if not text:
        return ""
    text = text.replace("\r", "")
    paras = [p for p in re.split(r"\n\s*\n", text) if p.strip()]
    kept = []
    for p in paras:
        if SPONSOR.search(p) or THANKS.match(p):
            continue                                   # sponsor plugs and thank-you/credit paragraphs go as a whole
        lines = []
        for line in p.split("\n"):
            if SECTION.match(line):
                break                                  # "References:", "Timestamps:", ... -> rest of the paragraph is a list
            if _line_ok(line):
                lines.append(line.strip())
        if not lines:
            continue
        para = " ".join(lines) if all(len(l) > 60 for l in lines[:-1]) else "\n".join(lines)
        para = HASHTAG.sub("", para)
        para = re.sub(r"[ \t]+", " ", para).strip(" -–—|•·")
        if len(para.split()) < 4 and kept:
            continue
        kept.append(para)
    out = "\n\n".join(kept).strip()
    if len(out) > max_chars:
        cut = out[:max_chars]
        stop = max(cut.rfind(". "), cut.rfind("! "), cut.rfind("? "), cut.rfind("\n"))
        out = (cut[:stop + 1] if stop > max_chars * 0.5 else cut.rstrip() + "…").strip()
    return out
