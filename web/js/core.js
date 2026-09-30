/* Tubarr web UI: core helpers. Plain classic script (no modules) so index.html also works from file://. */
(function () {
  'use strict';
  const T = (window.T = window.T || {});
  T.views = T.views || {};

  /* ---------- Auto-escaping HTML templates ----------
     html`<b>${text}</b>` escapes every interpolated value unless it is itself an html`` result (Raw)
     or an array of them. Use it for everything that touches API data. */
  class Raw { constructor(s) { this.s = s; } toString() { return this.s; } }
  const ESC = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;', "'": '&#39;' };
  const esc = (s) => String(s).replace(/[&<>"']/g, (c) => ESC[c]);
  function toHtml(v) {
    if (v == null || v === false || v === true) return '';
    if (v instanceof Raw) return v.s;
    if (Array.isArray(v)) return v.map(toHtml).join('');
    return esc(v);
  }
  function html(strings, ...vals) {
    let out = strings[0];
    for (let i = 0; i < vals.length; i++) out += toHtml(vals[i]) + strings[i + 1];
    return new Raw(out);
  }
  T.html = html;
  T.raw = (s) => new Raw(String(s));
  T.esc = esc;
  T.Raw = Raw;

  /* ---------- DOM helpers ---------- */
  T.$ = (sel, root) => (root || document).querySelector(sel);
  T.$$ = (sel, root) => Array.from((root || document).querySelectorAll(sel));
  T.setHTML = (el, raw) => { if (el) el.innerHTML = raw instanceof Raw ? raw.s : toHtml(raw); };
  T.textOf = (raw) => { const t = document.createElement('span'); T.setHTML(t, raw); return t.textContent; };
  T.debounce = (fn, ms) => { let t; return (...a) => { clearTimeout(t); t = setTimeout(() => fn(...a), ms); }; };
  T.sleep = (ms) => new Promise((r) => setTimeout(r, ms));
  T.clamp = (x, a, b) => Math.min(b, Math.max(a, x));
  T.reducedMotion = () => window.matchMedia && window.matchMedia('(prefers-reduced-motion: reduce)').matches;

  /* ---------- Event bus ---------- */
  const listeners = {};
  T.on = (evt, fn) => { (listeners[evt] = listeners[evt] || new Set()).add(fn); return () => listeners[evt].delete(fn); };
  T.emit = (evt, data) => { (listeners[evt] || []).forEach((fn) => { try { fn(data); } catch (e) { console.error(e); } }); };

  /* ---------- Per-browser conveniences (never app state) ---------- */
  T.prefs = {
    get(k, d) { try { const v = localStorage.getItem('tubarr.' + k); return v == null ? d : JSON.parse(v); } catch (e) { return d; } },
    set(k, v) { try { localStorage.setItem('tubarr.' + k, JSON.stringify(v)); } catch (e) { /* private mode etc. */ } },
  };

  /* ---------- Formatting ---------- */
  const nf = new Intl.NumberFormat('en-US');
  T.fmtNum = (n) => (n == null ? '—' : nf.format(n));
  T.fmtCompact = (n) => {
    if (n == null) return '—';
    if (n >= 1e9) return trim(n / 1e9, 2) + 'B';
    if (n >= 1e6) return trim(n / 1e6, n >= 1e7 ? 1 : 2) + 'M';
    if (n >= 1e3) return trim(n / 1e3, n >= 1e5 ? 0 : 1) + 'K';
    return String(n);
  };
  function trim(x, d) { return String(Number(x.toFixed(d))); }

  /* Sizes: 1024-based like `df -h`, labelled GB/TB (a "3 TB" quota is 3 TiB). */
  T.fmtBytes = (b, opts) => {
    if (b == null) return '—';
    const o = opts || {};
    const u = ['B', 'KB', 'MB', 'GB', 'TB', 'PB'];
    let i = 0, v = Math.abs(b);
    while (v >= 1024 && i < u.length - 1) { v /= 1024; i++; }
    let d = i === 0 ? 0 : o.digits != null ? o.digits : (i >= 4 ? 2 : i === 3 ? (v >= 100 ? 0 : 1) : 0);
    if (o.unit === false) return v.toFixed(d);
    return (b < 0 ? '-' : '') + v.toFixed(d) + (o.nbsp === false ? ' ' : ' ') + u[i];
  };
  T.bytesIn = (b, unit) => b / Math.pow(1024, { KB: 1, MB: 2, GB: 3, TB: 4 }[unit]);
  T.fmtSpeed = (bps) => (bps == null ? '—' : (bps / 1048576).toFixed(1) + ' MB/s');

  /* 1342 -> "22:22", 4000 -> "1:06:40" */
  T.fmtClock = (s) => {
    if (s == null) return '—';
    s = Math.round(s);
    const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    return (h ? h + ':' + String(m).padStart(2, '0') : m) + ':' + String(x).padStart(2, '0');
  };
  /* 5820 -> "1h 37m", 97 -> "1m 37s", 40 -> "40s" */
  T.fmtSpan = (s, parts) => {
    if (s == null) return '—';
    s = Math.max(0, Math.round(s));
    const d = Math.floor(s / 86400), h = Math.floor((s % 86400) / 3600), m = Math.floor((s % 3600) / 60), x = s % 60;
    const out = [];
    if (d) out.push(d + 'd');
    if (h) out.push(h + 'h');
    if (m && out.length < 2) out.push(m + 'm');
    if (!d && !h && (x || !out.length)) out.push(x + 's');
    return out.slice(0, parts || 2).join(' ');
  };

  const MON = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];
  const parseDay = (s) => { const [y, m, d] = s.split('-').map(Number); return new Date(y, m - 1, d); };
  T.parseDay = parseDay;
  T.fmtDay = (s, withYear) => {
    if (!s) return '—';
    const d = typeof s === 'string' && s.length === 10 ? parseDay(s) : new Date(s);
    const y = withYear === undefined ? d.getFullYear() !== new Date().getFullYear() : withYear;
    return MON[d.getMonth()] + ' ' + d.getDate() + (y ? ', ' + d.getFullYear() : '');
  };
  T.fmtTime = (iso) => {
    if (!iso) return '—';
    const d = new Date(iso);
    let h = d.getHours(); const m = d.getMinutes(); const ap = h >= 12 ? 'PM' : 'AM';
    h = h % 12 || 12;
    return h + ':' + String(m).padStart(2, '0') + ' ' + ap;
  };
  T.fmtHHMM = (hhmm) => { const [h, m] = hhmm.split(':').map(Number); const ap = h >= 12 ? 'PM' : 'AM'; return (h % 12 || 12) + (m ? ':' + String(m).padStart(2, '0') : '') + ' ' + ap; };
  /* Relative time: "just now", "12m ago", "in 3h", "yesterday", then a date. */
  T.fmtAgo = (iso) => {
    if (!iso) return '—';
    const t = typeof iso === 'number' ? iso : Date.parse(iso);
    const diff = ((T.now ? T.now() : Date.now()) - t) / 1000, a = Math.abs(diff), past = diff >= 0;
    if (a < 45) return past ? 'just now' : 'in a moment';
    const m = a / 60, h = m / 60, d = h / 24;
    let v;
    if (m < 59.5) v = Math.round(m) + 'm';
    else if (h < 23.5) v = Math.round(h) + 'h';
    else if (d < 2) return past ? 'yesterday' : 'tomorrow';
    else if (d < 7) v = Math.round(d) + 'd';
    else return T.fmtDay(new Date(t).toISOString());
    return past ? v + ' ago' : 'in ' + v;
  };
  T.fmtUntil = (iso) => T.fmtSpan((Date.parse(iso) - (T.now ? T.now() : Date.now())) / 1000);
  T.pct = (x, d) => (x == null ? '—' : (x * 100).toFixed(d || 0) + '%');
  T.plural = (n, one, many) => T.fmtNum(n) + ' ' + (n === 1 ? one : many || one + 's');

  /* ---------- Deterministic hash + PRNG (placeholder art, mock data) ---------- */
  T.hash = (str) => {
    let h = 0x811c9dc5;
    for (let i = 0; i < str.length; i++) { h ^= str.charCodeAt(i); h = Math.imul(h, 0x01000193); }
    return h >>> 0;
  };
  T.rng = (seed) => {
    let a = typeof seed === 'string' ? T.hash(seed) : seed >>> 0;
    return function () {
      a = (a + 0x6d2b79f5) | 0;
      let t = Math.imul(a ^ (a >>> 15), 1 | a);
      t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t;
      return ((t ^ (t >>> 14)) >>> 0) / 4294967296;
    };
  };

  /* ---------- Generated placeholder art (posters, banners, thumbnails) ----------
     Colour pairs are hand-tuned (bright key colour -> deep base) so every poster looks deliberate. */
  const PALETTES = [
    ['#ff7a45', '#4a1030'], ['#3ec6ff', '#101f5c'], ['#8f7bff', '#1e0f4d'], ['#2fe0a6', '#0a3d4a'],
    ['#ffb347', '#6e1f0e'], ['#ff5fa2', '#2f0f55'], ['#5eead4', '#0f3a44'], ['#f97316', '#3a1206'],
    ['#b5e655', '#113d26'], ['#60a5fa', '#27206b'], ['#f472b6', '#5c0f35'], ['#fbbf24', '#5c2a0b'],
    ['#38bdf8', '#0a3354'], ['#c084fc', '#3a137a'], ['#fb7185', '#43071a'], ['#34d399', '#053b2d'],
  ];
  T.palette = (key) => PALETTES[T.hash(key) % PALETTES.length];

  const STOP = new Set(['the', 'a', 'an', 'of', 'and', 'tv', 'co']);
  T.initials = (title) => {
    const words = String(title).replace(/[^\p{L}\p{N}\s]/gu, ' ').split(/\s+/).filter(Boolean);
    if (!words.length) return '?';
    if (/^[A-Z0-9]{2,4}$/.test(words[0])) return words[0];
    const sig = words.filter((w) => !STOP.has(w.toLowerCase()));
    const use = sig.length ? sig : words;
    if (use.length === 1) {
      const w = use[0];
      const caps = w.match(/\p{Lu}/gu);
      if (caps && caps.length >= 2 && w !== w.toUpperCase()) return caps[0] + caps[1];
      return w[0].toUpperCase();
    }
    return (use[0][0] + use[1][0]).toUpperCase();
  };

  /* Poster: real image when the API has one, generated art underneath as the fallback. */
  T.posterArt = (ch) => {
    const h = T.hash(ch.title || ch.id || '?');
    const [c1, c2] = PALETTES[h % PALETTES.length];
    const longest = String(ch.title || '').split(/\s+/).reduce((m, w) => Math.max(m, w.length), 0);
    return html`<div class="ph" data-m="${(h >>> 7) % 6}" style="--c1:${c1};--c2:${c2}" aria-hidden="true"><span class="ph-ini">${T.initials(ch.title || '?')}</span><span class="ph-name${longest > 11 ? ' is-long' : ''}">${ch.title}</span></div>`;
  };
  T.poster = (ch, cls) => html`<div class="poster ${cls || ''}">${T.posterArt(ch)}${ch.poster_url ? html`<img src="${ch.poster_url}" alt="" loading="lazy" decoding="async" data-fallback>` : ''}</div>`;

  T.bannerArt = (ch) => {
    const h = T.hash((ch.title || '') + '#banner');
    const [c1, c2] = T.palette(ch.title || '');
    return html`<div class="bph" data-m="${h % 4}" style="--c1:${c1};--c2:${c2}" aria-hidden="true"><i></i><i></i><i></i></div>`;
  };

  const THUMB_STOP = new Set(['the', 'a', 'an', 'of', 'and', 'to', 'in', 'on', 'for', 'is', 'it', 'my', 'we', 'i', 'you', 'this', 'that', 'with', 'how', 'why', 'what', 'from', 'are', 'was', 'at', 'vs', 'be', 'your', 'our', 'about', 'actually', 'part', 'finally', 'nobody', 'every', 'ever', 'most', 'really', 'without', 'into', 'after', 'again', 'just', 'here', "don't", 'did', 'does', 'will', 'can', 'so', 'than', 'then']);
  function thumbWord(title) {
    const words = String(title).replace(/[^\p{L}\p{N}'\s-]/gu, ' ').split(/\s+/).filter(Boolean);
    const num = words.findIndex((w) => /\d/.test(w) && w.length <= 6);
    if (num >= 0 && words[num + 1] && !THUMB_STOP.has(words[num + 1].toLowerCase())) return words[num] + ' ' + words[num + 1];
    const cands = words.filter((w) => !THUMB_STOP.has(w.toLowerCase()) && w.length >= 3);
    if (!cands.length) return words.slice(0, 2).join(' ');
    return cands.reduce((a, b) => (b.length > a.length ? b : a));
  }
  T.thumbArt = (v) => {
    const h = T.hash(v.id || v.title || '?');
    const [c1, c2] = PALETTES[(h >>> 5) % PALETTES.length];
    const word = thumbWord(v.title || '');
    return html`<div class="tph" data-m="${h % 4}" style="--c1:${c1};--c2:${c2}" aria-hidden="true"><span class="tph-word${word.length > 9 ? ' is-long' : ''}">${word}</span></div>`;
  };
  T.thumb = (v, cls, extra) => html`<div class="thumb ${cls || ''}">${T.thumbArt(v)}${v.thumbnail_url ? html`<img src="${v.thumbnail_url}" alt="" loading="lazy" decoding="async" data-fallback>` : ''}${extra || ''}</div>`;

  /* A broken image URL just disappears and the generated art underneath shows through. */
  document.addEventListener('error', (e) => {
    const t = e.target;
    if (t && t.tagName === 'IMG' && t.hasAttribute('data-fallback')) t.remove();
  }, true);

  /* ---------- Icons: one inline SVG sprite, drawn for Tubarr (24px grid, stroked) ---------- */
  const ICONS = {
    tv: '<rect x="3" y="7" width="18" height="13" rx="2.5"/><path d="m8 3 4 4 4-4"/>',
    activity: '<path d="M3 12h4l3-7.5 4 15 3-7.5h4"/>',
    storage: '<ellipse cx="12" cy="5.5" rx="8" ry="2.5"/><path d="M4 5.5v13c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5v-13"/><path d="M4 12c0 1.4 3.6 2.5 8 2.5s8-1.1 8-2.5"/>',
    sliders: '<path d="M4 6h9M17 6h3M4 12h3M11 12h9M4 18h11M19 18h1"/><circle cx="15" cy="6" r="2"/><circle cx="9" cy="12" r="2"/><circle cx="17" cy="18" r="2"/>',
    search: '<circle cx="11" cy="11" r="7"/><path d="m20 20-3.5-3.5"/>',
    plus: '<path d="M12 5v14M5 12h14"/>',
    x: '<path d="M18 6 6 18M6 6l12 12"/>',
    check: '<path d="m5 12.5 4.5 4.5L19 7.5"/>',
    'chev-down': '<path d="m6 9 6 6 6-6"/>',
    'chev-right': '<path d="m9 6 6 6-6 6"/>',
    'chev-left': '<path d="m15 6-6 6 6 6"/>',
    'arrow-left': '<path d="M19 12H5M11 18l-6-6 6-6"/>',
    refresh: '<path d="M20 11a8 8 0 0 0-14.6-4.5L4 8"/><path d="M4 3.5V8h4.5"/><path d="M4 13a8 8 0 0 0 14.6 4.5L20 16"/><path d="M20 20.5V16h-4.5"/>',
    sparkles: '<path d="M11 3.5 12.7 8.3 17.5 10 12.7 11.7 11 16.5 9.3 11.7 4.5 10 9.3 8.3Z"/><path d="M18.5 14.5 19.2 16.3 21 17 19.2 17.7 18.5 19.5 17.8 17.7 16 17 17.8 16.3Z"/>',
    trash: '<path d="M4 7h16M10 11v6M14 11v6M6 7l1 12a2 2 0 0 0 2 2h6a2 2 0 0 0 2-2l1-12M9 7V4.5A1.5 1.5 0 0 1 10.5 3h3A1.5 1.5 0 0 1 15 4.5V7"/>',
    undo: '<path d="M9 14 4 9l5-5"/><path d="M4 9h10.5a5.5 5.5 0 0 1 0 11H11"/>',
    alert: '<path d="M10.3 4.2 2.6 17.5A2 2 0 0 0 4.3 20.5h15.4a2 2 0 0 0 1.7-3L13.7 4.2a2 2 0 0 0-3.4 0Z"/><path d="M12 9.5v4M12 17h.01"/>',
    'alert-circle': '<circle cx="12" cy="12" r="9"/><path d="M12 7.5v5M12 16.2h.01"/>',
    info: '<circle cx="12" cy="12" r="9"/><path d="M12 11v5.5M12 7.8h.01"/>',
    clock: '<circle cx="12" cy="12" r="9"/><path d="M12 7v5l3 2"/>',
    play: '<path d="M8 5.5v13a1 1 0 0 0 1.5.9l10.2-6.5a1 1 0 0 0 0-1.8L9.5 4.6A1 1 0 0 0 8 5.5Z"/>',
    pause: '<rect x="6.5" y="5" width="4" height="14" rx="1"/><rect x="13.5" y="5" width="4" height="14" rx="1"/>',
    scissors: '<circle cx="6" cy="7" r="2.8"/><circle cx="6" cy="17" r="2.8"/><path d="M8.3 8.7 20 17M8.3 15.3 20 7"/>',
    eye: '<path d="M2.5 12S6 5.5 12 5.5 21.5 12 21.5 12 18 18.5 12 18.5 2.5 12 2.5 12Z"/><circle cx="12" cy="12" r="3"/>',
    'eye-off': '<path d="M3 3l18 18"/><path d="M10.6 5.6A10 10 0 0 1 12 5.5c6 0 9.5 6.5 9.5 6.5a17 17 0 0 1-3 3.8M6.6 6.6C4 8.3 2.5 12 2.5 12S6 18.5 12 18.5a9.6 9.6 0 0 0 4.3-1"/><path d="M9.9 9.9a3 3 0 0 0 4.2 4.2"/>',
    ext: '<path d="M14 4h6v6M20 4l-9 9M18 14v4a2 2 0 0 1-2 2H6a2 2 0 0 1-2-2V8a2 2 0 0 1 2-2h4"/>',
    link: '<path d="M10 14a4.5 4.5 0 0 0 6.4 0l3-3a4.5 4.5 0 0 0-6.4-6.4l-1 1"/><path d="M14 10a4.5 4.5 0 0 0-6.4 0l-3 3a4.5 4.5 0 0 0 6.4 6.4l1-1"/>',
    subs: '<path d="M3 6h11M3 12h8M3 18h8"/><path d="m15 11 6 3.5-6 3.5Z"/>',
    film: '<rect x="3" y="3" width="18" height="18" rx="2.5"/><path d="M7.5 3v18M16.5 3v18M3 8h4.5M3 12h18M3 16h4.5M16.5 8H21M16.5 16H21"/>',
    server: '<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',
    bell: '<path d="M6 16v-5a6 6 0 0 1 12 0v5l1.5 2h-15Z"/><path d="M10 20.5a2 2 0 0 0 4 0"/>',
    download: '<path d="M12 4v11M7 10l5 5 5-5M4 19.5h16"/>',
    posters: '<rect x="3.5" y="3" width="7" height="10.5" rx="1.5"/><rect x="13.5" y="3" width="7" height="10.5" rx="1.5"/><path d="M3.5 17.5h7M13.5 17.5h7M3.5 21h4.5M13.5 21h4.5"/>',
    rows: '<path d="M9 6h11M9 12h11M9 18h11"/><path d="M4.5 6h.01M4.5 12h.01M4.5 18h.01"/>',
    sort: '<path d="M7 4v16M3.5 16.5 7 20l3.5-3.5M17 20V4M13.5 7.5 17 4l3.5 3.5"/>',
    infinity: '<path d="M12 12c-2-2.7-3.6-4-5.5-4a4 4 0 0 0 0 8c1.9 0 3.5-1.3 5.5-4Zm0 0c2 2.7 3.6 4 5.5 4a4 4 0 0 0 0-8c-1.9 0-3.5 1.3-5.5 4Z"/>',
    lock: '<rect x="5" y="11" width="14" height="10" rx="2"/><path d="M8 11V8a4 4 0 0 1 8 0v3"/>',
    moon: '<path d="M20 14.5A8 8 0 1 1 9.5 4a6.5 6.5 0 0 0 10.5 10.5Z"/>',
    sun: '<circle cx="12" cy="12" r="4"/><path d="M12 2.5v2M12 19.5v2M2.5 12h2M19.5 12h2M5.3 5.3l1.4 1.4M17.3 17.3l1.4 1.4M5.3 18.7l1.4-1.4M17.3 6.7l1.4-1.4"/>',
    zap: '<path d="M13 3 5 13.5h6L10 21l8-10.5h-6Z"/>',
    clipboard: '<rect x="6" y="4.5" width="12" height="16.5" rx="2"/><path d="M9 4.5v-.7a.8.8 0 0 1 .8-.8h4.4a.8.8 0 0 1 .8.8v.7"/><path d="M9.5 11h5M9.5 15h5"/>',
    login: '<path d="M14 4h4a2 2 0 0 1 2 2v12a2 2 0 0 1-2 2h-4M10 16l4-4-4-4M14 12H4"/>',
    logout: '<path d="M10 4H6a2 2 0 0 0-2 2v12a2 2 0 0 0 2 2h4M15 16l4-4-4-4M19 12H9"/>',
    more: '<path d="M5.5 12h.01M12 12h.01M18.5 12h.01"/>',
    offline: '<path d="M3 3l18 18"/><path d="M8.5 7.1A6 6 0 0 1 17.7 11H18a4 4 0 0 1 2 7.5M16 19H7a5 5 0 0 1-1.5-9.8"/>',
    shield: '<path d="M12 3 5 6v5.5c0 4.4 2.9 8 7 9.5 4.1-1.5 7-5.1 7-9.5V6Z"/><path d="m9 12 2 2 4-4"/>',
    calendar: '<rect x="3.5" y="5" width="17" height="15.5" rx="2"/><path d="M3.5 10h17M8 3v4M16 3v4"/>',
    user: '<circle cx="12" cy="8" r="4"/><path d="M4.5 20.5a7.5 7.5 0 0 1 15 0"/>',
    timer: '<circle cx="12" cy="13.5" r="7.5"/><path d="M12 9.5v4l2.5 2M9.5 2.5h5"/>',
    live: '<circle cx="12" cy="12" r="2"/><path d="M8.2 15.8a5.4 5.4 0 0 1 0-7.6M15.8 8.2a5.4 5.4 0 0 1 0 7.6M5.2 18.8a9.6 9.6 0 0 1 0-13.6M18.8 5.2a9.6 9.6 0 0 1 0 13.6"/>',
    short: '<rect x="7" y="2.5" width="10" height="19" rx="2.5"/><path d="M11 18.5h2"/>',
    hourglass: '<path d="M6.5 3h11M6.5 21h11M7.5 3v3.5a4.5 4.5 0 0 0 9 0V3M7.5 21v-3.5a4.5 4.5 0 0 1 9 0V21"/>',
    image: '<rect x="3" y="4" width="18" height="16" rx="2.5"/><circle cx="9" cy="9.5" r="1.8"/><path d="m21 15.5-4.5-4.5L6 20"/>',
    'check-circle': '<circle cx="12" cy="12" r="9"/><path d="m8 12.5 2.8 2.7L16.5 9.5"/>',
    'x-circle': '<circle cx="12" cy="12" r="9"/><path d="m9 9 6 6M15 9l-6 6"/>',
    'minus-circle': '<circle cx="12" cy="12" r="9"/><path d="M8 12h8"/>',
    message: '<path d="M20 15a2 2 0 0 1-2 2H8l-4 4V6a2 2 0 0 1 2-2h12a2 2 0 0 1 2 2Z"/>',
    send: '<path d="M21 3 10 14M21 3l-7 18-4-7-7-4Z"/>',
    history: '<path d="M3.5 12a8.5 8.5 0 1 0 2.5-6L3.5 8.5"/><path d="M3.5 3.5v5h5"/><path d="M12 8v4.5l3 1.5"/>',
    list: '<path d="M8 6h13M8 12h13M8 18h13M3.5 6h.01M3.5 12h.01M3.5 18h.01"/>',
    inbox: '<path d="M3 13h5l1.5 2.5h5L16 13h5"/><path d="M5.5 5h13L21 13v5.5a1.5 1.5 0 0 1-1.5 1.5h-15A1.5 1.5 0 0 1 3 18.5V13Z"/>',
    file: '<path d="M14 3H7a2 2 0 0 0-2 2v14a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V8Z"/><path d="M14 3v5h5"/>',
    menu: '<path d="M4 7h16M4 12h16M4 17h16"/>',
    wand: '<path d="m4 20 11-11M14 4v2M19 9h2M17.5 5.5 19 4M9 4v2M4 9h2"/><path d="m13 7 4 4"/>',
    archive: '<rect x="3" y="4" width="18" height="5" rx="1.5"/><path d="M5 9v9a2 2 0 0 0 2 2h10a2 2 0 0 0 2-2V9M10 13h4"/>',
    layers: '<path d="m12 3 9 5-9 5-9-5Z"/><path d="m3 13 9 5 9-5"/>',
    tag: '<path d="M3 12V4.5A1.5 1.5 0 0 1 4.5 3H12l9 9-9 9Z"/><path d="M7.5 7.5h.01"/>',
  };
  T.iconSprite = () => {
    const sym = Object.entries(ICONS).map(([k, v]) => `<symbol id="i-${k}" viewBox="0 0 24 24">${v}</symbol>`).join('');
    return `<svg xmlns="http://www.w3.org/2000/svg" style="display:none" aria-hidden="true">${sym}</svg>`;
  };
  T.icon = (name, cls) => T.raw(`<svg class="ic ${cls || ''}" aria-hidden="true" focusable="false"><use href="#i-${name}"/></svg>`);

  /* The Tubarr mark: a little TV (rabbit-ear antenna) with a download arrow on its screen.
     Gradient ids are unique per copy: a gradient defined inside a hidden (display:none) copy would not paint elsewhere. */
  let markSeq = 0;
  T.logoMark = (cls) => {
    const g = 'tbg' + (++markSeq), s = 'tbs' + markSeq;
    return T.raw(`<svg class="mark ${cls || ''}" viewBox="0 0 48 48" aria-hidden="true" focusable="false">
    <defs>
      <linearGradient id="${g}" x1="4" y1="4" x2="44" y2="46" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#b09dff"/><stop offset=".5" stop-color="#7563ff"/><stop offset="1" stop-color="#3f8cff"/></linearGradient>
      <radialGradient id="${s}" cx="24" cy="26" r="16" gradientUnits="userSpaceOnUse"><stop offset="0" stop-color="#231d52"/><stop offset="1" stop-color="#0b0d16"/></radialGradient>
    </defs>
    <path d="M16.5 4.5 24 11.5l7.5-7" fill="none" stroke="url(#${g})" stroke-width="3.4" stroke-linecap="round" stroke-linejoin="round"/>
    <rect x="3" y="11.5" width="42" height="32.5" rx="10.5" fill="url(#${g})"/>
    <rect x="8.5" y="17" width="31" height="21.5" rx="6" fill="url(#${s})"/>
    <path d="M24 21.3v10.4M19.2 27.3 24 32l4.8-4.7" fill="none" stroke="#fff" stroke-width="3.1" stroke-linecap="round" stroke-linejoin="round"/>
  </svg>`);
  };
})();
