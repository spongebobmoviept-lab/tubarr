/* Tubarr mock backend: implements web/API.md in memory, including the live event stream and the
   fill-and-roll storage model, so the UI can be previewed without the server. Loaded only in mock mode.
   Deterministic (seeded) so screenshots are stable; time-based parts move live (downloads progress,
   new uploads arrive every ~40 s, and when the library is full each arrival rolls one video out).
   Scenarios (?scenario=): default = filling the backlog · full = at the target and rolling ·
   fresh = empty, first run (the setup wizard shows) · trouble = errors, downloads paused, the proxy failing. */
(function () {
  'use strict';
  const T = window.T;
  const SEED = window.TUBARR_SEED || [];
  const ART = window.TUBARR_MOCK_ART || {};
  const { GENRES, fill, SPECIAL, OVERRIDES, GENRE_TOPIC, TOPIC_OVERRIDE, SERIES, PART_SUBTITLES } = window.TUBARR_MOCK_DATA;
  const TOPICS = [['cars', 'Cars & Builds'], ['tech', 'Tech & PCs'], ['science', 'Science & Engineering'], ['space', 'Space'], ['aviation', 'Aviation'], ['gaming', 'Gaming'], ['history', 'History & Stories'], ['makers', 'Makers & DIY'], ['music', 'Music'], ['other', 'Other']];
  const Q = new URLSearchParams(location.search);
  const SCENARIO = (Q.get('scenario') || 'default').toLowerCase();
  const FULL = SCENARIO === 'full', FRESH = SCENARIO === 'fresh';

  const MIN = 60e3, HOUR = 36e5, DAY = 864e5;
  const MB = 1048576, GB = 1073741824, TIB = 1099511627776;
  const CAP = 4 * TIB;
  const CATALOG_MAX = 360;
  const NOW0 = Date.now();
  const START = NOW0 - (FULL ? 30 : 17) * DAY;          // the big initial import
  const FULL_AT = NOW0 - 4 * DAY;                        // (full scenario) when the fill reached the target
  const iso = (t) => (t == null ? null : new Date(t).toISOString().replace(/\.\d{3}Z$/, 'Z'));
  const pad = (n) => String(n).padStart(2, '0');
  const dayOf = (t) => { const d = new Date(t); return d.getFullYear() + '-' + pad(d.getMonth() + 1) + '-' + pad(d.getDate()); };
  const B64 = 'ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_';
  const rid = (r, n) => { let s = ''; for (let i = 0; i < n; i++) s += B64[Math.floor(r() * 64)]; return s; };
  const pick = (r, a) => a[Math.floor(r() * a.length)];
  const between = (r, a, b) => a + (b - a) * r();
  const clone = (o) => JSON.parse(JSON.stringify(o));
  const LISTED = (v) => v && v.state != null;
  const REMOVE_BY_ROLL = new Set(['watched', 'making_room']);

  const DEFAULT_DOWNLOADS = {
    fill_target_bytes: Math.round(2.5 * TIB), protect_newest: 3,
    quality: '2160p', max_height: 2160, allow_av1: false, max_duration_minutes: 180, include_live: false, skip_shorts: true,
    sponsorblock: ['sponsor', 'selfpromo', 'interaction'], sponsorblock_wait_hours: 6, check_interval_minutes: 5,
    pacing: { concurrency: 1, overnight_start: '01:00', overnight_end: '07:00', daytime: 'all', day_rate_limit_bps: 6 * MB, night_rate_limit_bps: null },
  };

  /* ---------- State ---------- */
  const S = {
    channels: [], byId: new Map(), byHandle: new Map(), videos: new Map(),
    queue: [], current: null, history: [], paused: false, histSeq: 1, ver: 1, frontier: null, rolling: FULL,
    settings: { downloads: clone(DEFAULT_DOWNLOADS), notifications: { discord_webhook_url: 'https://discord.com/api/webhooks/000000000000000000/example', events: { download_failed: true, channel_changes: true, storage_warning: true, daily_summary: true, each_download: false } } },
    plex: null, imports: {},
    trimarr: { enabled: false, channels: new Set(), last_run_at: null },
    setup: null, apikey: null, netTests: [],
    network: {
      mode: 'direct', selected: null, order: ['direct', 'p_demo01'], fail_threshold: 3, cooldown_minutes: 360, rotate_hours: 6, check_preferred: true,
      never_direct: false, never_direct_auto: true,
      proxies: [{ id: 'p_demo01', name: 'Backup line', masked: 'socks5h://***@proxy.example:1080', scheme: 'socks5h' }],
      status: { active: 'direct', active_name: 'Direct', ip: '203.0.113.5', latency_ms: 45, since: NOW0 - 2 * DAY, reason: null, last_switch: null, next_rotate_at: null, preferred_retry_at: null, updated_at: null, stale: false, paused: false },
    },
    audit: [
      { at: NOW0 - 3 * MIN, who: 'admin', action: 'auth.login', detail: 'From this browser' },
      { at: NOW0 - 2 * HOUR, who: 'system', action: 'auth.login_failed', detail: 'Wrong password for “admin”' },
      { at: NOW0 - 26 * HOUR, who: 'admin', action: 'network.proxy_added', detail: 'Backup line' },
      { at: NOW0 - 2 * DAY, who: 'admin', action: 'auth.setup', detail: 'Admin account created' },
    ],
    feed: { last: NOW0 - 2 * MIN - 10e3 },
  };
  const target = () => S.settings.downloads.fill_target_bytes;

  /* ---------- Events (the mock's stand-in for GET /api/events) ---------- */
  const subs = new Set();
  let evSeq = 1;
  function emit(event, data) { if (!subs.size) return; const msg = { id: evSeq++, event, data: clone(data) }; subs.forEach((fn) => { try { fn(msg); } catch (e) { console.error(e); } }); }

  /* ---------- Rules ---------- */
  function effective(ch) {
    const s = ch.settings, d = S.settings.downloads;
    return {
      retention: clone(s.retention || { mode: 'fill' }),
      max_duration_minutes: s.max_duration_minutes ?? d.max_duration_minutes,
      quality: s.quality ?? d.quality,
      include_live: s.include_live ?? d.include_live,
      sponsorblock: (s.sponsorblock ?? d.sponsorblock).slice(),
    };
  }
  const passes = (v, eff) => v.kind !== 'short' && v.state !== 'upcoming' && (v.kind !== 'live' || eff.include_live) && (!eff.max_duration_minutes || v.duration_seconds <= eff.max_duration_minutes * 60);
  /* Returns inside(v) for eligible videos, called newest-first. fill uses the global frontier. */
  function insideTest(ret, ch, frontier) {
    const now = Date.now();
    switch (ret.mode) {
      case 'count': { let n = 0; return () => n++ < ret.count; }
      case 'days': { const from = now - ret.days * DAY; return (v) => v.published >= from; }
      case 'since': { const from = T.parseDay(ret.since).getTime(); return (v) => v.published >= from; }
      case 'new_only': return (v) => !v.backfill;
      case 'forever': return () => true;
      default: return (v) => !v.backfill || (frontier != null && v.published >= frontier && !(v.state === 'removed' && REMOVE_BY_ROLL.has(v.removed_reason)));
    }
  }
  const bpsFor = (ch, quality) => (quality === '720p' ? 1.3e5 : 2.45e5) * ch.rate;
  const estBytes = (ch, v, quality) => Math.round((v.duration_seconds - (v.cutS || 0)) * bpsFor(ch, quality) * v.jit);
  const inOvernight = (t) => {
    const p = S.settings.downloads.pacing, d = new Date(t || Date.now());
    const m = d.getHours() * 60 + d.getMinutes();
    const [a, b] = [p.overnight_start, p.overnight_end].map((x) => { const [h, mm] = x.split(':').map(Number); return h * 60 + mm; });
    return a <= b ? m >= a && m < b : m >= a || m < b;
  };
  function nextNightStart(t) { const d = new Date(t); d.setHours(1, 0, 0, 0); if (d.getTime() <= t) d.setDate(d.getDate() + 1); return d.getTime(); }

  /* ---------- Building the library ---------- */
  function makeChannel(row, order) {
    const [title, handle, subscribers, genre] = row;
    const r = T.rng('ch:' + handle);
    const G = GENRES[genre] || GENRES.general;
    const sp = SPECIAL[handle] || {}, ov = OVERRIDES[handle] || {};
    const pop = subscribers > 3e6 ? 1.25 : subscribers < 1e5 ? 0.6 : 1;
    const settings = Object.assign({ retention: null, max_duration_minutes: null, quality: null, include_live: null, sponsorblock: null }, clone(sp.settings || {}), clone(ov.settings || {}));
    const ch = {
      id: 'UC' + rid(r, 22), handle, genre, order, subscribers,
      yt_title: title, title_override: null, summary_override: null, genre_override: null, poster_choice: null, poster_upload: null,
      url: 'https://www.youtube.com/' + handle, settings,
      perWeek: sp.perWeek || ov.perWeek || between(r, G.perWeek[0], G.perWeek[1]) * pop,
      youtube_video_count: sp.total || ov.total || Math.round(between(r, 60, 700) * pop),
      about: sp.about || G.about, rate: (sp.rate || 1) * (G.rate || 1),
      added_at: ov.addedAgo ? NOW0 - ov.addedAgo : START + r() * 50 * MIN,
      last_checked_at: NOW0 - r() * 55 * MIN,
      status: 'monitored', status_detail: null, removal: null, sync: null, subscribed: true, kept_while_unsubscribed: false,
      art_poster: (ART[handle] && ART[handle].poster) || null, banner_url: (ART[handle] && ART[handle].banner) || null,
      topic: TOPIC_OVERRIDE[handle] || GENRE_TOPIC[genre] || 'other', topic_source: 'auto', playlists: [],
      catalog: [],
    };
    if (ov.sync) { ch.status = 'syncing'; ch.sync = { done: ov.sync.done, total: ov.sync.total, started: NOW0, ends: NOW0 + ov.sync.ms, refresh: !!ov.sync.refresh }; }
    return ch;
  }
  const chTitle = (ch) => ch.title_override || ch.yt_title;
  function posterUrl(ch) {
    if (ch.poster_choice === 'upload') return ch.poster_upload;
    if (ch.poster_choice) return posterVariant(ch, ch.poster_choice).url;
    return ch.art_poster;
  }
  function newEntry(ch, id, title, published, dur, kind) {
    const G = GENRES[ch.genre] || GENRES.general;
    const h = T.rng('e:' + id);
    const sbHit = h() < G.sb;
    return { id, channel_id: ch.id, yt_title: title, title_override: null, summary_override: null, thumb_choice: null, thumb_upload: null,
      published, duration_seconds: dur, kind, state: null, stage: null, watched: false, watched_at: null, keep: false,
      cutS: sbHit ? Math.min(Math.round(between(h, 18, 150)), Math.round(dur * 0.2)) : 0, jit: between(h, 0.72, 1.3),
      sponsorblock_cut_seconds: null, size_bytes: null, est: null, error: null, removed_reason: null, removed_at: null,
      downloaded_at: null, added_at: null, failed_at: null, backfill: published < ch.added_at, reason: null, resolution: null, wait_until: null, episode: null };
  }
  /* Every upload the channel has (capped), newest first. state null = not listed (outside every window). */
  function genCatalog(ch, opts) {
    const r = T.rng('v:' + ch.handle + (opts.salt || ''));
    const G = GENRES[ch.genre] || GENRES.general;
    const special = ((SPECIAL[ch.handle] || {}).titles || []).slice();
    const gap = (7 * DAY) / ch.perWeek;
    let t = opts.newest != null ? opts.newest : NOW0 - r() * gap * 0.9;
    const out = [];
    const n = Math.min(ch.youtube_video_count, CATALOG_MAX);
    for (let i = 0; i < n; i++) {
      const roll = r();
      let kind = roll < G.shorts ? 'short' : roll < G.shorts + G.live ? 'live' : 'video';
      if (i === 0 && (special.length || opts.newest != null)) kind = 'video';
      if (kind === 'live' && !G.l.length) kind = 'video';
      const title = kind === 'video' && special.length ? special.shift() : fill(pick(r, kind === 'short' ? G.s : kind === 'live' ? G.l : G.t), r);
      const dur = kind === 'short' ? Math.round(between(r, 14, 59)) : kind === 'live' ? Math.round(between(r, 1.4, 4.5) * 3600) : Math.round(between(r, G.dur[0], G.dur[1]) * 60 * (r() < 0.1 ? 2.1 : 1));
      out.push(newEntry(ch, rid(r, 11), title, t, dur, kind));
      let prev = t - gap * (0.35 + r() * 1.3);
      const d = new Date(prev); d.setHours(9 + Math.floor(r() * 11), Math.floor(r() * 60), Math.floor(r() * 60), 0);
      t = Math.min(d.getTime(), t - HOUR);
    }
    return out;
  }
  function skipState(v, eff) {
    if (v.kind === 'short') return 'skipped_short';
    if (v.kind === 'live' && !eff.include_live) return 'skipped_live';
    if (eff.max_duration_minutes && v.duration_seconds > eff.max_duration_minutes * 60) return 'skipped_too_long';
    return null;
  }
  /* Shorts / streams / over-long uploads inside the listed range show as skipped; nothing older is listed. */
  function listSkipped(ch) {
    const eff = effective(ch);
    let oldest = -1;
    ch.catalog.forEach((v, i) => { if (LISTED(v) && v.state.indexOf('skipped') !== 0 && v.state !== 'upcoming') oldest = i; });
    ch.catalog.forEach((v, i) => {
      if (v.state && v.state.indexOf('skipped') === 0 && i > oldest) v.state = null;
      if (!LISTED(v) && i <= oldest) { const s = skipState(v, eff); if (s) { v.state = s; v.added_at = v.added_at || (v.backfill ? ch.added_at + 2 * MIN : v.published + 6 * MIN); } }
    });
  }
  function landed(v, ch, at) {
    const eff = effective(ch);
    v.state = 'downloaded'; v.downloaded_at = at; v.resolution = eff.quality;
    v.sponsorblock_cut_seconds = eff.sponsorblock.length ? v.cutS : 0;
    v.size_bytes = estBytes(ch, v, eff.quality);
  }
  const toQueued = (v, reason) => { v.est = v.est || v.size_bytes; v.size_bytes = null; v.state = 'queued'; v.reason = reason; v.watched = false; v.sponsorblock_cut_seconds = null; v.downloaded_at = null; };
  function episodeCodes(ch) {
    const byDay = {};
    ch.catalog.filter((v) => v.kind !== 'short').forEach((v) => { const k = dayOf(v.published); (byDay[k] = byDay[k] || []).push(v); });
    Object.entries(byDay).forEach(([k, list]) => list.sort((a, b) => a.published - b.published).forEach((v, i) => { const [y, m, d] = k.split('-'); v.episode = 'S' + y + 'E' + m + d + pad(Math.min(99, i + 1)); }));
  }
  function indexChannel(ch) { S.byId.set(ch.id, ch); S.byHandle.set(ch.handle.toLowerCase(), ch); ch.catalog.forEach((v) => S.videos.set(v.id, v)); }

  function build() {
    S.channels = (FRESH ? [] : SEED).map(makeChannel);
    const justUploaded = { '@SlowTrainJournal': 3 * HOUR + 12 * MIN, '@PocketGadgetDaily': 38 * MIN, '@RustToRoad': 74 * MIN, '@BridgeMath': 2 * HOUR + 5 * MIN, '@BenchmarkBarn': 6 * HOUR, '@SimpleMachinesClub': 64 * MIN, '@StarlightAlmanac': 2 * HOUR + 40 * MIN };
    const active = [];
    S.channels.forEach((ch) => {
      ch.catalog = genCatalog(ch, { newest: justUploaded[ch.handle] != null ? NOW0 - justUploaded[ch.handle] : null });
      if (!(ch.sync && !ch.sync.refresh)) active.push(ch);
    });
    S.channels.forEach(indexChannel);
    // 1) what each channel wants: new uploads always; windows for overrides; fill/forever backlog decided by the frontier
    const pool = [];
    let fixed = 0;
    active.forEach((ch) => {
      const eff = effective(ch), mode = eff.retention.mode;
      const inW = insideTest(eff.retention, ch, null);
      let pushedOut = mode === 'count' || mode === 'days' ? Math.min(6, Math.round((NOW0 - ch.added_at) / ((7 * DAY) / ch.perWeek))) : 0;
      ch.catalog.forEach((v) => {
        if (!passes(v, eff)) return;
        let want;
        if (mode === 'fill' || mode === 'forever') want = v.backfill ? null : true;
        else { want = inW(v); if (!want && pushedOut > 0 && v.backfill) { v.pushed = true; pushedOut--; } }
        v.want = want;
        if (want) fixed += estBytes(ch, v, eff.quality);
        else if (want === null) pool.push([v, ch, estBytes(ch, v, eff.quality)]);
      });
    });
    // 2) the frontier: the backlog walks back in time across all channels until the budget is used
    const budget = FULL ? target() + 40 * GB : 1.84 * TIB;
    pool.sort((a, b) => b[0].published - a[0].published);
    let acc = fixed, i = 0;
    for (; i < pool.length; i++) { if (acc + pool[i][2] > budget) break; pool[i][0].want = true; acc += pool[i][2]; S.frontier = pool[i][0].published; }
    const nextUp = FULL ? [] : pool.slice(i, i + 14).map((x) => x[0]);
    // 3) states
    active.forEach((ch) => {
      ch.catalog.forEach((v) => {
        if (v.want === true) landed(v, ch, null);
        else if (v.pushed) { landed(v, ch, null); v.state = 'removed'; v.removed_reason = 'retention'; }
        delete v.want; delete v.pushed;
      });
    });
    nextUp.forEach((v) => { toQueued(v, 'backfill'); v.est = estBytes(chOf(v), v, effective(chOf(v)).quality); v.added_at = NOW0 - 3 * HOUR; });
    // 4) when things landed: new uploads 15-75 min after publishing (the newest few are still queued), backlog night by night
    active.forEach((ch) => ch.catalog.forEach((v, idx) => {
      if (v.state !== 'downloaded' || v.backfill) return;
      const at = v.published + (6 + T.rng(v.id)() * 14) * MIN;
      if (at > NOW0 - 2 * MIN || (idx === 0 && justUploaded[ch.handle] != null)) { toQueued(v, 'new_upload'); v.est = estBytes(ch, v, effective(ch).quality); v.added_at = v.published + 5 * MIN; return; }
      v.downloaded_at = at; v.added_at = v.published + (4 + (T.hash(v.id) % 30)) * MIN;
    }));
    const backlog = [];
    active.forEach((ch) => ch.catalog.forEach((v) => { if ((v.state === 'downloaded' || v.state === 'removed') && v.backfill) backlog.push(v); }));
    backlog.sort((a, b) => b.published - a.published);
    const endAt = FULL ? FULL_AT : NOW0;
    const nights = [];
    for (let n = nextNightStart(START); n < endAt; n += DAY) nights.push(n);
    const perNight = Math.max(1, backlog.length / Math.max(1, nights.length));
    backlog.forEach((v, k) => {
      const ch = chOf(v);
      const n = Math.min(nights.length - 1, Math.floor(k / perNight));
      const base = nights[Math.max(0, n)] || START + HOUR;
      const nightStart = Math.max(base, nextNightStart(ch.added_at));
      v.downloaded_at = Math.min(endAt - MIN, nightStart + (k - n * perNight) * 62e3);
      v.added_at = v.downloaded_at - between(T.rng('a:' + v.id), 1, 18) * HOUR;
    });
    backlog.filter((v) => v.state === 'removed').forEach((v) => { v.removed_at = Math.min(NOW0 - HOUR, v.downloaded_at + between(T.rng('rm:' + v.id), 2, 12) * DAY); });
    // 5) watching
    S.videos.clear();
    S.channels.forEach(indexChannel);
    S.videos.forEach((v) => {
      if (!v.downloaded_at) return;
      const rr = T.rng('w:' + v.id);
      const age = (NOW0 - v.downloaded_at) / DAY;
      if (rr() < 0.002 + Math.min(0.014, age * 0.0007)) { v.watched = true; v.watched_at = v.downloaded_at + rr() * Math.max(HOUR, (v.removed_at || NOW0) - v.downloaded_at); }
    });
    // 6) (full scenario) since the library hit the target, every new arrival rolled one video out
    if (FULL) {
      const arrivals = [];
      S.videos.forEach((v) => { if (v.state === 'downloaded' && !v.backfill && v.downloaded_at > FULL_AT) arrivals.push(v); });
      arrivals.sort((a, b) => a.downloaded_at - b.downloaded_at);
      const order = removalOrder(arrivals.length);
      arrivals.forEach((a, k) => { const x = order[k]; if (x) { x.v.state = 'removed'; x.v.removed_reason = x.reason; x.v.removed_at = a.downloaded_at; } });
      bump();
    }
    S.channels.forEach((ch) => { listSkipped(ch); episodeCodes(ch); });
    S.channels.forEach(indexChannel);
    if (!FRESH) scenarioStates();
    const queued = [];
    S.videos.forEach((v) => { if (v.state === 'queued') queued.push(v); });
    const rank = { new_upload: 0, retry: 1, manual: 2, backfill: 3 };
    queued.sort((a, b) => rank[a.reason] - rank[b.reason] || b.published - a.published);
    S.queue = queued.map((v) => ({ vid: v.id, reason: v.reason, added_at: v.queued_at || v.added_at || NOW0 }));
    buildHistory();
    S.setup = { done: !FRESH, configured: !FRESH, token_set: !FRESH, library: FRESH ? null : 'YouTube', plex_root: FRESH ? null : '/data/youtube', pins: {} };
    S.plex = { state: 'connected', url: 'http://plex.local:32400', server_name: 'Home Server', version: '1.43.3.10896', checked_at: NOW0 - 40e3, message: null,
      library: { state: 'ready', name: 'YouTube', section_id: 14, path: '/data/youtube', agent: 'Plex Personal Media', scanner: 'Plex TV Series', last_scan_at: NOW0 - 4 * MIN, shared_with_friends: [], home_users_with_access: 3 } };
    if (SCENARIO === 'trouble') troubleScenario();
    S.ver++;
  }

  function scenarioStates() {
    const H = (h) => S.byHandle.get(h.toLowerCase());
    const mp = H('@SlowTrainJournal');
    const job = mp && mp.catalog.find((v) => v.state === 'queued');
    if (job) {
      job.state = 'downloading'; job.reason = 'new_upload';
      const total = Math.round(job.duration_seconds * 2.45e5 * 1.6);
      S.current = { vid: job.id, stage: 'download', stageAt: NOW0, bytes: Math.round(total * 0.42), total, speed: 6.08e6, started: NOW0 - 50e3, stages: ['download', 'sponsorblock', 'artwork', 'plex'], cut: 71 };
    }
    ['@SimpleMachinesClub', '@StarlightAlmanac'].forEach((h) => {
      const ch = H(h); const v = ch && ch.catalog.find((x) => x.state === 'queued');
      if (v) { v.state = 'waiting_sponsorblock'; v.wait_until = v.published + S.settings.downloads.sponsorblock_wait_hours * HOUR; }
    });
    const fail = (h, idx, error) => {
      const ch = H(h); if (!ch) return;
      const v = ch.catalog.filter((x) => x.state === 'downloaded' && !x.backfill)[idx] || ch.catalog.filter((x) => x.state === 'downloaded')[idx];
      if (!v) return;
      v.state = 'failed'; v.size_bytes = null; v.downloaded_at = null; v.watched = false; v.sponsorblock_cut_seconds = null;
      v.failed_at = Math.min(NOW0 - 10 * MIN, v.published + (70 + T.rng(v.id)() * 90) * MIN);
      v.error = Object.assign({ attempts: 3, last_attempt_at: iso(v.failed_at), next_retry_at: null, retryable: true }, error);
    };
    fail('@PocketGadgetDaily', 1, { code: 'members_only', message: 'This video is for channel members only.', attempts: 1, retryable: false });
    fail('@BenchmarkBarn', 0, { code: 'http_403', message: 'YouTube refused the download (HTTP 403) three times.' });
    fail('@RustToRoad', 1, { code: 'network', message: 'The connection dropped four times while downloading.' });
    const hs = H('@GrainAndGlue');
    const rv = hs && hs.catalog.find((x) => x.state === 'downloaded' && !x.backfill);
    if (rv) { toQueued(rv, 'retry'); rv.queued_at = NOW0 - 25 * MIN; }
    const hist = H('@LedgerOfEmpires');
    const longOnes = hist ? hist.catalog.filter((x) => x.state === 'downloaded' && !x.backfill).slice(0, 2) : [];
    if (longOnes[0]) Object.assign(longOnes[0], { yt_title: 'The Salt Road: The Complete Series', duration_seconds: 3 * 3600 + 12 * 60, state: 'skipped_too_long', size_bytes: null, downloaded_at: null, sponsorblock_cut_seconds: null, watched: false });
    if (longOnes[1]) { Object.assign(longOnes[1], { yt_title: 'Rivers of Iron: Feature-Length Cut', duration_seconds: 2 * 3600 + 26 * 60 }); toQueued(longOnes[1], 'manual'); longOnes[1].est = Math.round(longOnes[1].duration_seconds * 2.45e5); longOnes[1].queued_at = NOW0 - 9 * MIN; }
    const mn = H('@BackroadsAtlas');
    if (mn) {
      const v = newEntry(mn, rid(T.rng('premiere'), 11), 'Premiere: Across the Salt Flats by Bus', NOW0 + 5 * HOUR + 20 * MIN, 1520, 'video');
      v.state = 'upcoming'; v.added_at = NOW0 - 50 * MIN; v.backfill = false;
      mn.catalog.unshift(v); S.videos.set(v.id, v);
    }
    ['@ScrapyardRobotics', '@KitchenChemistryHour', '@SimpleMachinesClub'].forEach((h, i) => { const ch = H(h); const v = ch && ch.catalog.filter((x) => x.state === 'downloaded')[4 + i]; if (v) v.keep = true; });
    const ve = H('@FieldNotesScience');
    const ev = ve && ve.catalog.find((x) => x.state === 'downloaded' && /Spinning Tops/.test(x.yt_title));
    if (ev) ev.title_override = 'Spinning Tops: The Bizarre Physics (Director\u2019s Cut)';
    const sc = H('@PatchNotesWeekly');
    if (sc) { sc.status = 'pending_removal'; sc.subscribed = false; sc.removal = { reason: 'unsubscribed', requested_at: NOW0 - 18 * HOUR, delete_at: NOW0 + 2 * DAY + 6 * HOUR }; dropQueued(sc); }
    const ob = H('@TinyIslandHops');
    if (ob) { ob.status = 'pending_removal'; ob.removal = { reason: 'manual', requested_at: NOW0 - 2 * DAY - 19 * HOUR, delete_at: NOW0 + 5 * HOUR }; dropQueued(ob); }
    // Trimarr (optional add-on): off, but a few videos were trimmed by hand and one channel is ticked
    ['@QuietPCCorner', '@PocketGadgetDaily', '@SolderAndSawdust', '@KitchenChemistryHour', '@FieldNotesScience', '@BenchmarkBarn'].forEach((h, i) => {
      const ch = H(h); const v = ch && ch.catalog.find((x) => x.state === 'downloaded' && !x.sponsorblock_cut_seconds && x.backfill);
      if (v) v.trim = { state: 'trimmed', removed_seconds: 38 + ((i * 29) % 90) };
    });
    const gnc = H('@BenchmarkBarn'); if (gnc) S.trimarr.channels.add(gnc.id);
    // gone from YouTube after Tubarr kept them: never rolled out
    [['@PocketGadgetDaily', 'deleted', 3], ['@RustToRoad', 'private', 5], ['@LedgerOfEmpires', 'unavailable', 8], ['@QuietPCCorner', 'deleted', 12],
     ['@LostLettersHistory', 'private', 2], ['@KitchenChemistryHour', 'deleted', 16], ['@ScrapyardRobotics', 'unavailable', 21]].forEach(([h, reason, days], i) => {
      const ch = H(h); const v = ch && ch.catalog.filter((x) => x.state === 'downloaded')[6 + i];
      if (v) {
        v.gone = { at: NOW0 - days * DAY - i * 3 * HOUR, reason };
        // it can only vanish after Tubarr had it
        if (v.downloaded_at && v.downloaded_at > v.gone.at - HOUR) v.gone.at = Math.min(NOW0 - HOUR, v.downloaded_at + (6 + i * 5) * HOUR);
      }
    });
    // playlists detected as series (their parts get numbered titles), and the ones that were ignored
    Object.entries(SERIES).forEach(([h, def]) => {
      const ch = H(h); if (!ch) return;
      const r = T.rng('pl:' + h);
      const pool = ch.catalog.filter((v) => LISTED(v) && v.kind !== 'short' && v.state !== 'upcoming' && v.state.indexOf('skipped') !== 0);
      let at = 0;
      def.series.forEach(([title, n, order]) => {
        const members = pool.slice(at, at + n).reverse(); at += n;
        const pl = { id: 'PL' + rid(r, 16), title, enabled: true, order, members: members.map((v) => v.id), ignored: false, reason: null, count: n };
        members.forEach((v, i) => { v.series = pl; v.part = i + 1; v.yt_title = title + ' | Part ' + (i + 1) + ': ' + PART_SUBTITLES[(i + T.hash(title)) % PART_SUBTITLES.length]; });
        ch.playlists.push(pl);
      });
      def.ignored.forEach(([title, n, reason]) => ch.playlists.push({ id: 'PL' + rid(r, 16), title, enabled: false, order: 'playlist', members: [], ignored: true, reason, count: n }));
    });
    const pr = H('@MidnightMuffler');
    if (pr) {
      pr.status = 'gone'; pr.gone = { at: NOW0 - 6 * DAY - 4 * HOUR, reason: 'terminated' };
      // everything it had in Plex landed before it was terminated
      const had = pr.catalog.filter((v) => v.downloaded_at).sort((a, b) => b.published - a.published);
      had.forEach((v, i) => { if (v.downloaded_at > pr.gone.at - HOUR) v.downloaded_at = pr.gone.at - (2 + i * 0.8) * DAY; });
      pr.catalog.forEach((v) => { if (LISTED(v)) v.gone = { at: pr.gone.at, reason: 'terminated' }; });
      dropQueued(pr);
    }
    S.trimarr.last_run_at = NOW0 - 2 * DAY - 5 * HOUR;
    const rh = H('@OddJobsAlmanac');
    if (rh) { rh.status = 'error'; rh.status_detail = 'YouTube rate-limited the channel page (HTTP 429). Tubarr will try again at ' + T.fmtTime(iso(NOW0 + 38 * MIN)).replace('\u00a0', ' ') + '.'; }
  }
  function dropQueued(ch) {
    ch.catalog.forEach((v) => { if (v.state === 'queued' || v.state === 'waiting_sponsorblock') { v.state = 'removed'; v.removed_reason = 'manual'; v.removed_at = ch.removal ? ch.removal.requested_at : Date.now(); } });
    S.queue = S.queue.filter((q) => { const v = S.videos.get(q.vid); return v && v.state === 'queued'; });
  }
  function troubleScenario() {
    Object.assign(S.network, { mode: 'failover', never_direct: true, never_direct_auto: false });
    Object.assign(S.network.status, { active: 'p_demo01', active_name: 'Backup line', ip: null, latency_ms: null, paused: true, reason: 'Backup line failed 3 connection checks in a row', since: NOW0 - 25 * MIN,
      last_switch: { at: iso(NOW0 - 25 * MIN), from_name: 'Direct', to_name: 'Backup line', reason: '3 connection failures in a row' } });
    S.paused = true;
    if (S.current) { const v = S.videos.get(S.current.vid); v.state = 'queued'; S.queue.unshift({ vid: v.id, reason: 'new_upload', added_at: NOW0 - 3 * HOUR }); S.current = null; }
    const pg = S.byHandle.get('@TidepoolPhysics');
    if (pg) { pg.status = 'error'; pg.status_detail = 'Channel page not found: it may have been renamed. Check the handle and add it again if it moved.'; }
    [['@QuietPCCorner', 'network', 'The connection dropped four times while downloading. Your internet may have been down.'], ['@SolderAndSawdust', 'unavailable', 'This video was removed by the uploader.'], ['@RouterRescue', 'age_restricted', 'Age-restricted: YouTube needs a signed-in session to download it.']].forEach(([h, code, message]) => {
      const ch = S.byHandle.get(h.toLowerCase()); const v = ch && ch.catalog.find((x) => x.state === 'downloaded');
      if (!v) return;
      v.state = 'failed'; v.size_bytes = null; v.failed_at = NOW0 - T.rng(v.id)() * 20 * HOUR;
      v.error = { code, message, attempts: 3, last_attempt_at: iso(v.failed_at), next_retry_at: null, retryable: code !== 'unavailable' };
    });
    S.plex.library.shared_with_friends = ['a friend'];
    buildHistory();
  }

  /* ---------- Derived data (memoised per state version) ---------- */
  const chOf = (v) => S.byId.get(v.channel_id);
  const vTitle = (v) => v.title_override || v.yt_title;
  const sizeOf = (v) => v.size_bytes || 0;
  const memo = new Map();
  function once(key, fn) { const m = memo.get(key); if (m && m.ver === S.ver) return m.val; const val = fn(); memo.set(key, { ver: S.ver, val }); return val; }
  const bump = () => { S.ver++; };
  function usedBytes() { return once('used', () => { let s = 0; S.videos.forEach((v) => { if (v.state === 'downloaded') s += sizeOf(v); }); return s; }); }
  /* Which downloaded videos the roll may never remove, and why. */
  function protections() {
    return once('prot', () => {
      const prot = new Map(), n = S.settings.downloads.protect_newest;
      S.channels.forEach((ch) => {
        const forever = effective(ch).retention.mode === 'forever';
        const down = ch.catalog.filter((v) => v.state === 'downloaded');
        if (ch.status === 'gone') { down.forEach((v) => prot.set(v.id, 'gone')); return; }
        down.forEach((v) => { if (v.gone) prot.set(v.id, 'gone'); });
        if (forever) { down.forEach((v) => { if (!prot.has(v.id)) prot.set(v.id, v.keep ? 'keep' : 'channel'); }); return; }
        down.sort((a, b) => b.published - a.published).forEach((v, i) => { if (prot.has(v.id)) return; if (v.keep) prot.set(v.id, 'keep'); else if (i < n) prot.set(v.id, 'newest'); });
      });
      return prot;
    });
  }
  /* The roll's order: watched (oldest watched first), then the oldest backlog video of the biggest channel. */
  function removalOrder(limit) {
    return once('order' + limit, () => {
      const prot = protections(), sizes = new Map(), byCh = new Map(), watched = [];
      S.videos.forEach((v) => {
        if (v.state !== 'downloaded') return;
        sizes.set(v.channel_id, (sizes.get(v.channel_id) || 0) + sizeOf(v));
        if (prot.has(v.id)) return;
        if (v.watched) watched.push(v);
        else if (v.backfill) { if (!byCh.has(v.channel_id)) byCh.set(v.channel_id, []); byCh.get(v.channel_id).push(v); }
      });
      watched.sort((a, b) => a.watched_at - b.watched_at);
      const out = watched.slice(0, limit).map((v) => ({ v, reason: 'watched' }));
      byCh.forEach((list) => list.sort((a, b) => a.published - b.published));
      while (out.length < limit) {
        let best = null, bestSize = -1;
        byCh.forEach((list, id) => { if (list.length && sizes.get(id) > bestSize) { best = id; bestSize = sizes.get(id); } });
        if (!best) break;
        const v = byCh.get(best).shift();
        out.push({ v, reason: 'making_room' });
        sizes.set(best, sizes.get(best) - sizeOf(v));
      }
      return out;
    });
  }
  function rollOne(at, quiet) {
    const next = removalOrder(1)[0];
    if (!next) return null;
    const v = next.v;
    v.state = 'removed'; v.removed_reason = next.reason; v.removed_at = at;
    bump();
    if (!quiet) { pushHistory('removed', v, { detail: next.reason }); touchV(v); touchCh(chOf(v)); }
    return v;
  }
  function reachDate() {
    return once('reach', () => {
      // median of each fill channel's oldest backlog video in Plex: "roughly everything since then is in"
      const dates = [];
      S.channels.forEach((ch) => {
        if (effective(ch).retention.mode !== 'fill') return;
        let oldest = null;
        ch.catalog.forEach((v) => { if (v.state === 'downloaded' && v.backfill && (oldest == null || v.published < oldest)) oldest = v.published; });
        if (oldest != null) dates.push(oldest);
      });
      if (!dates.length) return S.frontier;
      dates.sort((a, b) => a - b);
      return dates[Math.floor(dates.length / 2)];
    });
  }
  function fillOut() {
    const used = usedBytes(), tgt = target();
    if (used >= tgt) S.rolling = true;
    else if (used < tgt - 60 * GB) S.rolling = false;
    const rolling = S.rolling;
    let last = null;
    S.videos.forEach((v) => { if (v.state === 'removed' && REMOVE_BY_ROLL.has(v.removed_reason) && (!last || v.removed_at > last.removed_at)) last = v; });
    const nx = removalOrder(1)[0];
    const rd = reachDate();
    return { state: rolling ? 'rolling' : 'filling', target_bytes: tgt, reach_date: rd ? dayOf(rd) : null,
      last_removed: last ? { video: videoOut(last), reason: last.removed_reason, at: iso(last.removed_at) } : null,
      next_removal: nx ? { video: videoOut(nx.v), reason: nx.reason } : null };
  }
  const storageOut = () => { const u = usedBytes(); return { used_bytes: u, free_bytes: CAP - u, cap_bytes: CAP, target_bytes: target(), fill: fillOut() }; };

  function waitsFor(item) {
    if (S.paused) return 'paused';
    const d = S.settings.downloads.pacing.daytime;
    if (inOvernight()) return null;
    if (d === 'none') return 'overnight';
    if (d === 'new_only' && item.reason === 'backfill') return 'overnight';
    return null;
  }
  const runOrder = () => S.queue.filter((q) => !waitsFor(q)).concat(S.queue.filter((q) => waitsFor(q)));
  function stageProgress(c) {
    if (c.stage === 'download') return Math.min(1, c.bytes / c.total);
    const len = { sponsorblock: 5e3, artwork: 3.5e3, plex: 2.5e3 }[c.stage];
    return Math.min(1, (Date.now() - c.stageAt) / len);
  }
  function activityAt(v) {
    let t;
    if (v.state === 'removed') t = v.removed_at;
    else if (v.state === 'failed') t = v.failed_at;
    else if (v.state === 'downloaded') t = v.downloaded_at || v.added_at;
    else t = v.added_at || v.published;
    // disappearing from YouTube is a milestone of its own
    if (v.gone && v.gone.at > (t || 0)) t = v.gone.at;
    return t;
  }
  function videoOut(v) {
    const ch = chOf(v), cur = S.current && S.current.vid === v.id ? S.current : null;
    const estimate = v.est || (ch ? estBytes(ch, v, effective(ch).quality) : null);
    const prot = v.state === 'downloaded' ? protections().get(v.id) || null : null;
    return {
      id: v.id, channel_id: v.channel_id, channel_title: ch ? chTitle(ch) : '', title: vTitle(v),
      upload_date: dayOf(v.published), published_at: iso(v.published), added_at: iso(v.added_at), activity_at: iso(activityAt(v)), backfill: !!v.backfill,
      episode: v.kind === 'short' ? null : v.episode, duration_seconds: v.duration_seconds, state: v.state,
      stage: cur ? cur.stage : null, progress: cur ? stageProgress(cur) : null, wait_until: iso(v.wait_until),
      size_bytes: v.state === 'downloaded' ? sizeOf(v) : ['queued', 'waiting_sponsorblock'].includes(v.state) ? estimate : cur ? cur.total : null,
      resolution: v.state === 'downloaded' ? v.resolution || '1080p' : null,
      sponsorblock_cut_seconds: v.state === 'downloaded' ? v.sponsorblock_cut_seconds : null,
      trim: v.state === 'downloaded' && v.trim ? { state: v.trim.state, removed_seconds: v.trim.removed_seconds } : null,
      watched: !!v.watched, keep: !!v.keep, protected: prot, youtube_gone: v.gone ? { at: iso(v.gone.at), reason: v.gone.reason } : null,
      series: v.series && v.series.enabled ? { id: v.series.id, title: v.series.title, part: v.part } : null, edited: !!(v.title_override || v.summary_override || v.thumb_choice),
      error: v.error || null, removed_reason: v.removed_reason || null, removed_at: iso(v.removed_at),
      thumbnail_url: thumbUrl(v), youtube_url: 'https://www.youtube.com/watch?v=' + v.id,
      plex_url: v.state === 'downloaded' ? 'http://plex.local:32400/web/index.html' : null,
    };
  }
  function thumbUrl(v) { if (v.thumb_choice === 'upload') return v.thumb_upload; if (v.thumb_choice) return thumbVariant(v, v.thumb_choice).url; return null; }
  function describe(v) {
    const ch = chOf(v), G = GENRES[ch ? ch.genre : 'general'] || GENRES.general, r = T.rng('d:' + v.id);
    const lines = [v.yt_title + '.', '', pick(r, ['In this one we get to the bottom of it, with a lot of testing along the way.', "We've wanted to make this video for a long time. Here's how it went.", 'Thanks to everyone who suggested this in the comments.', 'Full breakdown below, with sources and timestamps.']), ''];
    if (v.kind !== 'short') lines.push('Chapters', '0:00 Intro', '1:12 ' + pick(r, ['The setup', 'Background', 'The problem', 'First test']), T.fmtClock(Math.round(v.duration_seconds * 0.45)) + ' ' + pick(r, ['What actually happened', 'Results', 'The twist', 'Round two']), T.fmtClock(Math.round(v.duration_seconds * 0.9)) + ' Wrap-up');
    lines.push('', G.about);
    return lines.join('\n');
  }
  const videoDetail = (v) => Object.assign(videoOut(v), { youtube_title: v.yt_title, description: describe(v), summary: v.summary_override || describe(v),
    locked_fields: [v.title_override ? 'title' : null, v.summary_override ? 'summary' : null, v.thumb_choice ? 'thumbnail' : null].filter(Boolean) });

  function chStats(ch) {
    const prot = protections();
    let count = 0, size = 0, queued = 0, failed = 0, lastDl = null, lastUp = null, watched = 0, skipped = 0, sb = 0, kept = 0, protectedN = 0, oldest = null, newest = null;
    ch.catalog.forEach((v) => {
      if (!LISTED(v) || v.state === 'upcoming') return;
      if (lastUp == null || v.published > lastUp) lastUp = v.published;
      if (v.state === 'downloaded') {
        count++; size += sizeOf(v); sb += v.sponsorblock_cut_seconds || 0; if (v.watched) watched++; if (v.keep) kept++; if (prot.has(v.id)) protectedN++;
        if (v.downloaded_at && (lastDl == null || v.downloaded_at > lastDl)) lastDl = v.downloaded_at;
        const d = dayOf(v.published); if (!oldest || d < oldest) oldest = d; if (!newest || d > newest) newest = d;
      }
      if (v.state === 'queued' || v.state === 'downloading' || v.state === 'waiting_sponsorblock') queued++;
      if (v.state === 'failed') failed++;
      if (v.state.indexOf('skipped') === 0) skipped++;
    });
    return { count, size, queued, failed, lastDl, lastUp, watched, skipped, sb, kept, protectedN, oldest, newest };
  }
  function channelSummary(ch) {
    const st = chStats(ch), mode = effective(ch).retention.mode;
    return {
      id: ch.id, title: chTitle(ch), handle: ch.handle, url: ch.url, subscribers: ch.subscribers,
      status: ch.status, status_detail: ch.status_detail,
      removal: ch.removal ? { reason: ch.removal.reason, requested_at: iso(ch.removal.requested_at), delete_at: iso(ch.removal.delete_at) } : null,
      sync_progress: ch.status === 'syncing' && ch.sync && !ch.sync.refresh ? { done: ch.sync.done, total: ch.sync.total } : null,
      video_count: st.count, size_bytes: st.size, queued_count: st.queued, failed_count: st.failed,
      retention_mode: mode, keep_forever: mode === 'forever', gone: ch.gone ? { at: iso(ch.gone.at), reason: ch.gone.reason } : null, topic: ch.topic, topic_source: ch.topic_source, subscribed: ch.subscribed,
      added_at: iso(ch.added_at), last_checked_at: iso(ch.last_checked_at), last_upload_at: iso(st.lastUp), last_download_at: iso(st.lastDl),
      poster_url: posterUrl(ch),
    };
  }
  function channelDetail(ch) {
    const st = chStats(ch);
    return Object.assign(channelSummary(ch), {
      youtube_title: ch.yt_title, about: ch.about, summary: ch.summary_override || ch.about,
      genre: ch.genre_override || (GENRES[ch.genre] || GENRES.general).cat,
      locked_fields: [ch.title_override ? 'title' : null, ch.summary_override ? 'summary' : null, ch.genre_override ? 'genre' : null, ch.poster_choice ? 'poster' : null].filter(Boolean),
      banner_url: ch.banner_url, avatar_url: null, youtube_video_count: ch.youtube_video_count,
      plex_url: st.count ? 'http://plex.local:32400/web/index.html' : null, kept_while_unsubscribed: ch.kept_while_unsubscribed,
      settings: clone(ch.settings), effective_settings: effective(ch),
      stats: { watched_count: st.watched, skipped_count: st.skipped, kept_count: st.kept, protected_count: st.protectedN, sponsorblock_saved_seconds: st.sb, oldest_upload_date: st.oldest, newest_upload_date: st.newest },
    });
  }
  function downloaderOut() {
    const order = runOrder();
    const waiting = S.queue.filter((q) => waitsFor(q) === 'overnight').length;
    let failed = 0; S.videos.forEach((v) => { if (v.state === 'failed') failed++; });
    const c = S.current, v = c && S.videos.get(c.vid), ch = v && chOf(v);
    const state = S.paused ? 'paused' : c ? 'downloading' : order.length ? (order.every((q) => waitsFor(q)) ? 'waiting' : 'downloading') : 'idle';
    const p = S.settings.downloads.pacing;
    return {
      state, paused: S.paused,
      current: c ? { video_id: v.id, channel_id: v.channel_id, channel_title: ch ? chTitle(ch) : '', title: vTitle(v), thumbnail_url: thumbUrl(v), stage: c.stage, progress: stageProgress(c), speed_bps: c.stage === 'download' ? Math.round(c.speed) : null, eta_seconds: c.stage === 'download' ? Math.round((c.total - c.bytes) / c.speed) + 12 : Math.round((1 - stageProgress(c)) * 4) + 3 } : null,
      queue_count: S.queue.length, waiting_count: waiting, failed_count: failed,
      overnight: null,
    };
  }
  function todayOut() {
    const mid = new Date(); mid.setHours(0, 0, 0, 0);
    const m = mid.getTime();
    let added = 0, bytes = 0, backfill = 0, removed = 0;
    S.videos.forEach((v) => {
      if (v.downloaded_at >= m && (v.state === 'downloaded' || v.state === 'removed')) { if (v.backfill) backfill++; else { added++; bytes += v.size_bytes || 0; } }
      if (v.state === 'removed' && v.removed_at >= m && v.removed_reason !== 'manual') removed++;
    });
    return { added, bytes, backfill, removed };
  }
  function feedOut() {
    const iv = S.settings.downloads.check_interval_minutes;
    const mins = once('typical', () => {
      const xs = [];
      S.videos.forEach((v) => { if (v.state === 'downloaded' && !v.backfill && v.downloaded_at && v.downloaded_at > Date.now() - 7 * DAY) xs.push((v.downloaded_at - v.published) / MIN); });
      if (!xs.length) return null;
      xs.sort((a, b) => a - b);
      return Math.round(xs[Math.floor(xs.length / 2)]);
    });
    return { interval_minutes: iv, last_check_at: iso(S.feed.last), next_check_at: iso(S.feed.last + iv * MIN), typical_minutes_to_plex: mins };
  }
  function status() {
    let pending = 0, errs = 0, vids = 0, goneCh = 0, goneV = 0;
    S.channels.forEach((c) => { if (c.status === 'pending_removal') pending++; if (c.status === 'error') errs++; if (c.status === 'gone') goneCh++; });
    S.videos.forEach((v) => { if (v.state === 'downloaded') { vids++; if (v.gone) goneV++; } });
    const st = storageOut();
    return {
      version: '0.1.0-preview', server_time: iso(Date.now()), timezone: 'UTC',
      storage: { used_bytes: st.used_bytes, cap_bytes: CAP, target_bytes: target(), fill: st.fill },
      youtube: null,
      downloader: downloaderOut(), today: todayOut(), feed: feedOut(),
      counts: { channels: S.channels.length, videos: vids, pending_removal: pending, channel_errors: errs, gone_channels: goneCh, gone_videos: goneV },
    };
  }

  function buildHistory() {
    const ev = [], since = Date.now() - 3 * DAY;
    S.videos.forEach((v) => {
      if (!LISTED(v) || !chOf(v)) return;
      if (v.downloaded_at && v.downloaded_at > since && (v.state === 'downloaded' || v.state === 'removed')) ev.push({ at: v.downloaded_at + 150e3, event: 'downloaded', v, size_bytes: v.size_bytes || 0, cut: v.sponsorblock_cut_seconds, detail: null });
      if (v.state.indexOf('skipped') === 0 && v.published > since) ev.push({ at: (v.added_at || v.published) + 10 * MIN, event: 'skipped', v, detail: { skipped_short: 'short', skipped_live: 'live', skipped_too_long: 'too_long' }[v.state] });
      if (v.state === 'removed' && v.removed_at > since) ev.push({ at: v.removed_at, event: 'removed', v, detail: v.removed_reason });
      if (v.state === 'failed' && v.failed_at > since) ev.push({ at: v.failed_at, event: 'failed', v, detail: v.error.message });
    });
    ev.sort((a, b) => b.at - a.at);
    S.history = ev.filter((e) => e.at <= Date.now()).slice(0, 120).map((e) => ({ id: 'h_' + S.histSeq++, at: e.at, event: e.event, vid: e.v.id, size_bytes: e.size_bytes || null, cut: e.cut == null ? null : e.cut, detail: e.detail }));
  }
  const historyOut = (h) => { const v = S.videos.get(h.vid); return { id: h.id, at: iso(h.at), event: h.event, video: v ? videoOut(v) : null, size_bytes: h.size_bytes, sponsorblock_cut_seconds: h.cut, detail: h.detail }; };
  function pushHistory(event, v, extra) {
    const h = Object.assign({ id: 'h_' + S.histSeq++, at: Date.now(), event, vid: v.id, size_bytes: null, cut: null, detail: null }, extra || {});
    S.history.unshift(h); S.history.length = Math.min(S.history.length, 200);
    emit('history', { item: historyOut(h) });
  }
  const touchV = (v) => { bump(); emit('video.state', { video: videoOut(v) }); };
  const touchCh = (ch) => { bump(); if (ch) emit('channel.updated', { channel: channelSummary(ch) }); };
  const touchStorage = () => { bump(); emit('storage', storageOut()); };

  /* ---------- Retention: apply an override, and estimate one ---------- */
  function applyRetention(ch) {
    const eff = effective(ch), mode = eff.retention.mode;
    const inside = insideTest(eff.retention, ch, S.frontier);
    const prot = protections();
    let changed = false;
    ch.catalog.forEach((v) => {
      if (!passes(v, eff)) return;
      const inW = inside(v);
      if (inW && (v.state == null || (v.state === 'removed' && v.removed_reason === 'retention'))) {
        const wasListed = LISTED(v);
        toQueued(v, v.backfill ? 'backfill' : 'new_upload'); v.est = estBytes(ch, v, eff.quality); v.removed_reason = null; v.removed_at = null; v.added_at = Date.now();
        S.queue.push({ vid: v.id, reason: v.reason, added_at: Date.now() });
        bump(); emit(wasListed ? 'video.state' : 'video.added', { video: videoOut(v) }); changed = true;
      } else if (!inW && ['count', 'days', 'since', 'new_only'].includes(mode) && v.state === 'downloaded' && !(prot.get(v.id) === 'keep' || prot.get(v.id) === 'newest')) {
        v.state = 'removed'; v.removed_reason = 'retention'; v.removed_at = Date.now();
        pushHistory('removed', v, { detail: 'retention' }); touchV(v); changed = true;
      } else if (!inW && v.state === 'queued' && v.reason === 'backfill') {
        v.state = null; S.queue = S.queue.filter((q) => q.vid !== v.id); bump(); changed = true;
      }
    });
    listSkipped(ch);
    if (changed) { touchCh(ch); touchStorage(); }
  }
  function extrapolate(ch, eff, eligible) {
    const extra = ch.youtube_video_count - ch.catalog.length;
    if (extra <= 0) return { n: 0, bytes: 0 };
    const ratio = eligible / Math.max(1, ch.catalog.length);
    const avg = ch.catalog.reduce((s, v) => s + (passes(v, eff) ? v.duration_seconds : 0), 0) / Math.max(1, eligible);
    const n = Math.round(extra * ratio);
    return { n, bytes: Math.round(n * avg * bpsFor(ch, eff.quality)) };
  }
  function estimateChannel(b) {
    const ch = needCh(b.channel_id);
    const eff = effective(ch);
    if ('retention' in b) eff.retention = clone(b.retention || { mode: 'fill' });
    ['quality', 'max_duration_minutes', 'include_live'].forEach((k) => { if (k in b) eff[k] = b[k]; });
    const mode = eff.retention.mode;
    const inside = insideTest(eff.retention, ch, S.frontier);
    const prot = protections();
    let cur = 0, eligible = 0, kept = 0, oldest = null, lastInside = false, estimated = false;
    const add = { count: 0, bytes: 0 }, rem = { count: 0, bytes: 0 };
    ch.catalog.forEach((v) => {
      const down = v.state === 'downloaded' || v.state === 'downloading';
      if (v.state === 'downloaded') cur += sizeOf(v);
      if (!passes(v, eff)) return;
      eligible++;
      const inW = inside(v);
      lastInside = inW;
      if (inW) {
        kept++; const d = dayOf(v.published); if (!oldest || d < oldest) oldest = d;
        if (!down) { add.count++; add.bytes += estBytes(ch, v, eff.quality); estimated = true; }
      } else if (down && ['count', 'days', 'since', 'new_only'].includes(mode) && !(prot.get(v.id) === 'keep' || prot.get(v.id) === 'newest')) { rem.count++; rem.bytes += sizeOf(v); }
    });
    if (lastInside && (mode === 'forever' || mode === 'since' || mode === 'days' || (mode === 'count' && kept < eff.retention.count))) {
      const x = extrapolate(ch, eff, eligible);
      const n = mode === 'count' ? Math.min(eff.retention.count - kept, x.n) : x.n;
      if (n > 0) { const bytes = Math.round((x.bytes * n) / Math.max(1, x.n)); add.count += n; add.bytes += bytes; kept += n; estimated = true; oldest = null; }
    }
    const used = usedBytes();
    return { scope: 'channel', channels_affected: 1, current_bytes: cur, projected_bytes: cur - rem.bytes + add.bytes, add, remove: rem, kept_count: kept, oldest_date: oldest,
      reach_date: mode === 'fill' && S.frontier ? dayOf(reachDate() || S.frontier) : null,
      total: { used_bytes: used, projected_bytes: used - rem.bytes + add.bytes, cap_bytes: CAP, target_bytes: target() }, estimated };
  }
  /* "What if the fill target were X?": walk the backlog of fill channels back in time until X is used. */
  function estimateTarget(tgt) {
    const used = usedBytes(), prot = protections();
    const fillChs = S.channels.filter((ch) => effective(ch).retention.mode === 'fill');
    let backlogDown = 0;
    const pool = [];
    fillChs.forEach((ch) => {
      const eff = effective(ch);
      ch.catalog.forEach((v) => {
        if (!v.backfill || !passes(v, eff)) return;
        const down = v.state === 'downloaded';
        if (down) backlogDown += sizeOf(v);
        pool.push([v, down ? sizeOf(v) : estBytes(ch, v, eff.quality), down]);
      });
    });
    pool.sort((a, b) => b[0].published - a[0].published);
    let acc = used - backlogDown, reach = null, kept = 0;
    const add = { count: 0, bytes: 0 }, rem = { count: 0, bytes: 0 };
    let k = 0;
    for (; k < pool.length; k++) {
      const [v, sz, down] = pool[k];
      if (acc + sz > tgt) break;
      acc += sz; reach = v.published; kept++;
      if (!down) { add.count++; add.bytes += sz; }
    }
    for (; k < pool.length; k++) { const [v, sz, down] = pool[k]; if (down && !prot.has(v.id)) { rem.count++; rem.bytes += sz; } }
    const projected = used + add.bytes - rem.bytes;
    return { scope: 'defaults', channels_affected: fillChs.length, current_bytes: used, projected_bytes: projected, add, remove: rem, kept_count: kept,
      oldest_date: reach ? dayOf(reach) : null, reach_date: reach ? dayOf(reach) : null,
      total: { used_bytes: used, projected_bytes: projected, cap_bytes: CAP, target_bytes: tgt }, estimated: add.count > 0 };
  }

  /* ---------- Generated art variants (SVG data URLs, so the picker has real images to show) ---------- */
  const svgUrl = (s) => 'data:image/svg+xml;charset=utf-8,' + encodeURIComponent(s);
  const xmlEsc = (s) => String(s).replace(/[&<>"]/g, (c) => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]));
  function posterVariant(ch, id) {
    const [c1, c2] = T.palette(ch.yt_title);
    const t = xmlEsc(chTitle(ch)), ini = xmlEsc(T.initials(chTitle(ch)));
    const font = "font-family='Segoe UI, Helvetica, Arial, sans-serif'";
    const S2 = (body) => svgUrl(`<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 400 600'><defs><linearGradient id='g' x1='0' y1='0' x2='.4' y2='1'><stop offset='0' stop-color='${c1}'/><stop offset='1' stop-color='${c2}'/></linearGradient><radialGradient id='r' cx='.3' cy='.15' r='.8'><stop offset='0' stop-color='#fff' stop-opacity='.35'/><stop offset='1' stop-color='#fff' stop-opacity='0'/></radialGradient></defs>${body}</svg>`);
    const words = chTitle(ch).toUpperCase().split(/\s+/).slice(0, 3);
    const variants = {
      'gen-1': ['Tubarr classic', S2(`<rect width='400' height='600' fill='url(#g)'/><rect width='400' height='600' fill='url(#r)'/><text x='200' y='300' text-anchor='middle' ${font} font-weight='900' font-size='170' fill='#fff' fill-opacity='.95' letter-spacing='-6'>${ini}</text><rect x='170' y='452' width='60' height='3' fill='#fff' fill-opacity='.6'/><text x='200' y='505' text-anchor='middle' ${font} font-weight='800' font-size='30' fill='#fff' letter-spacing='3'>${t.toUpperCase().slice(0, 22)}</text>`)],
      'gen-2': ['Minimal', S2(`<rect width='400' height='600' fill='#0c0e14'/><circle cx='200' cy='250' r='120' fill='none' stroke='${c1}' stroke-width='10'/><circle cx='200' cy='250' r='84' fill='${c1}' fill-opacity='.18'/><text x='200' y='285' text-anchor='middle' ${font} font-weight='800' font-size='96' fill='${c1}'>${ini}</text><text x='200' y='470' text-anchor='middle' ${font} font-weight='700' font-size='32' fill='#eef0f6'>${t.slice(0, 20)}</text><text x='200' y='510' text-anchor='middle' ${font} font-size='20' fill='#8b93a7' letter-spacing='4'>ON TUBARR</text>`)],
      'gen-3': ['Bold type', S2(`<rect width='400' height='600' fill='${c2}'/><rect y='0' width='400' height='600' fill='url(#g)' opacity='.55'/>${words.map((w, i) => `<text x='26' y='${170 + i * 118}' ${font} font-weight='900' font-size='${Math.max(40, Math.min(120, Math.floor(560 / Math.max(4, w.length))))}' fill='#fff' letter-spacing='-3'>${xmlEsc(w)}</text>`).join('')}<rect x='26' y='520' width='90' height='8' fill='${c1}'/>`)],
      'gen-4': ['Spotlight', S2(`<rect width='400' height='600' fill='#07080c'/><ellipse cx='200' cy='230' rx='230' ry='260' fill='${c1}' fill-opacity='.55'/><ellipse cx='200' cy='230' rx='150' ry='170' fill='${c1}' fill-opacity='.45'/><text x='200' y='285' text-anchor='middle' ${font} font-weight='900' font-size='150' fill='#fff'>${ini}</text><rect y='420' width='400' height='180' fill='#07080c' fill-opacity='.7'/><text x='200' y='515' text-anchor='middle' ${font} font-weight='800' font-size='34' fill='#fff'>${t.slice(0, 20)}</text>`)],
      youtube: ['From the YouTube avatar', S2(`<rect width='400' height='600' fill='${c2}'/><rect width='400' height='600' fill='url(#g)' opacity='.35'/><circle cx='200' cy='250' r='110' fill='${c1}'/><text x='200' y='290' text-anchor='middle' ${font} font-weight='800' font-size='110' fill='#fff'>${ini.slice(0, 1)}</text><text x='200' y='470' text-anchor='middle' ${font} font-weight='700' font-size='32' fill='#fff'>${t.slice(0, 20)}</text>`)],
    };
    const v = variants[id] || variants['gen-1'];
    return { id, label: v[0], url: v[1] };
  }
  function thumbVariant(v, id) {
    const [c1, c2] = T.palette(v.id);
    const font = "font-family='Impact, Arial Black, Segoe UI, sans-serif'";
    const word = xmlEsc((vTitle(v).split(/[\s:]+/).filter((w) => w.length > 3).sort((a, b) => b.length - a.length)[0] || vTitle(v)).toUpperCase().slice(0, 12));
    const at = { 'frame-25': 0.25, 'frame-50': 0.5, 'frame-75': 0.75 }[id];
    const body = at == null
      ? `<rect width='640' height='360' fill='${c2}'/><circle cx='500' cy='170' r='170' fill='${c1}' fill-opacity='.85'/><text x='36' y='318' ${font} font-size='96' fill='#fff'>${word}</text>`
      : `<rect width='640' height='360' fill='${c2}'/><rect x='${60 + at * 200}' y='40' width='300' height='280' rx='40' fill='${c1}' fill-opacity='.5'/><circle cx='${200 + at * 260}' cy='180' r='70' fill='#fff' fill-opacity='.25'/><rect x='20' y='310' width='118' height='34' rx='6' fill='#000' fill-opacity='.7'/><text x='79' y='334' text-anchor='middle' font-family='Segoe UI, Arial, sans-serif' font-size='20' fill='#fff'>${T.fmtClock(Math.round(v.duration_seconds * at))}</text>`;
    return { id, label: at == null ? "YouTube's thumbnail" : 'Frame at ' + Math.round(at * 100) + '%', url: svgUrl(`<svg xmlns='http://www.w3.org/2000/svg' viewBox='0 0 640 360'>${body}</svg>`) };
  }

  /* ---------- Live simulation ---------- */
  let lastTick = Date.now(), lastStatusEmit = 0, lastProgressEmit = 0, nextUploadAt = Date.now() + 12e3, uploadSeq = 0;
  function tick() {
    const now = Date.now(), dt = Math.min(5, (now - lastTick) / 1000);
    lastTick = now;
    S.channels.forEach((ch) => {
      if (ch.status !== 'syncing' || !ch.sync) return;
      if (ch.sync.total) {
        const before = ch.sync.done;
        ch.sync.done = Math.min(ch.sync.total, Math.round(ch.sync.done + (ch.sync.total * dt * 1000) / Math.max(1, ch.sync.ends - ch.sync.started)));
        if (ch.sync.done !== before && now - (ch.sync.lastEmit || 0) > 1000) { ch.sync.lastEmit = now; touchCh(ch); }
      }
      if (now >= ch.sync.ends) finishSync(ch);
    });
    S.channels.slice().forEach((ch) => { if (ch.status === 'pending_removal' && ch.removal && now >= ch.removal.delete_at) deleteChannel(ch); });
    if (!S.paused) {
      if (!S.current) startNext();
      const c = S.current;
      if (c) {
        if (c.stage === 'download') {
          c.speed = T.clamp(c.speed + (Math.random() - 0.5) * 0.9e6, 4.4e6, 7.1e6);
          c.bytes = Math.min(c.total, c.bytes + c.speed * dt);
          if (c.bytes >= c.total) nextStage(c);
        } else if (stageProgress(c) >= 1) nextStage(c);
      }
      if (S.current && now - lastProgressEmit >= 1000) {
        lastProgressEmit = now;
        const cc = S.current;
        emit('video.progress', { id: cc.vid, stage: cc.stage, progress: stageProgress(cc), bytes_done: Math.round(cc.bytes), bytes_total: cc.total, speed_bps: cc.stage === 'download' ? Math.round(cc.speed) : null, eta_seconds: cc.stage === 'download' ? Math.round((cc.total - cc.bytes) / cc.speed) + 12 : null });
      }
    }
    if (now - S.feed.last >= S.settings.downloads.check_interval_minutes * MIN) S.feed.last = now;
    if ((SCENARIO === 'default' || FULL) && now >= nextUploadAt) { nextUploadAt = now + 32e3 + Math.random() * 18e3; simulateNewUpload(); }
    if (S.plex && S.plex.library && S.plex.library.state === 'scanning' && now > S.plex.scanEnds) { S.plex.library.state = 'ready'; S.plex.library.last_scan_at = now; }
    Object.values(S.setup.pins).forEach((pin) => { if (pin.state === 'waiting' && now >= pin.authAt) pin.state = 'authorized'; });
    if (now - lastStatusEmit >= 1000) { lastStatusEmit = now; emit('status', status()); }
  }
  function nextStage(c) {
    const v = S.videos.get(c.vid);
    const i = c.stages.indexOf(c.stage);
    if (i < c.stages.length - 1) { c.stage = c.stages[i + 1]; c.stageAt = Date.now(); touchV(v); return; }
    const ch = chOf(v);
    landed(v, ch, Date.now());
    v.size_bytes = c.total; v.sponsorblock_cut_seconds = c.cut || 0; v.error = null;
    S.current = null;
    bump();
    touchV(v);
    pushHistory('downloaded', v, { size_bytes: c.total, cut: v.sponsorblock_cut_seconds });
    // at the target, every arrival pushes the next one out
    if (S.rolling || usedBytes() > target()) { S.rolling = true; rollOne(Date.now()); }
    touchCh(ch); touchStorage();
    startNext();
  }
  function startNext() {
    const next = runOrder().find((q) => !waitsFor(q));
    if (!next) return;
    S.queue = S.queue.filter((q) => q !== next);
    const v = S.videos.get(next.vid);
    if (!v || v.state !== 'queued') return;
    const ch = chOf(v), eff = effective(ch), rr = T.rng('job:' + v.id);
    v.state = 'downloading';
    const total = Math.round(v.est || estBytes(ch, v, eff.quality));
    const cut = eff.sponsorblock.length && rr() < 0.6 ? Math.round(between(rr, 20, 120)) : 0;
    S.current = { vid: v.id, stage: 'download', stageAt: Date.now(), bytes: 0, total, speed: 5.6e6, started: Date.now(), stages: cut ? ['download', 'sponsorblock', 'artwork', 'plex'] : ['download', 'artwork', 'plex'], cut };
    touchV(v);
  }
  const UPLOADERS = ['@PocketGadgetDaily', '@FieldNotesScience', '@RustToRoad', '@BenchmarkBarn', '@SlowTrainJournal', '@ScrapyardRobotics', '@KitchenChemistryHour', '@QuietPCCorner', '@LedgerOfEmpires', '@TwoCarGarage', '@SimpleMachinesClub', '@GrainAndGlue'];
  function simulateNewUpload() {
    const handle = UPLOADERS[uploadSeq++ % UPLOADERS.length];
    const ch = S.byHandle.get(handle.toLowerCase());
    if (!ch || ch.status !== 'monitored') return;
    const r = T.rng('live:' + handle + ':' + uploadSeq);
    const G = GENRES[ch.genre] || GENRES.general;
    const short = r() < 0.18;
    const title = fill(pick(r, short ? G.s : G.t), r);
    const dur = short ? Math.round(between(r, 15, 58)) : Math.round(between(r, G.dur[0], G.dur[1]) * 60);
    const v = newEntry(ch, rid(r, 11), title, Date.now() - between(r, 1, 9) * MIN, dur, short ? 'short' : 'video');
    v.added_at = Date.now(); v.backfill = false; S.feed.last = Date.now();
    const eff = effective(ch);
    if (short) v.state = 'skipped_short';
    else if (eff.sponsorblock.length && S.settings.downloads.sponsorblock_wait_hours && r() < 0.3) { v.state = 'waiting_sponsorblock'; v.wait_until = v.published + S.settings.downloads.sponsorblock_wait_hours * HOUR; }
    else {
      v.state = 'queued'; v.reason = 'new_upload'; v.est = estBytes(ch, v, eff.quality);
      const at = S.queue.findIndex((q) => q.reason !== 'new_upload');
      const item = { vid: v.id, reason: 'new_upload', added_at: Date.now() };
      if (at < 0) S.queue.push(item); else S.queue.splice(at, 0, item);
    }
    const d = dayOf(v.published);
    const sameDay = ch.catalog.filter((x) => x.kind !== 'short' && dayOf(x.published) === d).length;
    v.episode = short ? null : 'S' + d.slice(0, 4) + 'E' + d.slice(5, 7) + d.slice(8, 10) + pad(sameDay + 1);
    ch.catalog.unshift(v); S.videos.set(v.id, v); ch.last_checked_at = Date.now();
    bump();
    emit('video.added', { video: videoOut(v) });
    if (short) pushHistory('skipped', v, { detail: 'short' });
    touchCh(ch);
  }
  function finishSync(ch) {
    const wasRefresh = ch.sync && ch.sync.refresh;
    ch.status = 'monitored'; ch.sync = null; ch.last_checked_at = Date.now();
    if (!wasRefresh) {
      // a new channel: its uploads are read now. The newest 3 download right away; the backlog joins the fill tonight.
      ch.added_at = Math.min(ch.added_at, Date.now() - 60e3);
      const eff = effective(ch), inside = insideTest(eff.retention, ch, S.frontier);
      let n = 0;
      ch.catalog.forEach((v) => {
        v.backfill = true;
        if (!passes(v, eff)) return;
        if (n < 3) { toQueued(v, 'new_upload'); v.est = estBytes(ch, v, eff.quality); v.added_at = Date.now(); n++; return; }
        if (inside(v) && eff.retention.mode !== 'new_only') { toQueued(v, 'backfill'); v.est = estBytes(ch, v, eff.quality); v.added_at = Date.now(); }
      });
      listSkipped(ch); episodeCodes(ch); indexChannel(ch);
      ch.catalog.filter((v) => v.state === 'queued').forEach((v) => S.queue.push({ vid: v.id, reason: v.reason, added_at: Date.now() }));
      const rank = { new_upload: 0, retry: 1, manual: 2, backfill: 3 };
      S.queue.sort((a, b) => rank[a.reason] - rank[b.reason]);
      bump();
      ch.catalog.filter(LISTED).forEach((v) => emit('video.added', { video: videoOut(v) }));
    }
    touchCh(ch);
  }
  function deleteChannel(ch) {
    S.channels = S.channels.filter((c) => c !== ch);
    S.byId.delete(ch.id); S.byHandle.delete(ch.handle.toLowerCase());
    ch.catalog.forEach((v) => { if (LISTED(v)) emit('video.removed', { id: v.id, channel_id: ch.id }); S.videos.delete(v.id); });
    S.queue = S.queue.filter((q) => S.videos.has(q.vid));
    if (S.current && !S.videos.has(S.current.vid)) S.current = null;
    bump();
    emit('channel.removed', { id: ch.id }); touchStorage();
  }

  /* ---------- Handlers ---------- */
  class HttpErr { constructor(status, code, message) { this.status = status; this.code = code; this.message = message; } }
  const E = (status, code, message) => new HttpErr(status, code, message);
  function needCh(id) { const ch = S.byId.get(id); if (!ch) throw E(404, 'not_found', 'Channel not found.'); return ch; }
  function needV(id) { const v = S.videos.get(id); if (!LISTED(v)) throw E(404, 'not_found', 'Video not found.'); return v; }
  function normalizeQ(q) {
    q = String(q || '').trim();
    let m = q.match(/\/channel\/(UC[\w-]{22})/) || q.match(/^(UC[\w-]{22})$/);
    if (m) return { id: m[1] };
    m = q.match(/@([\w.\-·]+)/);
    if (m) return { handle: '@' + m[1].replace(/[/?#].*$/, '') };
    m = q.match(/youtube\.com\/(?:c\/|user\/)?([\w.-]+)/i);
    if (m && !/^(watch|shorts|feed|playlist)$/i.test(m[1])) return { handle: '@' + m[1] };
    if (/^[\w.-]{3,30}$/.test(q)) return { handle: '@' + q };
    return null;
  }
  /* Invented channels that are not in the seed, so a lookup or the sample import can show a new one. */
  const EXTRA = { '@harborlightsvlog': ['Harbor Lights Vlog', 1210000, 'travel'], '@clockworkcuriosities': ['Clockwork Curiosities', 2730000, 'science'], '@copperpotcooking': ['Copper Pot Cooking', 640000, 'cooking'], '@bitandbrace': ['Bit and Brace', 380000, 'woodworking'] };
  function previewFor(n) {
    if (n.id) { const ch = S.byId.get(n.id); if (ch) return { ch }; }
    const h = (n.handle || '').toLowerCase();
    const ch = S.byHandle.get(h);
    if (ch) return { ch };
    const seedRow = SEED.find((s) => s[1].toLowerCase() === h), ex = EXTRA[h], r = T.rng('lookup:' + h);
    let title, subs, genre;
    if (seedRow) [title, , subs, genre] = seedRow;
    else if (ex) [title, subs, genre] = ex;
    else {
      const raw = (n.handle || '@' + (n.id || 'channel')).replace(/^@/, '');
      title = raw.replace(/[_.-]+/g, ' ').replace(/([a-z])([A-Z])/g, '$1 $2').replace(/\b\w/g, (c) => c.toUpperCase());
      subs = Math.round(Math.pow(10, between(r, 3.2, 6.6))); genre = 'general';
    }
    const handle = seedRow ? seedRow[1] : n.handle || '@' + title.replace(/\s+/g, '');
    return { preview: { id: 'UC' + rid(T.rng('ch:' + handle), 22), title, handle, url: 'https://www.youtube.com/' + handle, subscribers: subs, about: (GENRES[genre] || GENRES.general).about, avatar_url: null, banner_url: null, youtube_video_count: Math.round(between(r, 40, 900)), genre } };
  }
  /* POST /api/lookup {q}: a channel link or @handle -> a preview (or the channel, when it is already added). */
  function lookup(q) {
    const n = normalizeQ(q);
    if (!n) throw E(404, 'not_found', 'No YouTube channel found for that link or handle.');
    const p = previewFor(n);
    if (p.ch) { const d = channelDetail(p.ch); return { channel: { id: d.id, title: d.title, handle: d.handle, url: d.url, subscribers: d.subscribers, about: d.about, avatar_url: null, banner_url: d.banner_url, youtube_video_count: d.youtube_video_count, poster_url: d.poster_url }, already_added: true }; }
    const pv = Object.assign({}, p.preview); delete pv.genre;
    return { channel: pv, already_added: false };
  }
  function addChannelFrom(p, settings) {
    const ch = makeChannel([p.title, p.handle, p.subscribers, p.genre || 'general'], S.channels.length);
    ch.added_at = Date.now();
    ch.youtube_video_count = p.youtube_video_count || ch.youtube_video_count;
    Object.assign(ch.settings, clone(settings || {}));
    ch.status = 'syncing';
    ch.sync = { done: 0, total: Math.min(ch.youtube_video_count, 400), started: Date.now(), ends: Date.now() + 16e3 + Math.random() * 8e3, refresh: false };
    ch.catalog = genCatalog(ch, { salt: ':added', newest: Date.now() - between(T.rng(ch.id), 0.5, 5) * DAY });
    ch.catalog.forEach((v) => { v.state = null; });
    S.channels.push(ch); indexChannel(ch); bump();
    emit('channel.added', { channel: channelSummary(ch) });
    return ch;
  }
  /* Takeout CSV rows (Channel Id,Channel Url,Channel Title), channel links (/@x, /channel/UC…, /c/x, /user/x),
     bare @handles. The mock keys every channel by a handle (CSV rows: made from the title). */
  function parseImport(text, htmlText) {
    const src = (htmlText || '') + '\n' + (text || ''), found = new Map();
    const add = (h) => { h = h.replace(/[.\-·]+$/, ''); if (h.length < 3 || /@(gmail|youtube|google)\b/i.test(h)) return; if (!found.has(h.toLowerCase())) found.set(h.toLowerCase(), h); };
    src.split(/\r?\n/).forEach((line) => {
      const csv = line.match(/^\s*(UC[\w-]{22})\s*,[^,]*,\s*"?([^"]+?)"?\s*$/);
      if (csv) { const known = S.byId.get(csv[1]); add(known ? known.handle : '@' + csv[2].replace(/[^\w.-]+/g, '')); return; }
      const re = /@([A-Za-z0-9._\-·]{2,40})/g;
      let m, any = false;
      while ((m = re.exec(line))) { add('@' + m[1]); any = true; }
      if (any) return;
      const u = line.match(/youtube\.com\/(?:c\/|user\/)([\w.-]+)/i);
      if (u) { add('@' + u[1]); return; }
      const id = line.match(/(UC[\w-]{22})/);
      if (id) { const known = S.byId.get(id[1]); add(known ? known.handle : '@Channel' + id[1].slice(2, 8)); }
    });
    return found;
  }
  function videoDownload(v, next) {
    if (v.state === 'skipped_short') throw E(409, 'conflict', 'Shorts are always skipped.');
    if (v.state === 'downloaded' || v.state === 'downloading') throw E(409, 'conflict', 'That video is already in Plex.');
    if (v.state === 'upcoming') throw E(409, 'conflict', "That premiere hasn't started yet. Tubarr queues it automatically.");
    const reason = v.state === 'failed' ? 'retry' : v.state === 'waiting_sponsorblock' ? 'new_upload' : 'manual';
    const existing = S.queue.find((q) => q.vid === v.id);
    if (existing) S.queue = S.queue.filter((q) => q !== existing);
    const ch = chOf(v);
    toQueued(v, reason); v.error = null; v.removed_reason = null; v.removed_at = null; v.wait_until = null;
    v.est = estBytes(ch, v, effective(ch).quality);
    const item = existing || { vid: v.id, reason, added_at: Date.now() };
    if (next) S.queue.unshift(item);
    else { const fb = S.queue.findIndex((q) => q.reason === 'backfill'); if (fb < 0) S.queue.push(item); else S.queue.splice(fb, 0, item); }
    touchV(v); touchCh(ch);
    return v;
  }
  function videoDelete(v) {
    if (S.current && S.current.vid === v.id) S.current = null;
    S.queue = S.queue.filter((q) => q.vid !== v.id);
    const wasIn = v.state === 'downloaded';
    v.state = 'removed'; v.removed_reason = 'manual'; v.removed_at = Date.now(); v.keep = false;
    pushHistory('removed', v, { detail: 'manual' });
    touchV(v); touchCh(chOf(v)); if (wasIn) touchStorage();
    return v;
  }
  function readFileBody(b) { if (!b || !b.file_data_url) throw E(400, 'bad_request', 'Choose an image file.'); if (b.file_data_url.length > 14e6) throw E(413, 'too_large', 'That image is over 10 MB.'); return b.file_data_url; }
  function seriesOut(ch, p) {
    const vids = p.members.map((id) => S.videos.get(id)).filter(Boolean);
    const dates = vids.map((v) => v.published).sort((a, b) => a - b);
    return { id: p.id, title: p.title, episode_count: vids.length, in_plex_count: vids.filter((v) => v.state === 'downloaded').length, enabled: p.enabled, order: p.order,
      first_date: dates.length ? dayOf(dates[0]) : null, last_date: dates.length ? dayOf(dates[dates.length - 1]) : null, url: 'https://www.youtube.com/playlist?list=' + p.id };
  }
  function trimStatus() {
    let n = 0, sec = 0;
    S.videos.forEach((v) => { if (v.state === 'downloaded' && v.trim && v.trim.state === 'trimmed') { n++; sec += v.trim.removed_seconds; } });
    return { available: true, enabled: S.trimarr.enabled, version: 'mock', trimmed_videos: n, removed_seconds: sec, queue: 0, channels_enabled: S.trimarr.channels.size, last_run_at: iso(S.trimarr.last_run_at) };
  }
  function plexOut() {
    const p = S.plex;
    let shows = 0, eps = 0;
    S.channels.forEach((c) => { const st = chStats(c); if (st.count) { shows++; eps += st.count; } });
    return { state: p.state, url: p.url, server_name: p.server_name, version: p.version, checked_at: iso(p.checked_at), message: p.message,
      library: p.library ? Object.assign({}, p.library, { show_count: shows, episode_count: eps, last_scan_at: iso(p.library.last_scan_at), shared_with_friends: p.library.shared_with_friends.slice() }) : null };
  }

  /* ---------- Sign-in, setup and network (mock stand-ins) ---------- */
  function authState() { return { setup_required: false, setup_code_required: false, authenticated: true, user: 'admin', csrf: null, via: 'session' }; }
  function apikeyOut() { return S.apikey ? { exists: true, created_at: iso(S.apikey.created_at), hint: S.apikey.hint } : { exists: false, created_at: null, hint: null }; }
  function audit(who, action, detail) { S.audit.unshift({ at: Date.now(), who, action, detail: detail || '' }); if (S.audit.length > 200) S.audit.length = 200; }
  const MOCK_SERVERS = [{ name: 'Home Server', owned: true, connections: [
    { uri: 'http://plex.local:32400', local: true, relay: false },
    { uri: 'https://203-0-113-5.a1b2c3d4.plex.direct:32400', local: false, relay: false },
    { uri: 'https://relay-a1b2c3.plex.direct:8443', local: false, relay: true },
  ] }];
  const MOCK_LIBRARIES = [
    { key: '7', title: 'YouTube', agent: 'tv.plex.agents.none', agent_label: 'Personal Media Shows (local NFO files)', paths: ['/data/youtube'] },
    { key: '2', title: 'TV Shows', agent: 'tv.plex.agents.series', agent_label: 'Plex Series', paths: ['/data/tv'] },
  ];
  function setupPlexOut() {
    const su = S.setup, p = S.plex;
    if (!su.configured) return { configured: false, url: null, token_set: su.token_set, library: null, plex_root: null, state: 'not_configured', server_name: null, message: null };
    return { configured: true, url: p.url, token_set: su.token_set, library: su.library, plex_root: su.plex_root, state: p.state, server_name: p.server_name, message: p.message };
  }
  function maskProxy(url) {
    const m = String(url || '').trim().match(/^(https?|socks5h?):\/\/(?:([^@\/\s]*)@)?([^\/\s:]+)(?::(\d+))?\/?$/i);
    if (!m) throw E(400, 'bad_request', 'The address should look like socks5h://user:pass@host:1080 (http, https, socks5 or socks5h).');
    return m[1].toLowerCase() + '://' + (m[2] ? '***@' : '') + m[3] + (m[4] ? ':' + m[4] : '');
  }
  function needProxy(id) { const p = S.network.proxies.find((x) => x.id === id); if (!p) throw E(404, 'not_found', 'No proxy with that id.'); return p; }
  const proxyIp = (p) => '198.51.100.' + (10 + (T.hash(p.id) % 200));
  /* Which line is active after a change: single -> the picked proxy; failover/rotate -> the first in the order. */
  function netActivate(reason) {
    const N = S.network, st = N.status, now = Date.now();
    const want = N.mode === 'direct' ? 'direct' : N.mode === 'single' ? N.selected : N.order[0];
    const p = N.proxies.find((x) => x.id === want);
    const next = p ? p.id : 'direct';
    st.next_rotate_at = N.mode === 'rotate' ? iso(now + N.rotate_hours * HOUR) : null;
    st.preferred_retry_at = null; st.paused = false;
    if (next === st.active) return;
    st.last_switch = { at: iso(now), from_name: st.active_name, to_name: p ? p.name : 'Direct', reason };
    Object.assign(st, { active: next, active_name: p ? p.name : 'Direct', ip: p ? proxyIp(p) : '203.0.113.5', latency_ms: p ? 210 : 45, since: now, reason: null });
    audit('system', 'network.switch', st.last_switch.from_name + ' → ' + st.last_switch.to_name + ' (' + reason + ')');
  }
  function netOut() {
    const N = S.network;
    return { mode: N.mode, proxies: N.proxies.map((p) => ({ id: p.id, name: p.name, masked: p.masked, scheme: p.scheme })), order: N.order.slice(), selected: N.selected,
      fail_threshold: N.fail_threshold, cooldown_minutes: N.cooldown_minutes, rotate_hours: N.rotate_hours, check_preferred: N.check_preferred,
      never_direct: N.never_direct, never_direct_auto: N.never_direct_auto,
      status: Object.assign({}, N.status, { since: iso(N.status.since), updated_at: iso(Date.now() - 20e3) }) };
  }

  const handlers = [
    ['GET', /^\/api\/status$/, () => status()],
    ['GET', /^\/api\/timeline$/, (m, b, qs) => {
      const sort = qs.get('sort') === 'published' ? 'published' : 'activity';
      const states = qs.get('state') ? new Set(qs.get('state').split(',')) : null;
      const chans = qs.getAll('channel').filter(Boolean), cset = chans.length ? new Set(chans) : null;
      const kept = qs.get('kept') === '1', bf = qs.get('backfill');
      const from = qs.get('from'), to = qs.get('to'), text = (qs.get('q') || '').trim().toLowerCase();
      const limit = Math.min(200, Number(qs.get('limit')) || 60);
      const offset = qs.get('cursor') ? Number(String(qs.get('cursor')).replace(/^o:/, '')) || 0 : 0;
      const key = (v) => (sort === 'activity' ? activityAt(v) || v.published : v.published);
      const list = [];
      S.videos.forEach((v) => {
        if (!LISTED(v)) return;
        const ch = chOf(v); if (!ch) return;
        if (states && !states.has(v.state)) return;
        if (cset && !cset.has(v.channel_id)) return;
        if (kept && !v.keep) return;
        if (qs.get('gone') === '1' && !v.gone) return;
        if (bf === '1' && !v.backfill) return;
        if (bf === '0' && v.backfill) return;
        const d = dayOf(key(v));
        if (from && d < from) return;
        if (to && d > to) return;
        if (text && (vTitle(v) + ' ' + chTitle(ch)).toLowerCase().indexOf(text) < 0) return;
        list.push(v);
      });
      list.sort((a, b2) => key(b2) - key(a));
      return { videos: list.slice(offset, offset + limit).map(videoOut), next: offset + limit < list.length ? 'o:' + (offset + limit) : null, total: list.length };
    }],
    ['GET', /^\/api\/channels$/, () => ({ channels: S.channels.map(channelSummary) })],
    ['POST', /^\/api\/channels\/refresh$/, () => {
      S.feed.last = Date.now();
      S.channels.forEach((ch) => { if (ch.status === 'monitored' || ch.status === 'error') { ch.status = 'syncing'; ch.status_detail = null; ch.sync = { done: null, total: null, started: Date.now(), ends: Date.now() + 3e3 + Math.random() * 9e3, refresh: true }; touchCh(ch); } });
      return [202, { ok: true, message: 'Checking all ' + S.channels.length + ' channels for new uploads.' }];
    }],
    ['POST', /^\/api\/channels$/, (m, b) => {
      const n = normalizeQ(b && b.q);
      if (!n) throw E(400, 'bad_request', 'Paste a channel link or an @handle.');
      const p = previewFor(n);
      if (p.ch) throw E(409, 'conflict', chTitle(p.ch) + ' is already in Tubarr.');
      return [201, channelDetail(addChannelFrom(p.preview, b.settings))];
    }],
    ['GET', /^\/api\/channels\/([\w-]+)$/, (m) => channelDetail(needCh(m[1]))],
    ['PATCH', /^\/api\/channels\/([\w-]+)$/, (m, b) => {
      const ch = needCh(m[1]);
      const s = (b && b.settings) || {}, meta = (b && b.meta) || {};
      const before = JSON.stringify(effective(ch));
      ['retention', 'max_duration_minutes', 'quality', 'include_live', 'sponsorblock'].forEach((k) => { if (k in s) ch.settings[k] = clone(s[k]); });
      if ('title' in meta) ch.title_override = meta.title ? String(meta.title).trim() : null;
      if ('summary' in meta) ch.summary_override = meta.summary ? String(meta.summary) : null;
      if ('genre' in meta) ch.genre_override = meta.genre ? String(meta.genre).trim() : null;
      if ('topic' in meta) { if (!TOPICS.some((t) => t[0] === meta.topic)) throw E(400, 'bad_request', 'Unknown topic.'); ch.topic = meta.topic; ch.topic_source = 'user'; }
      bump();
      if (JSON.stringify(effective(ch)) !== before) applyRetention(ch);
      touchCh(ch);
      if ('title' in meta) ch.catalog.forEach((v) => { if (LISTED(v)) touchV(v); });
      return channelDetail(ch);
    }],
    ['GET', /^\/api\/topics$/, () => ({ topics: TOPICS.map(([id, label]) => ({ id, label, channel_count: S.channels.filter((c) => c.topic === id).length })) })],
    ['GET', /^\/api\/channels\/([\w-]+)\/series$/, (m) => {
      const ch = needCh(m[1]);
      return { series: ch.playlists.filter((p) => !p.ignored).map((p) => seriesOut(ch, p)), ignored: ch.playlists.filter((p) => p.ignored).map((p) => ({ id: p.id, title: p.title, video_count: p.count, reason: p.reason, url: 'https://www.youtube.com/playlist?list=' + p.id })) };
    }],
    ['PATCH', /^\/api\/channels\/([\w-]+)\/series\/([\w-]+)$/, (m, b) => {
      const ch = needCh(m[1]);
      const p = ch.playlists.find((x) => x.id === m[2]);
      if (!p) throw E(404, 'not_found', 'Playlist not found.');
      b = b || {};
      if (p.ignored && b.enabled) {
        // promote: take a few of the channel's videos as its parts
        p.ignored = false; p.reason = null;
        const pool = ch.catalog.filter((v) => LISTED(v) && v.kind !== 'short' && v.state.indexOf('skipped') !== 0 && !v.series).slice(0, Math.min(p.count, 8)).reverse();
        p.members = pool.map((v) => v.id); pool.forEach((v, i) => { v.series = p; v.part = i + 1; });
      }
      if ('enabled' in b) p.enabled = !!b.enabled;
      if (b.order === 'playlist' || b.order === 'upload') p.order = b.order;
      p.members.forEach((id) => { const v = S.videos.get(id); if (v && LISTED(v)) touchV(v); });
      return seriesOut(ch, p);
    }],
    ['GET', /^\/api\/channels\/([\w-]+)\/posters$/, (m) => {
      const ch = needCh(m[1]);
      const variants = ['gen-1', 'gen-2', 'gen-3', 'gen-4', 'youtube'].map((id) => posterVariant(ch, id));
      if (ch.art_poster) variants.unshift({ id: 'art', label: 'Tubarr artwork', url: ch.art_poster });
      if (ch.poster_upload) variants.push({ id: 'upload', label: 'Your upload', url: ch.poster_upload });
      return { current: ch.poster_choice || (ch.art_poster ? 'art' : null), variants };
    }],
    ['POST', /^\/api\/channels\/([\w-]+)\/poster$/, (m, b) => {
      const ch = needCh(m[1]);
      if (b && b.file_data_url) { ch.poster_upload = readFileBody(b); ch.poster_choice = 'upload'; }
      else if (b && b.variant) ch.poster_choice = b.variant === 'art' ? null : b.variant;
      else throw E(400, 'bad_request', 'Choose a poster.');
      touchCh(ch);
      return channelDetail(ch);
    }],
    ['DELETE', /^\/api\/channels\/([\w-]+)$/, (m, b) => {
      const ch = needCh(m[1]);
      if (b && b.immediate) { deleteChannel(ch); return { ok: true, deleted: true }; }
      ch.status = 'pending_removal'; ch.status_detail = null; ch.sync = null;
      ch.removal = { reason: 'manual', requested_at: Date.now(), delete_at: Date.now() + 3 * DAY };
      if (S.current && S.videos.get(S.current.vid).channel_id === ch.id) { S.videos.get(S.current.vid).state = 'queued'; S.current = null; }
      dropQueued(ch);
      touchCh(ch);
      return channelDetail(ch);
    }],
    ['GET', /^\/api\/channels\/([\w-]+)\/videos$/, (m) => {
      const ch = needCh(m[1]);
      const list = ch.catalog.filter(LISTED).sort((a, b) => b.published - a.published).map(videoOut);
      return { videos: list, total: list.length };
    }],
    ['POST', /^\/api\/channels\/([\w-]+)\/refresh$/, (m) => {
      const ch = needCh(m[1]);
      if (ch.status === 'pending_removal') throw E(409, 'conflict', 'This channel is pending removal. Undo that first.');
      if (ch.status === 'gone') throw E(409, 'conflict', 'This channel no longer exists on YouTube, so there is nothing new to check. Its videos stay in Plex.');
      ch.status = 'syncing'; ch.status_detail = null;
      ch.sync = { done: null, total: null, started: Date.now(), ends: Date.now() + 4e3, refresh: true };
      touchCh(ch);
      return [202, { ok: true, message: 'Checking ' + chTitle(ch) + ' for new uploads.' }];
    }],
    ['POST', /^\/api\/channels\/([\w-]+)\/repolish$/, (m) => { const ch = needCh(m[1]); return [202, { ok: true, message: 'Re-polishing ' + chTitle(ch) + ' in Plex. New artwork shows up in a minute or two.' }]; }],
    ['POST', /^\/api\/channels\/([\w-]+)\/restore$/, (m) => {
      const ch = needCh(m[1]);
      if (ch.status !== 'pending_removal') throw E(409, 'conflict', 'This channel is not pending removal.');
      if (ch.removal && ch.removal.reason === 'unsubscribed') ch.kept_while_unsubscribed = true;
      ch.status = 'monitored'; ch.removal = null;
      touchCh(ch);
      return channelDetail(ch);
    }],
    ['POST', /^\/api\/lookup$/, (m, b) => lookup(b && b.q)],
    ['GET', /^\/api\/lookup$/, (m, b, qs) => lookup(qs.get('q'))],   // the older form; harmless to keep
    ['POST', /^\/api\/videos\/bulk$/, (m, b) => {
      const ids = (b && b.ids) || [], action = b && b.action;
      if (!['keep', 'unkeep', 'delete', 'download'].includes(action)) throw E(400, 'bad_request', 'Unknown bulk action.');
      let done = 0; const skipped = [];
      ids.forEach((id) => {
        const v = S.videos.get(id);
        if (!LISTED(v)) { skipped.push({ id, reason: 'Video not found.' }); return; }
        try {
          if (action === 'keep' || action === 'unkeep') {
            if (v.state !== 'downloaded' && action === 'keep') { skipped.push({ id, reason: 'Only videos in Plex can be kept.' }); return; }
            v.keep = action === 'keep'; touchV(v);
          } else if (action === 'delete') { if (v.state === 'removed' || v.state.indexOf('skipped') === 0) { skipped.push({ id, reason: 'Nothing to delete.' }); return; } videoDelete(v); }
          else videoDownload(v, false);
          done++;
        } catch (e) { if (e instanceof HttpErr) skipped.push({ id, reason: e.message }); else throw e; }
      });
      return { done, skipped };
    }],
    ['GET', /^\/api\/videos\/([\w-]+)$/, (m) => videoDetail(needV(m[1]))],
    ['PATCH', /^\/api\/videos\/([\w-]+)$/, (m, b) => {
      const v = needV(m[1]); b = b || {};
      if ('title' in b) v.title_override = b.title && String(b.title).trim() !== v.yt_title ? String(b.title).trim() : null;
      if ('summary' in b) v.summary_override = b.summary && b.summary !== describe(v) ? String(b.summary) : null;
      if ('keep' in b) { if (b.keep && v.state !== 'downloaded') throw E(409, 'conflict', 'Only videos in Plex can be kept.'); v.keep = !!b.keep; }
      touchV(v);
      return videoDetail(v);
    }],
    ['GET', /^\/api\/videos\/([\w-]+)\/thumbnails$/, (m) => {
      const v = needV(m[1]);
      const variants = (v.state === 'downloaded' ? ['youtube', 'frame-25', 'frame-50', 'frame-75'] : ['youtube']).map((id) => thumbVariant(v, id));
      if (v.thumb_upload) variants.push({ id: 'upload', label: 'Your upload', url: v.thumb_upload });
      return { current: v.thumb_choice || 'youtube', variants };
    }],
    ['POST', /^\/api\/videos\/([\w-]+)\/thumbnail$/, (m, b) => {
      const v = needV(m[1]);
      if (b && b.file_data_url) { v.thumb_upload = readFileBody(b); v.thumb_choice = 'upload'; }
      else if (b && b.variant) v.thumb_choice = b.variant === 'youtube' ? null : b.variant;
      else throw E(400, 'bad_request', 'Choose a thumbnail.');
      touchV(v);
      return videoDetail(v);
    }],
    ['POST', /^\/api\/videos\/([\w-]+)\/download$/, (m, b) => videoOut(videoDownload(needV(m[1]), b && b.next))],
    ['DELETE', /^\/api\/videos\/([\w-]+)$/, (m) => videoOut(videoDelete(needV(m[1])))],
    ['POST', /^\/api\/estimate$/, (m, b) => {
      b = b || {};
      if (b.channel_id) return estimateChannel(b);
      return estimateTarget(b.fill_target_bytes != null ? Number(b.fill_target_bytes) : target());
    }],
    ['GET', /^\/api\/activity$/, () => {
      const c = S.current, v = c && S.videos.get(c.vid);
      const failed = [];
      S.videos.forEach((x) => { if (x.state === 'failed') failed.push(x); });
      failed.sort((a, b) => (b.failed_at || 0) - (a.failed_at || 0));
      return {
        downloader: downloaderOut(),
        current: c ? { video: videoOut(v), stage: c.stage, stages: c.stages.slice(), progress: stageProgress(c), bytes_done: Math.round(c.bytes), bytes_total: c.total, speed_bps: c.stage === 'download' ? Math.round(c.speed) : null, eta_seconds: c.stage === 'download' ? Math.round((c.total - c.bytes) / c.speed) + 12 : null, resolution: effective(chOf(v)).quality, started_at: iso(c.started), sponsorblock_cut_seconds: c.stages.indexOf('sponsorblock') >= 0 && c.stages.indexOf(c.stage) > c.stages.indexOf('sponsorblock') ? c.cut : null } : null,
        queue: runOrder().map((q, i) => { const x = S.videos.get(q.vid); return { position: i + 1, video: videoOut(x), reason: q.reason, waits_for: waitsFor(q), estimated_bytes: x.est || null, added_at: iso(q.added_at) }; }),
        failed: failed.map((x) => ({ video: videoOut(x), error: x.error, failed_at: iso(x.failed_at || Date.now()) })),
        history: S.history.slice(0, 50).map(historyOut),
      };
    }],
    ['GET', /^\/api\/history$/, (m, b, qs) => {
      const before = qs.get('before') ? Date.parse(qs.get('before')) : Infinity, limit = Math.min(200, Number(qs.get('limit')) || 50);
      const list = S.history.filter((h) => h.at < before).slice(0, limit);
      return { history: list.map(historyOut), next_before: list.length === limit ? iso(list[list.length - 1].at) : null };
    }],
    ['POST', /^\/api\/downloader\/pause$/, () => {
      S.paused = true;
      if (S.current) { const v = S.videos.get(S.current.vid); v.state = 'queued'; v.est = S.current.total; S.queue.unshift({ vid: v.id, reason: 'new_upload', added_at: Date.now() }); S.current = null; touchV(v); }
      emit('status', status());
      return downloaderOut();
    }],
    ['POST', /^\/api\/downloader\/resume$/, () => { S.paused = false; startNext(); emit('status', status()); return downloaderOut(); }],
    ['POST', /^\/api\/downloader\/retry-failed$/, () => {
      let n = 0;
      S.videos.forEach((v) => { if (v.state === 'failed' && v.error && v.error.retryable) { videoDownload(v, true); n++; } });
      return { queued: n };
    }],
    ['GET', /^\/api\/storage$/, () => {
      const used = usedBytes(), prot = protections();
      let count = 0, wc = 0, wb = 0, sb = 0, kept = 0, bf = 0, pc = 0, pb = 0;
      S.videos.forEach((v) => { if (v.state === 'downloaded') { count++; sb += v.sponsorblock_cut_seconds || 0; if (v.keep) kept++; if (v.backfill) bf++; if (v.watched) { wc++; wb += sizeOf(v); } if (v.gone) { pc++; pb += sizeOf(v); } } });
      const by = S.channels.map((ch) => { const st = chStats(ch); return { channel_id: ch.id, title: chTitle(ch), size_bytes: st.size, video_count: st.count, poster_url: posterUrl(ch), retention_mode: effective(ch).retention.mode }; }).filter((x) => x.size_bytes > 0).sort((a, b) => b.size_bytes - a.size_bytes).slice(0, 12);
      const next = removalOrder(10).map((x) => ({ video: videoOut(x.v), size_bytes: sizeOf(x.v), reason: x.reason, watched_at: iso(x.v.watched_at) }));
      const recent = [];
      S.videos.forEach((v) => { if (v.state === 'removed' && REMOVE_BY_ROLL.has(v.removed_reason)) recent.push(v); });
      recent.sort((a, b) => b.removed_at - a.removed_at);
      const days = [], today = new Date(); today.setHours(0, 0, 0, 0);
      for (let i = 29; i >= 0; i--) { const d = new Date(today); d.setDate(d.getDate() - i); days.push({ date: dayOf(d.getTime()), new_videos: 0, backfill_videos: 0, removed_videos: 0, bytes: 0, removed_bytes: 0 }); }
      const idx = new Map(days.map((d, i) => [d.date, i]));
      S.videos.forEach((v) => {
        if (!LISTED(v)) return;
        if (v.downloaded_at) { const i = idx.get(dayOf(v.downloaded_at)); if (i != null) { if (v.backfill) days[i].backfill_videos++; else days[i].new_videos++; days[i].bytes += v.size_bytes || 0; } }
        if (v.state === 'removed' && v.removed_at && v.removed_reason !== 'manual') { const i = idx.get(dayOf(v.removed_at)); if (i != null) { days[i].removed_videos++; days[i].removed_bytes += v.size_bytes || 0; } }
      });
      const net7 = days.slice(-8, -1).reduce((s, d) => s + d.bytes - d.removed_bytes, 0) / 7;
      days.forEach((d) => delete d.removed_bytes);
      const fillState = fillOut();
      return {
        path: '/youtube', used_bytes: used, cap_bytes: CAP, target_bytes: target(), free_bytes: CAP - used, fill: fillState,
        video_count: count, backfill_count: bf, watched_count: wc, watched_bytes: wb, kept_count: kept, protected_count: prot.size, sponsorblock_saved_seconds: sb,
        preserved: { count: pc, bytes: pb },
        by_channel: by, next_to_remove: next,
        recently_removed: recent.slice(0, 10).map((v) => ({ video: videoOut(v), size_bytes: v.size_bytes || 0, reason: v.removed_reason, at: iso(v.removed_at) })),
        daily: days,
        projection: { avg_bytes_per_day: Math.round(net7), days_to_target: fillState.state === 'rolling' || net7 <= 0 ? null : Math.round((target() - used) / net7) },
      };
    }],
    ['POST', /^\/api\/import\/preview$/, (m, b) => {
      const found = parseImport(b && b.text, b && b.html);
      if (found.size < 1) throw E(400, 'bad_request', "Tubarr couldn't find any channels in that. Paste channel links or @handles (one per line), or use the subscriptions.csv file from Google Takeout.");
      const have = new Map(S.channels.map((c) => [c.handle.toLowerCase(), c]));
      const neu = []; let unchanged = 0;
      found.forEach((h, key) => { if (have.has(key)) unchanged++; else { const p = previewFor({ handle: h }).preview; neu.push({ handle: p.handle, title: p.title, subscribers: p.subscribers, channel_id: null }); } });
      const missing = S.channels.filter((c) => !found.has(c.handle.toLowerCase()) && c.status !== 'pending_removal' && !c.kept_while_unsubscribed && c.subscribed).map((c) => ({ channel_id: c.id, title: chTitle(c), handle: c.handle }));
      const id = 'imp_' + rid(T.rng(String(Date.now())), 6);
      S.imports[id] = { at: Date.now() };
      return { import_id: id, found: found.size, new: neu, missing, unchanged, unrecognized_lines: 0 };
    }],
    ['POST', /^\/api\/import\/apply$/, (m, b) => {
      if (!S.imports[b && b.import_id]) throw E(404, 'not_found', 'That preview expired. Paste the page again.');
      let added = 0, flagged = 0;
      (b.add || []).forEach((h) => { const p = previewFor({ handle: h }); if (p.preview) { addChannelFrom(p.preview, null); added++; } });
      (b.remove || []).forEach((id) => {
        const ch = S.byId.get(id);
        if (ch && ch.status !== 'pending_removal') { ch.status = 'pending_removal'; ch.subscribed = false; ch.removal = { reason: 'unsubscribed', requested_at: Date.now(), delete_at: Date.now() + 3 * DAY }; dropQueued(ch); touchCh(ch); flagged++; }
      });
      delete S.imports[b.import_id];
      return { added, flagged_for_removal: flagged, queued: true };
    }],
    ['GET', /^\/api\/plex$/, () => plexOut()],
    ['POST', /^\/api\/plex\/test$/, () => { S.plex.checked_at = Date.now(); return plexOut(); }],
    ['POST', /^\/api\/plex\/scan$/, () => {
      if (S.plex.state !== 'connected' || !S.plex.library) throw E(502, 'plex_unreachable', "Couldn't reach Plex.");
      S.plex.library.state = 'scanning'; S.plex.scanEnds = Date.now() + 6e3;
      return [202, { ok: true, message: 'Plex is scanning the YouTube library.' }];
    }],
    ['POST', /^\/api\/plex\/repolish$/, () => [202, { ok: true, message: 'Re-polishing all ' + S.channels.length + ' channels. This runs in the background and takes a while.' }]],
    ['GET', /^\/api\/settings$/, () => clone(S.settings)],
    ['PATCH', /^\/api\/settings$/, (m, b) => {
      b = b || {};
      if (b.downloads) {
        const d = b.downloads;
        if ('skip_shorts' in d && d.skip_shorts !== true) throw E(400, 'bad_request', 'Shorts are always skipped.');
        if (d.pacing && 'concurrency' in d.pacing && d.pacing.concurrency !== 1) throw E(400, 'bad_request', 'Tubarr downloads one video at a time.');
        if ('fill_target_bytes' in d && (d.fill_target_bytes > CAP || d.fill_target_bytes < 10 * GB)) throw E(400, 'bad_request', 'The fill target has to be between 10 GB and the space available.');
        if ('max_height' in d && ![2160, 1440, 1080, 720].includes(d.max_height)) throw E(400, 'bad_request', 'Best quality must be 2160, 1440, 1080 or 720.');
        if ('allow_av1' in d && typeof d.allow_av1 !== 'boolean') throw E(400, 'bad_request', 'allow_av1 must be true or false.');
        if ('max_height' in d) d.quality = d.max_height + 'p';
        if (d.human_pace) { delete d.human_pace.use_cookies; delete d.human_pace.cookies_present; }
        const before = S.channels.map((ch) => JSON.stringify(effective(ch)));
        const pacing = Object.assign({}, S.settings.downloads.pacing, d.pacing || {});
        Object.assign(S.settings.downloads, clone(d), { pacing });
        bump();
        S.channels.forEach((ch, i) => { if (JSON.stringify(effective(ch)) !== before[i]) applyRetention(ch); });
        if ('fill_target_bytes' in d) { while (usedBytes() > target() && rollOne(Date.now())) { /* shrink to the new target */ } touchStorage(); }
        emit('status', status());
      }
      if (b.notifications) {
        const n = b.notifications;
        const events = Object.assign({}, S.settings.notifications.events, n.events || {});
        Object.assign(S.settings.notifications, n, { events });
      }
      return clone(S.settings);
    }],
    /* ---- Sign-in (the mock is always signed in) ---- */
    ['GET', /^\/api\/auth\/state$/, () => authState()],
    ['POST', /^\/api\/auth\/(login|setup)$/, () => authState()],
    ['POST', /^\/api\/auth\/logout$/, () => ({ ok: true })],
    ['POST', /^\/api\/auth\/password$/, (m, b) => {
      if (!b || !b.current_password) throw E(403, 'forbidden', 'The current password isn’t right.');
      if (!b.new_password || String(b.new_password).length < 10) throw E(400, 'bad_request', 'The new password needs at least 10 characters.');
      audit('admin', 'auth.password_changed', 'Other sessions were signed out.');
      return { ok: true };
    }],
    ['GET', /^\/api\/auth\/apikey$/, () => apikeyOut()],
    ['POST', /^\/api\/auth\/apikey$/, () => {
      const key = 'tbk_' + rid(T.rng(String(Date.now())), 40);
      S.apikey = { created_at: Date.now(), hint: '…' + key.slice(-4) };
      audit('admin', 'auth.apikey_created', 'Key ' + S.apikey.hint);
      return Object.assign({ api_key: key }, apikeyOut());
    }],
    ['DELETE', /^\/api\/auth\/apikey$/, () => { S.apikey = null; audit('admin', 'auth.apikey_revoked', ''); return { ok: true }; }],
    ['GET', /^\/api\/audit$/, (m, b, qs) => ({ entries: S.audit.slice(0, Math.min(500, Number(qs.get('limit')) || 100)).map((e) => Object.assign({}, e, { at: iso(e.at) })) })],
    /* ---- First-run setup (Plex first) ---- */
    ['GET', /^\/api\/setup$/, () => ({ done: S.setup.done, plex: setupPlexOut(), channels: S.channels.length, subscriptions_pending: 0, media_path: '/youtube' })],
    ['POST', /^\/api\/setup\/plex\/pin$/, () => {
      const id = 4000 + Object.keys(S.setup.pins).length + 1;
      const code = rid(T.rng('pin' + id), 4).toUpperCase().replace(/[^A-Z0-9]/g, 'X');
      S.setup.pins[id] = { state: 'waiting', authAt: Date.now() + 6e3, expires: Date.now() + 15 * MIN };
      return { pin_id: id, code, auth_url: 'mock/plex-auth.html#code=' + code, link_url: 'https://plex.tv/link', expires_at: iso(Date.now() + 15 * MIN) };
    }],
    ['GET', /^\/api\/setup\/plex\/pin\/(\d+)$/, (m) => {
      const pin = S.setup.pins[m[1]];
      if (!pin) throw E(404, 'not_found', 'That sign-in code is unknown. Start again.');
      if (pin.state === 'waiting' && Date.now() > pin.expires) pin.state = 'expired';
      if (pin.state === 'authorized') S.setup.token_set = true;
      return { state: pin.state, servers: pin.state === 'authorized' ? MOCK_SERVERS : [] };
    }],
    ['POST', /^\/api\/setup\/plex$/, (m, b) => {
      const url = String((b && b.url) || '').trim();
      if (!/^https?:\/\/[^\s/]+/i.test(url)) throw E(400, 'bad_request', 'Enter the full address, like http://plex.local:32400.');
      if (b.token) S.setup.token_set = true;
      if (!S.setup.token_set) throw E(400, 'bad_request', 'Sign in with Plex first, or add the token.');
      Object.assign(S.setup, { configured: true });
      Object.assign(S.plex, { state: 'connected', url, server_name: 'Home Server', message: null, checked_at: Date.now() });
      audit('admin', 'plex.connected', 'Home Server at ' + url);
      return Object.assign(setupPlexOut(), { libraries: MOCK_LIBRARIES });
    }],
    ['GET', /^\/api\/setup\/plex\/libraries$/, () => {
      if (!S.setup.configured) throw E(409, 'conflict', 'Connect Plex first.');
      return { libraries: MOCK_LIBRARIES };
    }],
    ['POST', /^\/api\/setup\/plex\/library$/, (m, b) => {
      const lib = MOCK_LIBRARIES.find((l) => l.title === (b && b.title));
      if (!lib) throw E(404, 'not_found', 'Plex has no TV library called that.');
      if (!b.plex_root || !String(b.plex_root).trim()) throw E(400, 'bad_request', 'Enter the folder as Plex sees it.');
      Object.assign(S.setup, { library: lib.title, plex_root: String(b.plex_root).trim() });
      if (S.plex.library) Object.assign(S.plex.library, { name: lib.title, path: S.setup.plex_root });
      return setupPlexOut();
    }],
    ['POST', /^\/api\/setup\/plex\/clear$/, () => {
      Object.assign(S.setup, { configured: false, token_set: false, library: null, plex_root: null });
      audit('admin', 'plex.cleared', '');
      return setupPlexOut();
    }],
    ['POST', /^\/api\/setup\/complete$/, () => { S.setup.done = true; audit('admin', 'setup.complete', ''); return { done: true }; }],
    /* ---- Network: direct, or the user's own proxies ---- */
    ['GET', /^\/api\/network$/, () => netOut()],
    ['PATCH', /^\/api\/network$/, (m, b) => {
      b = b || {};
      const N = S.network, ids = N.proxies.map((p) => p.id);
      if ('mode' in b && !['direct', 'single', 'failover', 'rotate'].includes(b.mode)) throw E(400, 'bad_request', 'Unknown mode.');
      if ('selected' in b && b.selected != null && !ids.includes(b.selected)) throw E(400, 'bad_request', 'No proxy with that id.');
      const mode = b.mode || N.mode, selected = 'selected' in b ? b.selected : N.selected;
      if (mode === 'single' && !selected) throw E(400, 'bad_request', 'Pick which proxy to use.');
      if ('order' in b) {
        const o = b.order;
        if (!Array.isArray(o) || o.length !== ids.length + 1 || !o.includes('direct') || !ids.every((id) => o.includes(id))) throw E(400, 'bad_request', 'The order must list Direct and every proxy once.');
        N.order = o.slice();
      }
      const range = (k, lo, hi) => { if (k in b) { const v = Number(b[k]); if (!isFinite(v) || v < lo || v > hi) throw E(400, 'bad_request', k + ' must be between ' + lo + ' and ' + hi + '.'); N[k] = v; } };
      range('fail_threshold', 1, 20); range('cooldown_minutes', 5, 10080); range('rotate_hours', 0.5, 168);
      if ('check_preferred' in b) N.check_preferred = !!b.check_preferred;
      if ('never_direct' in b) { N.never_direct = !!b.never_direct; N.never_direct_auto = false; }
      const modeChanged = mode !== N.mode;
      N.mode = mode; N.selected = selected;
      if (modeChanged || 'selected' in b || 'order' in b) netActivate(modeChanged ? 'Mode changed to ' + mode : 'Settings changed');
      audit('admin', modeChanged ? 'network.mode' : 'network.settings', modeChanged ? 'Mode: ' + mode : Object.keys(b).join(', '));
      return netOut();
    }],
    ['POST', /^\/api\/network\/proxies$/, (m, b) => {
      const name = String((b && b.name) || '').trim(), url = String((b && b.url) || '').trim();
      if (!name) throw E(400, 'bad_request', 'Give the proxy a name.');
      const masked = maskProxy(url);
      const id = 'p_' + rid(T.rng(name + Date.now()), 6).toLowerCase();
      S.network.proxies.push({ id, name, masked, scheme: url.split(':')[0].toLowerCase(), url });
      S.network.order.push(id);
      if (S.network.never_direct_auto) S.network.never_direct = true;
      audit('admin', 'network.proxy_added', name);
      return netOut();
    }],
    ['PATCH', /^\/api\/network\/proxies\/([\w-]+)$/, (m, b) => {
      const p = needProxy(m[1]); b = b || {};
      if ('name' in b) { const n = String(b.name || '').trim(); if (!n) throw E(400, 'bad_request', 'Give the proxy a name.'); p.name = n; }
      if (b.url) { p.masked = maskProxy(b.url); p.url = b.url; p.scheme = b.url.split(':')[0].toLowerCase(); }
      if (S.network.status.active === p.id) S.network.status.active_name = p.name;
      audit('admin', 'network.proxy_updated', p.name);
      return netOut();
    }],
    ['DELETE', /^\/api\/network\/proxies\/([\w-]+)$/, (m) => {
      const p = needProxy(m[1]), N = S.network;
      N.proxies = N.proxies.filter((x) => x !== p); N.order = N.order.filter((id) => id !== p.id);
      if (N.selected === p.id) { N.selected = null; if (N.mode === 'single') N.mode = N.proxies.length ? 'failover' : 'direct'; }
      if (!N.proxies.length) { N.mode = 'direct'; if (N.never_direct_auto) N.never_direct = false; }
      if (N.status.active === p.id) netActivate('The proxy in use was deleted');
      audit('admin', 'network.proxy_deleted', p.name);
      return netOut();
    }],
    ['POST', /^\/api\/network\/proxies\/([\w-]+)\/test$/, (m) => {
      const now = Date.now();
      S.netTests = S.netTests.filter((t) => now - t < 60e3);
      if (S.netTests.length >= 5) throw E(429, 'rate_limited', 'Too many tests. Wait a minute.');
      S.netTests.push(now);
      if (m[1] === 'direct') return { ok: true, ip: '203.0.113.5', latency_ms: 40 + Math.round(Math.random() * 60) };
      const p = needProxy(m[1]);
      if (/fail|down/i.test(p.masked) || SCENARIO === 'trouble') throw E(502, 'proxy_failed', 'The proxy didn’t answer.');
      return { ok: true, ip: proxyIp(p), latency_ms: 150 + Math.round(Math.random() * 200) };
    }],
    /* ---- Trimarr (optional add-on), as proxied by Tubarr under /api/trimarr/ ---- */
    ['GET', /^\/api\/trimarr\/status$/, () => trimStatus()],
    ['PATCH', /^\/api\/trimarr\/settings$/, (m, b) => { S.trimarr.enabled = !!(b && b.enabled); return trimStatus(); }],
    ['GET', /^\/api\/trimarr\/channels\/([\w-]+)$/, (m) => { needCh(m[1]); return { channel_id: m[1], enabled: S.trimarr.channels.has(m[1]) }; }],
    ['PATCH', /^\/api\/trimarr\/channels\/([\w-]+)$/, (m, b) => { needCh(m[1]); if (b && b.enabled) S.trimarr.channels.add(m[1]); else S.trimarr.channels.delete(m[1]); return { channel_id: m[1], enabled: S.trimarr.channels.has(m[1]) }; }],
    ['POST', /^\/api\/trimarr\/videos\/([\w-]+)\/trim$/, (m) => {
      const v = needV(m[1]);
      if (v.state !== 'downloaded') throw E(409, 'conflict', 'Only videos in Plex can be trimmed.');
      if (v.sponsorblock_cut_seconds > 0 && !v.trim) throw E(409, 'conflict', 'Tubarr already cut the sponsors out of this one.');
      const r = T.rng('trim:' + v.id);
      if (r() < 0.12) throw E(404, 'not_found', 'SponsorBlock has no segments for this video yet.');
      v.trim = { state: 'trimmed', removed_seconds: Math.round(between(r, 25, 140)) };
      S.trimarr.last_run_at = Date.now();
      touchV(v);
      return { video_id: v.id, state: 'trimmed', removed_seconds: v.trim.removed_seconds };
    }],
    ['POST', /^\/api\/trimarr\/videos\/([\w-]+)\/undo$/, (m) => { const v = needV(m[1]); if (!v.trim) throw E(409, 'conflict', 'That video isn’t trimmed.'); v.trim = null; touchV(v); return { video_id: v.id, state: 'original', removed_seconds: 0 }; }],
    ['GET', /^\/api\/trimarr\/report$/, (m, b, qs) => {
      const only = qs.get('channel');
      const chs = only ? [needCh(only)] : (S.trimarr.channels.size ? S.channels.filter((c) => S.trimarr.channels.has(c.id)) : S.channels);
      let videos = 0, seconds = 0, bytes = 0;
      chs.forEach((ch) => ch.catalog.forEach((v) => {
        if (v.state !== 'downloaded' || v.sponsorblock_cut_seconds > 0 || v.trim) return;
        const r = T.rng('seg:' + v.id);
        if (r() < 0.45) { const sec = Math.round(between(r, 25, 140)); videos++; seconds += sec; bytes += Math.round(sec * bpsFor(ch, effective(ch).quality)); }
      }));
      return { videos, seconds, bytes_saved: bytes, channels: chs.length, dry_run: true };
    }],
    ['POST', /^\/api\/notifications\/test$/, (m, b) => {
      const url = (b && b.discord_webhook_url != null ? b.discord_webhook_url : S.settings.notifications.discord_webhook_url) || '';
      if (!/^https:\/\/(discord|discordapp)\.com\/api\/webhooks\//.test(url)) throw E(400, 'bad_request', "That doesn't look like a Discord webhook URL.");
      return { ok: true };
    }],
  ];

  build();
  setInterval(tick, 500);

  T.mock = {
    scenario: SCENARIO,
    handle(method, url, body) {
      tick();
      const u = new URL(url, 'http://tubarr.local');
      for (const [m, re, fn] of handlers) {
        if (m !== method) continue;
        const match = u.pathname.match(re);
        if (!match) continue;
        try {
          const res = fn(match, body ? clone(body) : null, u.searchParams);
          return Array.isArray(res) ? { status: res[0], body: clone(res[1]) } : { status: 200, body: clone(res) };
        } catch (e) {
          if (e instanceof HttpErr) return { status: e.status, body: { error: { code: e.code, message: e.message } } };
          console.error(e);
          return { status: 500, body: { error: { code: 'internal', message: 'The mock hit a bug: ' + e.message } } };
        }
      }
      return { status: 404, body: { error: { code: 'not_found', message: 'No such endpoint: ' + method + ' ' + u.pathname } } };
    },
    /* The in-page stand-in for the SSE stream. Returns an unsubscribe function. */
    subscribe(fn) {
      subs.add(fn);
      setTimeout(() => { fn({ id: evSeq++, event: 'hello', data: { version: '0.1.0-preview', server_time: iso(Date.now()) } }); fn({ id: evSeq++, event: 'status', data: status() }); }, 0);
      return () => subs.delete(fn);
    },
    /* Text for the Import card's "Paste a sample" button (mock only): the current list minus two, plus three new. */
    sampleImportText() {
      const drop = new Set(['@noodlenightmarket', '@hedgerowandhive']);
      const lines = [];
      S.channels.filter((c) => c.subscribed && !drop.has(c.handle.toLowerCase())).forEach((c) => lines.push(chTitle(c), c.handle + ' \u2022 ' + T.fmtCompact(c.subscribers) + ' subscribers', 'Subscribed', ''));
      [['Harbor Lights Vlog', '@HarborLightsVlog', 1210000], ['Clockwork Curiosities', '@ClockworkCuriosities', 2730000], ['Copper Pot Cooking', '@CopperPotCooking', 640000]]
        .forEach(([t, h, s]) => lines.push(t, h + ' \u2022 ' + T.fmtCompact(s) + ' subscribers', 'Subscribed', ''));
      return lines.join('\n');
    },
  };
})();
