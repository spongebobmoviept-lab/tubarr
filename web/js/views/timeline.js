/* Timeline (home): every video across all channels, newest first, grouped by day, live. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon, ui = T.ui;

  const GROUPS = [
    ['all', 'All', null],
    ['plex', 'In Plex', ['downloaded']],
    ['progress', 'In progress', ['downloading', 'queued']],
    ['waiting', 'Waiting', ['waiting_sponsorblock', 'upcoming']],
    ['failed', 'Failed', ['failed']],
    ['skipped', 'Skipped', ['skipped_short', 'skipped_live', 'skipped_too_long', 'skipped_members']],
    ['removed', 'Removed', ['removed']],
    ['kept', 'Kept forever', null],
    ['gone', 'Gone from YouTube', null],
  ];
  const G = Object.fromEntries(GROUPS.map((g) => [g[0], g]));
  const RANGES = [['any', 'Any time'], ['today', 'Today'], ['7d', 'Last 7 days'], ['30d', 'Last 30 days'], ['custom', 'Custom dates…']];
  const PAGE = 60;
  const todayStr = () => { const d = new Date(); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
  const shift = (days) => { const d = new Date(); d.setDate(d.getDate() - days); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
  const localDay = (iso) => { if (!iso) return null; const d = new Date(iso); return d.getFullYear() + '-' + String(d.getMonth() + 1).padStart(2, '0') + '-' + String(d.getDate()).padStart(2, '0'); };
  const WEEKDAY = ['Sunday', 'Monday', 'Tuesday', 'Wednesday', 'Thursday', 'Friday', 'Saturday'];

  const V = (T.views.timeline = { nav: 'timeline', title: 'Timeline' });
  let root, f, list, next, total, loading, unsubs = [], channels = [], io = null, seq = 0;

  const KINDS = [['all', 'New and backlog'], ['new', 'New uploads only'], ['backlog', 'Backlog only']];
  function readFilters(q) {
    return {
      q: q.get('q') || '', group: G[q.get('group')] ? q.get('group') : 'all', channel: q.get('channel') || '',
      range: RANGES.some((r) => r[0] === q.get('range')) ? q.get('range') : 'any', from: q.get('from') || '', to: q.get('to') || '',
      sort: q.get('sort') === 'published' ? 'published' : 'activity',
      kind: KINDS.some((k) => k[0] === q.get('kind')) ? q.get('kind') : 'all',
    };
  }
  function syncHash() {
    const q = new URLSearchParams();
    if (f.q) q.set('q', f.q);
    if (f.group !== 'all') q.set('group', f.group);
    if (f.channel) q.set('channel', f.channel);
    if (f.range !== 'any') q.set('range', f.range);
    if (f.range === 'custom') { if (f.from) q.set('from', f.from); if (f.to) q.set('to', f.to); }
    if (f.sort !== 'activity') q.set('sort', f.sort);
    if (f.kind !== 'all') q.set('kind', f.kind);
    const s = q.toString();
    history.replaceState(null, '', '#/timeline' + (s ? '?' + s : ''));
  }
  function bounds() {
    if (f.range === 'today') return [todayStr(), ''];
    if (f.range === '7d') return [shift(6), ''];
    if (f.range === '30d') return [shift(29), ''];
    if (f.range === 'custom') return [f.from, f.to];
    return ['', ''];
  }
  function query(cursor) {
    const q = new URLSearchParams({ limit: String(PAGE), sort: f.sort });
    const g = G[f.group];
    if (g[2]) q.set('state', g[2].join(','));
    if (f.group === 'kept') q.set('kept', '1');
    if (f.group === 'gone') q.set('gone', '1');
    if (f.channel) q.set('channel', f.channel);
    if (f.q) q.set('q', f.q);
    if (f.kind !== 'all') q.set('backfill', f.kind === 'backlog' ? '1' : '0');
    const [from, to] = bounds();
    if (from) q.set('from', from);
    if (to) q.set('to', to);
    if (cursor) q.set('cursor', cursor);
    return '/api/timeline?' + q.toString();
  }
  const keyOf = (v) => Date.parse(f.sort === 'activity' ? v.activity_at || v.published_at : v.published_at);
  const dayOf = (v) => (v.state === 'upcoming' ? 'upcoming' : f.sort === 'activity' ? localDay(v.activity_at || v.published_at) : v.upload_date);
  function matches(v) {
    const g = G[f.group];
    if (g[2] && !g[2].includes(v.state)) return false;
    if (f.group === 'kept' && !v.keep) return false;
    if (f.group === 'gone' && !v.youtube_gone) return false;
    if (f.kind === 'backlog' && !v.backfill) return false;
    if (f.kind === 'new' && v.backfill) return false;
    if (f.channel && v.channel_id !== f.channel) return false;
    if (f.q && (v.title + ' ' + v.channel_title).toLowerCase().indexOf(f.q.toLowerCase()) < 0) return false;
    const [from, to] = bounds(), d = dayOf(v);
    if (d !== 'upcoming' && ((from && d < from) || (to && d > to))) return false;
    return true;
  }

  function dayLabel(day) {
    if (day === 'upcoming') return ['Coming up', 'Premieres and scheduled streams'];
    const t = todayStr(), y = shift(1);
    const d = T.parseDay(day);
    const full = WEEKDAY[d.getDay()] + ', ' + T.fmtDay(day, d.getFullYear() !== new Date().getFullYear());
    if (day === t) return ['Today', full];
    if (day === y) return ['Yesterday', full];
    const age = (T.parseDay(t) - d) / 864e5;
    if (age < 7) return [WEEKDAY[d.getDay()], T.fmtDay(day, false)];
    return [T.fmtDay(day, d.getFullYear() !== new Date().getFullYear()), WEEKDAY[d.getDay()]];
  }
  function daySummary(day) {
    const rows = list.filter((x) => dayOf(x) === day);
    const partial = !!next && list.length && dayOf(list[list.length - 1]) === day;
    const parts = [T.fmtNum(rows.length) + (partial ? '+' : '') + (rows.length === 1 && !partial ? ' video' : ' videos')];
    const nNew = rows.filter((v) => !v.backfill && v.state === 'downloaded').length;
    const nBack = rows.filter((v) => v.backfill && v.state === 'downloaded').length;
    const nRem = rows.filter((v) => v.state === 'removed').length;
    if (nNew) parts.push(nNew + ' new in Plex');
    if (nBack) parts.push(T.fmtNum(nBack) + ' backlog');
    if (nRem) parts.push(nRem + ' removed');
    return parts.join(' · ');
  }
  function renderDay(day, rows) {
    const [label, sub] = dayLabel(day);
    return html`<section class="day" data-day="${day}">
      <header class="day-head"><h2>${label}</h2><span class="day-sub">${sub}</span><span class="day-sum" data-day-sum>${daySummary(day)}</span></header>
      <div class="day-rows">${rows.map((v) => rowHtml(v))}</div>
    </section>`;
  }
  const rowHtml = (v, fresh) => ui.videoRow(v, { fresh, when: f.sort === 'activity' ? 'activity' : 'time', selected: T.bulk.sel.has(v.id) });
  function renderList() {
    const box = root.querySelector('[data-slot="list"]');
    if (!list.length) {
      const filtered = f.q || f.group !== 'all' || f.channel || f.range !== 'any';
      T.setHTML(box, filtered
        ? ui.empty({ icon: 'search', title: 'No videos match these filters', body: 'Try another status or date range, or clear the filters.', actions: html`<button class="btn" type="button" data-act="clear-filters">${icon('x')}Clear filters</button>` })
        : ui.empty({ art: html`<span class="empty-mark">${T.logoMark()}</span>`, title: 'Your timeline is empty', body: 'Once Tubarr knows your channels, every new upload shows up here the moment it’s found, and moves along as it downloads.',
          actions: html`<a class="btn btn-primary" href="#/settings/import">${icon('clipboard')}Import your subscriptions</a><button class="btn" type="button" data-act="add-channel">${icon('plus')}Add a channel</button>` }));
      renderMore();
      return;
    }
    const days = [], by = {};
    list.forEach((v) => { const d = dayOf(v); if (!by[d]) { by[d] = []; days.push(d); } by[d].push(v); });
    const up = days.indexOf('upcoming'); if (up > 0) { days.splice(up, 1); days.unshift('upcoming'); }
    T.setHTML(box, days.map((d) => renderDay(d, by[d])));
    renderMore();
  }
  function renderMore() {
    const m = root.querySelector('[data-slot="more"]');
    const sum = root.querySelector('[data-slot="summary"]');
    if (sum) T.setHTML(sum, list.length ? html`<span class="live-dot" aria-hidden="true"></span>Live · showing ${T.fmtNum(list.length)} of ${T.fmtNum(total)}` : '');
    if (!m) return;
    T.setHTML(m, next ? html`<button class="btn" type="button" data-act="more" ${loading ? T.raw('disabled') : ''}>${loading ? html`<span class="spin"></span>Loading…` : 'Load more'}</button>` : list.length > 12 ? html`<span class="tl-end">That's everything${f.group !== 'all' || f.q || f.channel || f.range !== 'any' ? ' that matches' : ''}.</span>` : '');
  }

  async function load(reset) {
    const my = ++seq;
    loading = true;
    if (reset) { list = []; next = null; root.querySelector('[data-slot="list"]').classList.add('is-refetch'); }
    renderMore();
    try {
      const r = await T.api.get(query(reset ? null : next));
      if (my !== seq) return;
      T.cacheVideos(r.videos);
      list = reset ? r.videos : list.concat(r.videos);
      next = r.next; total = r.total;
      loading = false;
      renderList();
    } catch (e) {
      if (my !== seq) return;
      loading = false;
      T.setHTML(root.querySelector('[data-slot="list"]'), ui.errorState(e, 'retry-list'));
    }
    root.querySelector('[data-slot="list"]').classList.remove('is-refetch');
  }

  /* ---------- Live ---------- */
  function insert(v) {
    if (!matches(v)) return;
    const k = keyOf(v);
    if (next && list.length && k < keyOf(list[list.length - 1])) return; // older than what's loaded: comes with paging
    if (list.some((x) => x.id === v.id)) return;
    let i = list.findIndex((x) => keyOf(x) < k);
    if (i < 0) i = list.length;
    list.splice(i, 0, v);
    total++;
    const box = root.querySelector('[data-slot="list"]');
    if (list.length === 1) { renderList(); return; }
    const day = dayOf(v);
    let sec = box.querySelector(`.day[data-day="${CSS.escape(day)}"]`);
    const tmp = document.createElement('div');
    if (!sec) {
      T.setHTML(tmp, renderDay(day, [v]));
      sec = tmp.firstElementChild;
      sec.querySelector('.vrow').classList.add('is-new');
      const secs = Array.from(box.querySelectorAll('.day'));
      const before = secs.find((s) => s.dataset.day !== 'upcoming' && (day === 'upcoming' || s.dataset.day < day));
      box.insertBefore(sec, before || null);
    } else {
      T.setHTML(tmp, rowHtml(v, true));
      const row = tmp.firstElementChild;
      const rows = Array.from(sec.querySelectorAll('.vrow'));
      const after = rows.find((r) => { const x = T.vcache.get(r.dataset.vid); return x && keyOf(x) < k; });
      sec.querySelector('.day-rows').insertBefore(row, after || null);
      const s = sec.querySelector('[data-day-sum]');
      if (s) s.textContent = daySummary(day);
    }
    renderMore();
  }
  function onState(v) {
    const i = list.findIndex((x) => x.id === v.id);
    if (i < 0) { insert(v); return; }
    if (!matches(v)) {
      list.splice(i, 1); total = Math.max(0, total - 1);
      T.$$(`.vrow[data-vid="${CSS.escape(v.id)}"]`, root).forEach((r) => { r.classList.add('is-leaving'); setTimeout(() => { const sec = r.closest('.day'); r.remove(); if (sec && !sec.querySelector('.vrow')) sec.remove(); }, 320); });
      renderMore();
      return;
    }
    // a new milestone (e.g. a backlog video just landed, or the roll removed it) moves the row to its new place
    if (keyOf(list[i]) !== keyOf(v) || dayOf(list[i]) !== dayOf(v)) {
      list.splice(i, 1); total = Math.max(0, total - 1);
      T.$$(`.vrow[data-vid="${CSS.escape(v.id)}"]`, root).forEach((r) => { const sec = r.closest('.day'); r.remove(); if (sec && !sec.querySelector('.vrow')) sec.remove(); });
      insert(v);
      return;
    }
    list[i] = v;
    ui.patchRows(v, { when: f.sort === 'activity' ? 'activity' : 'time' });
    const sec = root.querySelector(`.day[data-day="${CSS.escape(dayOf(v))}"] [data-day-sum]`);
    if (sec) sec.textContent = daySummary(dayOf(v));
  }
  function onRemoved(d) {
    const i = list.findIndex((x) => x.id === d.id);
    if (i < 0) return;
    list.splice(i, 1); total = Math.max(0, total - 1);
    T.$$(`.vrow[data-vid="${CSS.escape(d.id)}"]`, root).forEach((r) => r.remove());
    renderMore();
  }

  /* ---------- Dashboard strip (live from status) ---------- */
  let dashKey = '';
  function dash(s) {
    const el = root && root.querySelector('[data-slot="dash"]');
    if (!el || !s) return;
    const st = s.storage, d = s.downloader, fl = st.fill || { state: 'filling' };
    const cnt = s.counts || {};
    const jobs = T.dlJobs(d), c = jobs.length === 1 ? jobs[0] : null, many = jobs.length > 1;
    const lead = many ? jobs.filter((j) => j.stage === 'download').sort((a, b) => (b.new_upload - a.new_upload) || ((b.progress || 0) - (a.progress || 0))).slice(0, 3) : [];
    const ratio = st.used_bytes / st.cap_bytes, tgt = st.target_bytes / st.cap_bytes, toTarget = Math.min(1, st.used_bytes / st.target_bytes);
    const key = [many ? 'many:' + lead.map((j) => j.video_id).join(',') : c ? c.video_id + c.stage : d.state + ((d.pacing || {}).next_download_at || ''), cnt.channels, cnt.channel_errors, fl.state].join('|');
    const q = (k) => el.querySelector(`[data-dash="${k}"]`);
    const today = s.today || { added: 0, bytes: 0, backfill: 0, removed: 0 };
    const todaySub = () => [today.backfill ? T.fmtNum(today.backfill) + ' from the backlog' : '', today.removed ? today.removed + ' rolled out' : ''].filter(Boolean).join(' · ') || (today.added ? T.fmtBytes(today.bytes) + ' added' : 'Nothing new yet today');
    if (key === dashKey) {
      if (q('store-used')) q('store-used').textContent = T.fmtBytes(st.used_bytes, { digits: 2 });
      if (q('store-bar')) q('store-bar').style.width = (Math.min(1, ratio) * 100).toFixed(2) + '%';
      if (q('store-sub')) T.setHTML(q('store-sub'), T.fillLine(st, true));
      if (c && q('now-pct')) q('now-pct').textContent = c.stage === 'download' ? Math.floor((c.progress || 0) * 100) + '%' : ui.STAGES[c.stage];
      if (c && q('now-speed')) q('now-speed').textContent = c.speed_bps ? T.fmtSpeed(c.speed_bps) + (c.eta_seconds != null ? ' · ' + T.fmtSpan(c.eta_seconds) + ' left' : '') : 'Finishing up';
      if (c && q('now-bar') && c.stage === 'download') q('now-bar').style.width = ((c.progress || 0) * 100).toFixed(2) + '%';
      if (many) {
        if (q('many-n')) q('many-n').textContent = T.fmtSpeed(d.speed_bps || 0);
        lead.forEach((j, i) => { const b = q('job-' + i); if (b) b.style.width = ((j.progress || 0) * 100).toFixed(2) + '%'; const t = q('jobp-' + i); if (t) t.textContent = Math.floor((j.progress || 0) * 100) + '%'; });
      }
      if (q('today-n')) q('today-n').textContent = '+' + today.added;
      if (q('today-b')) q('today-b').textContent = todaySub();
      if (q('feed')) T.setHTML(q('feed'), feedLine(s));
      if (q('queue-n')) q('queue-n').textContent = d.queue_count ? T.fmtNum(d.queue_count) + ' queued' : (many ? 'Queue is empty' : '');
      return;
    }
    dashKey = key;
    T.setHTML(el, html`
      <a class="dash-tile dash-store is-${fl.state}" href="#/storage">
        <span class="dash-label">${icon('storage')}Storage<span class="fill-pill is-${fl.state}">${fl.state === 'rolling' ? 'Full · rolling' : 'Filling'}</span></span>
        <span class="dash-fig"><b data-dash="store-used">${T.fmtBytes(st.used_bytes, { digits: 2 })}</b><span class="dash-of">of ${T.fmtBytes(st.target_bytes, { digits: 1 })} target</span></span>
        <span class="meter-sm"><i data-dash="store-bar" style="width:${(Math.min(1, ratio) * 100).toFixed(2)}%"></i><em style="left:${(tgt * 100).toFixed(1)}%"></em></span>
        <span class="dash-sub" data-dash="store-sub">${T.fillLine(st, true)}</span>
      </a>
      <a class="dash-tile dash-now ${c || many ? 'is-live' : ''}" href="#/activity">
        <span class="dash-label">${c || many ? html`<span class="tally"></span>` : icon('download')}${many ? jobs.length + ' downloading at once' : c ? 'Now downloading' : d.state === 'paused' ? 'Downloads paused' : d.state === 'stopped' ? 'Downloader stopped' : d.state === 'waiting' ? T.dlWaitHtml(d) : 'Downloads'}</span>
        ${many ? html`<span class="dash-fig"><b data-dash="many-n">${T.fmtSpeed(d.speed_bps || 0)}</b><span class="dash-of">${d.concurrency ? 'self-tuning · ' + d.concurrency + ' slots now' : 'combined'}</span></span>
          <span class="dash-jobs">${lead.map((j, i) => html`<span class="dash-job"><span class="dash-job-t">${j.new_upload ? html`<span class="new-mark">NEW</span>` : ''}${j.title}</span><b class="num" data-dash="jobp-${i}">${Math.floor((j.progress || 0) * 100)}%</b><span class="bar bar-xs"><i data-dash="job-${i}" style="width:${((j.progress || 0) * 100).toFixed(2)}%"></i></span></span>`)}</span>
          <span class="dash-sub"><span class="dash-q" data-dash="queue-n">${d.queue_count ? T.fmtNum(d.queue_count) + ' queued' : 'Queue is empty'}</span>${d.failed_count ? html` · <span class="t-crit">${d.failed_count} failed</span>` : ''}</span>`
        : c ? html`<span class="dash-now-row">${T.thumb({ id: c.video_id, title: c.title, thumbnail_url: c.thumbnail_url }, 'thumb-xs')}<span class="dash-now-text"><b class="dash-now-title">${c.title}</b><span class="dash-sub">${c.channel_title} · <b data-dash="now-pct">${c.stage === 'download' ? Math.floor((c.progress || 0) * 100) + '%' : ui.STAGES[c.stage]}</b></span></span></span>
          <span class="bar bar-sm ${c.stage === 'download' ? '' : 'is-ind'}"><i data-dash="now-bar" ${c && c.video_id ? T.raw('data-pbar="' + T.esc(c.video_id) + '"') : ''} style="width:${c.stage === 'download' ? ((c.progress || 0) * 100).toFixed(2) : 0}%"></i></span>
          <span class="dash-sub"><span data-dash="now-speed">${c.speed_bps ? T.fmtSpeed(c.speed_bps) + (c.eta_seconds != null ? ' · ' + T.fmtSpan(c.eta_seconds) + ' left' : '') : 'Finishing up'}</span> <span class="dash-q" data-dash="queue-n">${d.queue_count ? d.queue_count + ' queued' : ''}</span></span>`
        : html`<span class="dash-fig"><b>${d.state === 'paused' ? 'Paused' : d.state === 'stopped' ? 'Stopped' : d.state === 'waiting' ? (d.overnight && d.overnight.start ? T.fmtHHMM(d.overnight.start) : 'Waiting') : 'Idle'}</b></span><span class="dash-sub">${d.queue_count ? T.plural(d.queue_count, 'video') + ' queued' : 'Queue is empty'}${d.failed_count ? html` · <span class="t-crit">${d.failed_count} failed</span>` : ''}</span>`}
      </a>
      <a class="dash-tile dash-today" href="#/timeline?range=today">
        <span class="dash-label">${icon('sparkles')}Today</span>
        <span class="dash-fig"><b data-dash="today-n">+${today.added}</b><span class="dash-of">new uploads in Plex</span></span>
        <span class="dash-sub" data-dash="today-b">${todaySub()}</span>
        <span class="dash-feed" data-dash="feed">${feedLine(s)}</span>
      </a>
      <a class="dash-tile dash-sync" href="#/channels">
        <span class="dash-label">${icon('tv')}Channels</span>
        <span class="dash-fig"><b>${T.fmtNum(cnt.channels || 0)}</b><span class="dash-of">followed</span></span>
        <span class="dash-sub">${cnt.channel_errors ? html`<span class="t-crit">${T.plural(cnt.channel_errors, 'channel')} need a look</span>` : cnt.pending_removal ? T.plural(cnt.pending_removal, 'channel') + ' leaving soon' : cnt.channels ? 'All being checked for new uploads' : html`<span>Import your subscriptions in Settings</span>`}</span>
      </a>`);
    void toTarget;
  }
  function feedLine(s) {
    const f = s.feed;
    if (!f) return '';
    return html`${icon('refresh')}<span>Checks every ${f.interval_minutes} min · last ${T.fmtTime(f.last_check_at)}${f.typical_minutes_to_plex != null ? html` · in Plex ~${f.typical_minutes_to_plex} min after upload` : ''}</span>`;
  }

  /* ---------- View lifecycle ---------- */
  V.enter = async function (el, route) {
    root = el; f = readFilters(route.query); list = []; next = null; total = 0; dashKey = '';
    T.setHTML(root, html`<div class="page page-timeline">
      <header class="page-head">
        <div class="page-title"><h1>Timeline</h1><p class="page-sub">Everything happening to your library, newest first: new uploads, backlog filling in, and what rolled out. Live.</p></div>
        <div class="page-actions">
          <button class="btn btn-ghost select-toggle" type="button" data-act="select-mode">${icon('check-circle')}Select</button>
          <button class="btn" type="button" data-act="refresh-all">${icon('refresh')}<span class="hide-sm">Check all channels</span></button>
          <button class="btn btn-primary" type="button" data-act="add-channel">${icon('plus')}<span class="hide-sm">Add channel</span></button>
        </div>
      </header>
      <section class="dash" data-slot="dash" aria-label="At a glance"></section>
      <div class="toolbar tl-toolbar">
        <label class="search-field">${icon('search')}<input type="search" data-f="q" value="${f.q}" placeholder="Search titles and channels" aria-label="Search the timeline"></label>
        <div class="chips" role="group" aria-label="Status">${GROUPS.map(([k, label]) => html`<button class="chip ${f.group === k ? 'active' : ''}" type="button" data-group="${k}" aria-pressed="${f.group === k}">${label}</button>`)}</div>
        <div class="toolbar-right">
          ${ui.select('channel', [['', 'All channels']], f.channel, { label: 'Channel', attrs: 'data-f="channel"', cls: 'select-ch' })}
          ${ui.select('range', RANGES, f.range, { label: 'Date', attrs: 'data-f="range"' })}
          <span class="range-custom" ${f.range === 'custom' ? '' : T.raw('hidden')}><input class="input input-date" type="date" data-f="from" value="${f.from}" aria-label="From"><span>to</span><input class="input input-date" type="date" data-f="to" value="${f.to}" aria-label="To"></span>
          ${ui.select('kind', KINDS, f.kind, { label: 'New or backlog', attrs: 'data-f="kind"' })}
          ${ui.select('sort', [['activity', 'Latest activity'], ['published', 'Upload date']], f.sort, { label: 'Order', attrs: 'data-f="sort"' })}
        </div>
      </div>
      <div class="tl-summary" data-slot="summary"></div>
      <div class="tl-list" data-slot="list"><div class="skel-rows">${Array.from({ length: 6 }, () => html`<div class="vrow skel-row"><span class="thumb skel"></span><span class="skel-lines"><span class="skel skel-line"></span><span class="skel skel-line short"></span></span></div>`)}</div></div>
      <div class="tl-more" data-slot="more"></div>
    </div>`);
    dash(T.live.status);
    // channel filter options
    T.api.get('/api/channels').then((r) => {
      channels = r.channels.slice().sort((a, b) => a.title.localeCompare(b.title));
      const sel = root.querySelector('[data-f="channel"]');
      if (sel) T.setHTML(sel, html`<option value="">All channels</option>${channels.map((c) => html`<option value="${c.id}" ${c.id === f.channel ? T.raw('selected') : ''}>${c.title}</option>`)}`);
    }).catch(() => {});
    // filters
    const qInput = root.querySelector('[data-f="q"]');
    qInput.addEventListener('input', T.debounce(() => { f.q = qInput.value.trim(); syncHash(); load(true); }, 250));
    root.querySelector('.tl-toolbar').addEventListener('change', (e) => {
      const k = e.target.dataset.f;
      if (!k || k === 'q') return;
      f[k] = e.target.value;
      if (k === 'range') root.querySelector('.range-custom').hidden = f.range !== 'custom';
      if (k === 'range' && f.range === 'custom' && !f.from) return;
      syncHash(); load(true);
    });
    // infinite scroll
    io = new IntersectionObserver((ents) => { if (ents.some((x) => x.isIntersecting) && next && !loading) load(false); }, { rootMargin: '600px 0px' });
    io.observe(root.querySelector('[data-slot="more"]'));
    unsubs = [
      T.on('ev:video.added', (d) => { T.cacheVideos([d.video]); insert(d.video); }),
      T.on('ev:video.state', (d) => onState(d.video)),
      T.on('ev:video.progress', (p) => ui.patchProgress(p)),
      T.on('ev:video.removed', onRemoved),
    ];
    await load(true);
  };
  V.leave = function () { unsubs.forEach((u) => u()); unsubs = []; if (io) io.disconnect(); io = null; seq++; T.bulk.clear(); document.body.classList.remove('select-mode'); };
  V.onStatus = dash;
  V.refresh = () => load(true);
  V.act = function (name, el) {
    if (name === 'more') { load(false); return; }
    if (name === 'retry-list') { load(true); return; }
    if (name === 'clear-filters') { T.app.navigate('#/timeline'); return; }
    if (name === 'add-channel') { T.openAddChannel(); return; }
    if (name === 'select-mode') { const on = !document.body.classList.contains('select-mode'); document.body.classList.toggle('select-mode', on); el.classList.toggle('active', on); if (!on) T.bulk.clear(); return; }
    if (name === 'refresh-all') { T.api.post('/api/channels/refresh').then((r) => T.toast(r.message || 'Checking every channel now.')).catch(T.toastError); return; }
    return false;
  };
  // status chips (buttons, not selects)
  document.addEventListener('click', (e) => {
    const c = e.target.closest('[data-group]');
    if (!c || !root || !root.contains(c) || T.app.view !== V) return;
    f.group = c.dataset.group;
    root.querySelectorAll('[data-group]').forEach((b) => { const on = b === c; b.classList.toggle('active', on); b.setAttribute('aria-pressed', String(on)); });
    syncHash(); load(true);
  });
})();
