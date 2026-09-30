"""Artwork.
Episode thumbnails: the video's own thumbnail at max resolution, bars cropped, 16:9, JPEG.
Show art (YouTube only has square avatars and ultra-wide banners, neither fits Plex): generated
1000x1500 posters, season posters (poster + year) and a 1920x1080 background, from the channel's uncropped banner,
avatar and name. Falls back to a generated backdrop when a channel has no banner."""
import colorsys
import io
import logging
import os

from PIL import Image, ImageDraw, ImageEnhance, ImageFilter, ImageFont, ImageOps, ImageStat


log = logging.getLogger("tubarr.art")
FONTS = "/usr/local/share/fonts/tubarr"


# ---------------------------------------------------------------- helpers
MAX_FETCH_BYTES = 15 * 1024 * 1024


def fetch(url, timeout=30):
    from . import net
    r = net.yt_get(url, timeout=timeout, stream=True)   # YouTube image hosts only, through the active network line
    try:
        r.raise_for_status()
        buf = io.BytesIO()
        for chunk in r.iter_content(256 * 1024):
            buf.write(chunk)
            if buf.tell() > MAX_FETCH_BYTES:
                raise ValueError("image larger than %d MB" % (MAX_FETCH_BYTES >> 20))
    finally:
        r.close()
    buf.seek(0)
    im = Image.open(buf)
    im.load()
    return im


Image.MAX_IMAGE_PIXELS = 50_000_000           # big avatars (e.g. 3706x3709 = 14 MP) fit; decompression bombs don't


def rgb(im):
    """Flatten to RGB. Transparent logos (e.g. a dark logo on a transparent background) go onto a backdrop that
    contrasts with their visible pixels - white behind dark logos, near-black behind light ones - never plain black."""
    if im.mode in ("RGBA", "LA", "P"):
        im = im.convert("RGBA")
        a = im.split()[-1]
        bgc = (0, 0, 0)
        if a.getextrema()[0] < 250:
            mask = a.point(lambda v: 255 if v > 128 else 0)
            if mask.getbbox():
                lum = ImageStat.Stat(im.convert("L"), mask=mask).mean[0]
                bgc = (246, 246, 248) if lum < 110 else (18, 18, 24)
        bg = Image.new("RGB", im.size, bgc)
        bg.paste(im, mask=a)
        return bg
    return im.convert("RGB")


def cover(im, w, h, centering=(0.5, 0.5)):
    return ImageOps.fit(rgb(im), (w, h), Image.LANCZOS, centering=centering)


def save_jpg(im, path, q=92):
    tmp = path + ".tmp"
    rgb(im).save(tmp, "JPEG", quality=q, optimize=True, progressive=True, subsampling=0)
    os.replace(tmp, path)


def font(name, size, weight=None):
    f = ImageFont.truetype(os.path.join(FONTS, name), size)
    if weight is not None:
        try:
            vals = []
            for ax in f.get_variation_axes():
                nm = ax.get("name", b"")
                nm = nm.decode() if isinstance(nm, bytes) else str(nm)
                if "eight" in nm:
                    vals.append(max(ax["minimum"], min(ax["maximum"], weight)))
                elif "ptical" in nm:
                    vals.append(max(ax["minimum"], min(ax["maximum"], min(size, 32))))
                else:
                    vals.append(ax["default"])
            f.set_variation_by_axes(vals)
        except Exception as e:  # static font or no variation support
            log.debug("font variation: %s", e)
    return f


def accent_of(im):
    """A saturated, mid-light accent colour taken from the avatar (YouTube red if it's monochrome)."""
    q = rgb(im).resize((72, 72)).quantize(colors=10, method=Image.Quantize.MEDIANCUT)
    pal = q.getpalette()
    best, score = None, -1.0
    for cnt, idx in q.getcolors():
        r, g, b = pal[idx * 3:idx * 3 + 3]
        h, l, s = colorsys.rgb_to_hls(r / 255, g / 255, b / 255)
        sc = cnt * (0.15 + s) * max(0.05, 1 - abs(l - 0.5) * 1.6)
        if s > 0.18 and sc > score:
            best, score = (h, l, s), sc
    if best is None:
        return (235, 38, 52)
    h, l, s = best
    r, g, b = colorsys.hls_to_rgb(h, min(max(l, 0.48), 0.62), max(s, 0.6))
    return (int(r * 255), int(g * 255), int(b * 255))


def shade(c, f):
    return tuple(max(0, min(255, int(x * f))) for x in c)


def vgradient(w, h, top, bottom):
    g = Image.new("RGB", (1, h))
    for y in range(h):
        t = y / max(1, h - 1)
        g.putpixel((0, y), tuple(int(top[i] + (bottom[i] - top[i]) * t) for i in range(3)))
    return g.resize((w, h))


def alpha_ramp(w, h, a0, a1, vertical=True, start=0.0, end=1.0):
    """L mask: alpha a0 before `start`, ramping to a1 at `end` (fractions of h or w)."""
    n = h if vertical else w
    line = Image.new("L", (1, n) if vertical else (n, 1))
    for i in range(n):
        t = i / max(1, n - 1)
        k = 0.0 if t <= start else 1.0 if t >= end else (t - start) / (end - start)
        k = k * k * (3 - 2 * k)                          # smoothstep
        line.putpixel((0, i) if vertical else (i, 0), int(a0 + (a1 - a0) * k))
    return line.resize((w, h))


def grain(im, amount=0.035):
    noise = Image.effect_noise(im.size, 64).convert("RGB")
    return Image.blend(im, noise, amount)


def circle(im, d):
    im = cover(im, d * 2, d * 2).resize((d, d), Image.LANCZOS)
    m = Image.new("L", (d * 4, d * 4), 0)
    ImageDraw.Draw(m).ellipse((0, 0, d * 4 - 1, d * 4 - 1), fill=255)
    out = Image.new("RGBA", (d, d))
    out.paste(im, (0, 0), m.resize((d, d), Image.LANCZOS))
    return out


def disc(d, color):
    m = Image.new("L", (d * 4, d * 4), 0)
    ImageDraw.Draw(m).ellipse((0, 0, d * 4 - 1, d * 4 - 1), fill=255)
    out = Image.new("RGBA", (d, d), color + (0,))
    out.putalpha(m.resize((d, d), Image.LANCZOS))
    return out


def soft_disc(d, color, blur, opacity):
    """A blurred disc on a padded canvas (so the blur never gets clipped into a visible square)."""
    pad = blur * 3
    im = Image.new("RGBA", (d + 2 * pad, d + 2 * pad), color + (0,))
    im.alpha_composite(disc(d, color), (pad, pad))
    im = im.filter(ImageFilter.GaussianBlur(blur))
    im.putalpha(im.split()[-1].point(lambda a: int(a * opacity)))
    return im


def tagline_of(description):
    """A short tagline from the channel description ("An element of truth"), or ''."""
    first = (description or "").strip().split("\n")[0]
    for sep in (" - ", " – ", " | ", ". ", "! "):
        first = first.split(sep)[0]
    first = first.strip(" .!-–|")
    return first if 6 <= len(first) <= 46 and "http" not in first and "@" not in first else ""


def text_shadow(canvas, xy, text, f, fill, blur=10, alpha=170, anchor="mm", spacing=0):
    layer = Image.new("RGBA", canvas.size, (0, 0, 0, 0))
    ImageDraw.Draw(layer).text((xy[0], xy[1] + 4), text, font=f, fill=(0, 0, 0, alpha), anchor=anchor)
    canvas.alpha_composite(layer.filter(ImageFilter.GaussianBlur(blur)))
    ImageDraw.Draw(canvas).text(xy, text, font=f, fill=fill, anchor=anchor)


def tracked(draw, cx, y, text, f, fill, track):
    """Centred text with extra letter spacing (for small-caps labels)."""
    widths = [draw.textlength(ch, font=f) for ch in text]
    total = sum(widths) + track * (len(text) - 1)
    x = cx - total / 2
    for ch, w in zip(text, widths):
        draw.text((x, y), ch, font=f, fill=fill, anchor="ls")
        x += w + track


def fit_title(draw, text, max_w, max_lines=2, start=124, smallest=54, weight=800):
    words = text.split()
    for size in range(start, smallest - 1, -4):
        f = font("Montserrat.ttf", size, weight)
        best = None
        if len(words) == 1 or max_lines == 1:
            cands = [[text]]
        else:
            cands = [[text]] + [[" ".join(words[:i]), " ".join(words[i:])] for i in range(1, len(words))]
        for lines in cands:
            wmax = max(draw.textlength(l, font=f) for l in lines)
            if wmax <= max_w and (best is None or (len(lines), wmax) < (len(best[0]), best[1])):
                best = (lines, wmax)
        if best:
            return f, best[0], size
    f = font("Montserrat.ttf", smallest, weight)
    return f, [text], smallest


# ---------------------------------------------------------------- episode thumbnail
def _dark_run(g, vertical, from_start, limit):
    w, h = g.size
    n = 0
    rng = range(h) if vertical else range(w)
    for k in (rng if from_start else reversed(rng)):
        box = (0, k, w, k + 1) if vertical else (k, 0, k + 1, h)
        if g.crop(box).getextrema()[1] > 42:
            break
        n += 1
        if n >= limit:
            break
    return n


def crop_bars(im):
    """Remove letterbox/pillarbox bars (uniform dark bands on opposite sides)."""
    g = im.convert("L")
    w, h = g.size
    t, b = _dark_run(g, True, True, h // 3), _dark_run(g, True, False, h // 3)
    l, r = _dark_run(g, False, True, w // 3), _dark_run(g, False, False, w // 3)
    box = [0, 0, w, h]
    if t > h * 0.02 and b > h * 0.02 and abs(t - b) <= max(6, h * 0.03):
        box[1], box[3] = t, h - b
    if l > w * 0.02 and r > w * 0.02 and abs(l - r) <= max(6, w * 0.03):
        box[0], box[2] = l, w - r
    return im.crop(tuple(box)), (box != [0, 0, w, h])


def episode_thumb(info, out_path, W=1280, H=720):
    """Best available thumbnail -> bars cropped -> 16:9 -> JPEG. Returns a small report dict."""
    thumbs = sorted(info.get("thumbnails") or [], key=lambda t: (t.get("preference") or -99, (t.get("width") or 0) * (t.get("height") or 0)))
    last_err = None
    for t in reversed(thumbs):
        url = t.get("url") or ""
        if not url or "maxres" not in url and (t.get("width") or 0) and (t.get("width") or 0) < 400:
            continue
        try:
            im = rgb(fetch(url))
        except Exception as e:
            last_err = e
            continue
        if im.width < 320:
            continue
        src = "%s %dx%d" % (t.get("id"), im.width, im.height)
        im, cropped = crop_bars(im)
        im = cover(im, W, H) if im.width >= W else cover(im, W, H).filter(ImageFilter.UnsharpMask(1.2, 60, 2))
        save_jpg(im, out_path)
        return {"source": src, "bars_cropped": cropped, "size": [W, H]}
    raise RuntimeError("no usable thumbnail (%s)" % last_err)


# ---------------------------------------------------------------- show art
class ChannelArt:
    def __init__(self, title, avatar=None, banner=None, subtitle="", tagline=""):
        self.title = title
        self.avatar = rgb(avatar) if avatar is not None else None
        self.banner = rgb(banner) if banner is not None else None
        self.subtitle = subtitle
        self.tagline = tagline
        self.accent = accent_of(self.avatar or self.banner) if (self.avatar or self.banner) else (235, 38, 52)

    # ---- backgrounds
    def _backdrop(self, w, h, strong_blur=True):
        """Blurred banner, or a generated gradient with the avatar as a soft light source."""
        if self.banner is not None:
            bg = cover(self.banner, w, h).filter(ImageFilter.GaussianBlur(34 if strong_blur else 12))
            bg = ImageEnhance.Brightness(bg).enhance(0.42)
            bg = ImageEnhance.Color(bg).enhance(1.15)
        else:
            bg = vgradient(w, h, shade(self.accent, 0.42), (10, 10, 14))
            if self.avatar is not None:
                glow = cover(self.avatar, int(w * 1.3), int(w * 1.3)).filter(ImageFilter.GaussianBlur(w // 9))
                glow = ImageEnhance.Brightness(glow).enhance(0.55)
                layer = Image.new("RGB", (w, h), (0, 0, 0))
                layer.paste(glow, ((w - glow.width) // 2, -glow.height // 3))
                bg = Image.blend(bg, layer, 0.55)
        return bg

    def poster(self, year=None, W=1000, H=1500, series=None):
        canvas = self._backdrop(W, H).convert("RGBA")
        hero_h = int(W * 9 / 16)
        if self.banner is not None:        # banner on top, softened so its baked-in wordmark never reads as a 2nd title
            hero = cover(self.banner, W, hero_h).filter(ImageFilter.GaussianBlur(17))
            hero = ImageEnhance.Color(ImageEnhance.Brightness(hero).enhance(0.82)).enhance(1.2).convert("RGBA")
            hero.putalpha(alpha_ramp(W, hero_h, 255, 0, True, 0.55, 1.0))
            canvas.alpha_composite(hero, (0, 0))
        # darken towards the bottom for text contrast
        shade_l = Image.new("RGBA", (W, H), (6, 6, 10, 255))
        shade_l.putalpha(alpha_ramp(W, H, 0, 235, True, 0.35, 0.98))
        canvas.alpha_composite(shade_l)
        # avatar medallion straddling the banner's lower edge
        d = 380
        cx, cy = W // 2, hero_h if self.banner is not None else int(H * 0.30)
        if self.avatar is not None:
            sh = soft_disc(d + 40, (0, 0, 0), 24, 0.8)
            canvas.alpha_composite(sh, (cx - sh.width // 2, cy - sh.height // 2 + 18))
            canvas.alpha_composite(disc(d + 26, self.accent), (cx - (d + 26) // 2, cy - (d + 26) // 2))
            canvas.alpha_composite(disc(d + 10, (12, 12, 16)), (cx - (d + 10) // 2, cy - (d + 10) // 2))
            canvas.alpha_composite(circle(self.avatar, d), (cx - d // 2, cy - d // 2))
        draw = ImageDraw.Draw(canvas)
        # title
        top = cy + d // 2 + 70
        f, lines, size = fit_title(draw, self.title.upper(), W - 130)
        lh = int(size * 1.08)
        y0 = top + lh // 2
        for i, line in enumerate(lines):
            text_shadow(canvas, (W // 2, y0 + i * lh), line, f, (246, 246, 248))
        draw = ImageDraw.Draw(canvas)
        rule_y = y0 + (len(lines) - 1) * lh + int(size * 0.62) + 30
        draw.rounded_rectangle((W // 2 - 64, rule_y, W // 2 + 64, rule_y + 7), 3, fill=self.accent)
        if series:
            tracked(draw, W // 2, rule_y + 70, "SERIES", font("Inter.ttf", 30, 600), (214, 214, 220), 9)
            words = series.upper().split()
            for size_s in range(110, 49, -6):
                sf = font("BebasNeue.ttf", size_s)
                lines_s = [series.upper()]
                if draw.textlength(lines_s[0], font=sf) > W - 120 and len(words) > 1:
                    cut = max(range(1, len(words)), key=lambda k: -abs(len(" ".join(words[:k])) - len(" ".join(words[k:]))))
                    lines_s = [" ".join(words[:cut]), " ".join(words[cut:])]
                if all(draw.textlength(l, font=sf) <= W - 120 for l in lines_s):
                    break
            for i, line in enumerate(lines_s):
                text_shadow(canvas, (W // 2, rule_y + 70 + 40 + size_s // 2 + i * int(size_s * 0.95)), line, sf, self.accent,
                            blur=12, alpha=200)
            draw = ImageDraw.Draw(canvas)
        elif year is None:
            y = rule_y + 62
            if self.subtitle:
                tracked(draw, W // 2, y, self.subtitle.upper(), font("Inter.ttf", 30, 600), (214, 214, 220), 5)
                y += 58
            if self.tagline:
                draw.text((W // 2, y), "“%s”" % self.tagline, font=font("Inter.ttf", 30, 400),
                          fill=(170, 170, 180), anchor="ms")
        else:
            tracked(draw, W // 2, rule_y + 70, "SEASON", font("Inter.ttf", 30, 600), (214, 214, 220), 9)
            yf = font("BebasNeue.ttf", 200)
            text_shadow(canvas, (W // 2, rule_y + 70 + 118), str(year), yf, self.accent, blur=14, alpha=200)
            draw = ImageDraw.Draw(canvas)
        tracked(draw, W // 2, H - 44, "YOUTUBE", font("Inter.ttf", 22, 700), (150, 150, 158), 11)
        return grain(canvas.convert("RGB"))

    def background(self, W=1920, H=1080):
        if self.banner is not None:
            bg = ImageEnhance.Brightness(cover(self.banner, W, H)).enhance(0.88).convert("RGBA")
        else:
            bg = self._backdrop(W, H).convert("RGBA")
            if self.avatar is not None:
                d = 620
                glow = soft_disc(d + 80, self.accent, 60, 0.45)
                bg.alpha_composite(glow, (int(W * 0.70) - glow.width // 2, H // 2 - glow.height // 2))
                bg.alpha_composite(circle(self.avatar, d), (int(W * 0.70) - d // 2, H // 2 - d // 2))
        left = Image.new("RGBA", (W, H), (0, 0, 0, 255))              # readable area for Plex's text on the left
        left.putalpha(alpha_ramp(W, H, 150, 0, False, 0.0, 0.55))
        bg.alpha_composite(left)
        bottom = Image.new("RGBA", (W, H), (0, 0, 0, 255))
        bottom.putalpha(alpha_ramp(W, H, 0, 150, True, 0.6, 1.0))
        bg.alpha_composite(bottom)
        return grain(bg.convert("RGB"), 0.025)


def thumbs_background(paths, W=1920, H=1080, gap=6):
    """Show background from the channel's own recent episode thumbnails: a 2x2 mosaic (4+ thumbnails) or the newest
    one full-bleed, with the same left/bottom shading as ChannelArt.background() so Plex's text stays readable.
    Returns None without thumbnails (the caller keeps the banner-based background)."""
    ims = []
    for p in paths:
        try:
            ims.append(rgb(Image.open(p)))
        except Exception:
            continue
    if not ims:
        return None
    if len(ims) >= 4:
        bg = Image.new("RGB", (W, H), (10, 10, 14))
        cw, chh = (W - gap) // 2, (H - gap) // 2
        for i, im in enumerate(ims[:4]):
            r, c = divmod(i, 2)
            bg.paste(cover(crop_bars(im)[0], cw, chh), (c * (cw + gap), r * (chh + gap)))
    else:
        bg = cover(crop_bars(ims[0])[0], W, H)
    bg = ImageEnhance.Brightness(bg).enhance(0.92).convert("RGBA")
    left = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    left.putalpha(alpha_ramp(W, H, 150, 0, False, 0.0, 0.55))
    bg.alpha_composite(left)
    bottom = Image.new("RGBA", (W, H), (0, 0, 0, 255))
    bottom.putalpha(alpha_ramp(W, H, 0, 150, True, 0.6, 1.0))
    bg.alpha_composite(bottom)
    return grain(bg.convert("RGB"), 0.02)


TOPIC_ACCENT = {"Cars & Builds": (238, 96, 44), "Tech & PCs": (40, 190, 230), "Science & Engineering": (60, 200, 120),
                "Space": (150, 110, 240), "Aviation": (70, 150, 250), "Gaming": (230, 70, 200),
                "History & Stories": (225, 170, 60), "Makers & DIY": (250, 200, 50), "Music": (245, 90, 140),
                "Other": (150, 160, 180),
                # YouTube's own categories (topics since the no-AI rule)
                "Autos & Vehicles": (238, 96, 44), "Science & Technology": (40, 190, 230), "Education": (60, 200, 120),
                "Entertainment": (230, 70, 200), "People & Blogs": (225, 170, 60), "Howto & Style": (250, 200, 50),
                "Film & Animation": (150, 110, 240), "Comedy": (245, 150, 60), "News & Politics": (200, 60, 60),
                "Travel & Events": (70, 150, 250), "Sports": (40, 200, 160), "Pets & Animals": (160, 200, 80),
                "Nonprofits & Activism": (120, 170, 220)}


def topic_poster(topic, avatars, count, W=1000, H=1500):
    """Collection poster in the channel-poster style: a mosaic of the topic's channel avatars under the topic name."""
    accent = TOPIC_ACCENT.get(topic, (235, 38, 52))
    canvas = vgradient(W, H, shade(accent, 0.34), (8, 8, 12)).convert("RGBA")
    n = min(9, len(avatars))
    cols = 3 if n > 4 else max(1, min(2, n))
    rows = max(1, (n + cols - 1) // cols)
    d = 230 if cols == 3 else 300
    gap = 34
    gw = cols * d + (cols - 1) * gap
    y0 = 150 if rows == 3 else 240
    for i, av in enumerate(avatars[:n]):
        r, cidx = divmod(i, cols)
        in_row = min(cols, n - r * cols)
        x = (W - (in_row * d + (in_row - 1) * gap)) // 2 + cidx * (d + gap)
        y = y0 + r * (d + gap)
        sh = soft_disc(d + 16, (0, 0, 0), 14, 0.7)
        canvas.alpha_composite(sh, (x + d // 2 - sh.width // 2, y + d // 2 - sh.height // 2 + 10))
        canvas.alpha_composite(disc(d + 10, accent), (x - 5, y - 5))
        canvas.alpha_composite(circle(av, d), (x, y))
    shade_l = Image.new("RGBA", (W, H), (6, 6, 10, 255))
    shade_l.putalpha(alpha_ramp(W, H, 0, 245, True, 0.5, 0.95))
    canvas.alpha_composite(shade_l)
    draw = ImageDraw.Draw(canvas)
    top = y0 + rows * (d + gap) + 60
    f, lines, size = fit_title(draw, topic.upper(), W - 120, max_lines=2, start=116)
    lh = int(size * 1.08)
    for i, line in enumerate(lines):
        text_shadow(canvas, (W // 2, top + lh // 2 + i * lh), line, f, (246, 246, 248))
    draw = ImageDraw.Draw(canvas)
    rule_y = top + lh // 2 + (len(lines) - 1) * lh + int(size * 0.62) + 30
    draw.rounded_rectangle((W // 2 - 64, rule_y, W // 2 + 64, rule_y + 7), 3, fill=accent)
    tracked(draw, W // 2, rule_y + 64, ("%d CHANNELS" % count) if count != 1 else "1 CHANNEL",
            font("Inter.ttf", 30, 600), (214, 214, 220), 6)
    tracked(draw, W // 2, H - 44, "YOUTUBE", font("Inter.ttf", 22, 700), (150, 150, 158), 11)
    return grain(canvas.convert("RGB"))


def channel_images(ch):
    """(avatar, banner) PIL images from a yt-dlp channel dict (None when missing)."""
    urls = {t.get("id"): t.get("url") for t in ch.get("thumbnails") or []}
    out = []
    for key in ("avatar_uncropped", "banner_uncropped"):
        im = None
        if urls.get(key):
            try:
                im = fetch(urls[key])
            except Exception as e:
                log.warning("%s fetch failed: %s", key, e)
        out.append(im)
    return tuple(out)
