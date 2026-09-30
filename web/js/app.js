/* Tubarr web UI: sign-in gate, shell (sidebar, top bar, phone tab bar), hash router, global live indicators. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon;
  const app = (T.app = { view: null, route: null });
  T.views = T.views || {};

  const NAV = [
    { id: 'timeline', label: 'Timeline', icon: 'history', href: '#/timeline' },
    { id: 'channels', label: 'Channels', icon: 'tv', href: '#/channels' },
    { id: 'activity', label: 'Activity', icon: 'activity', href: '#/activity' },
    { id: 'storage', label: 'Storage', icon: 'storage', href: '#/storage' },
    { id: 'trim', label: 'Trim ads', icon: 'scissors', href: '#/trim' },
    { id: 'settings', label: 'Settings', icon: 'sliders', href: '#/settings' },
  ];
  const SETTINGS_SUB = [['plex', 'Plex'], ['import', 'Import'], ['downloads', 'Downloads'], ['network', 'Network'], ['notifications', 'Notifications'], ['trim', 'Trim ads'], ['account', 'Account']];

  /* ---------- Shell ---------- */
  function shell() {
    return html`
      <a class="skip" href="#view">Skip to content</a>
      <aside class="sidebar" aria-label="Main">
        <a class="brand" href="#/timeline" aria-label="Tubarr home">${T.logoMark()}<span class="wordmark">Tubarr</span></a>
        <nav class="nav">
          ${NAV.map((n) => html`<a class="nav-item" href="${n.href}" data-nav="${n.id}">
            ${icon(n.icon)}<span class="nav-label">${n.label}</span><span class="nav-badge" data-badge="${n.id}"></span></a>
            ${n.id === 'settings' ? html`<div class="nav-sub" data-navsub="settings">${SETTINGS_SUB.map(([k, l]) => html`<a href="#/settings/${k}" data-sub="${k}">${l}</a>`)}</div>` : ''}`)}
        </nav>
        <a class="side-now" href="#/activity" data-slot="side-now" hidden></a>
        <div class="side-foot"><span data-slot="version">Tubarr</span>${T.api.mode === 'mock' ? html`<button class="mock-pill" type="button" data-act="mock-menu" title="Preview mode: the data is made up">${icon('info')}Mock data</button>` : html`<button class="btn-icon side-logout" type="button" data-act="logout" title="Sign out${T.api.auth && T.api.auth.user ? ' (' + T.api.auth.user + ')' : ''}" aria-label="Sign out">${icon('logout')}</button>`}</div>
      </aside>
      <header class="topbar">
        <a class="brand brand-top" href="#/timeline" aria-label="Tubarr home">${T.logoMark()}<span class="wordmark">Tubarr</span></a>
        <div class="gsearch" role="search">
          ${icon('search', 'gsearch-ic')}
          <input id="gsearch" type="search" placeholder="Search channels and videos" autocomplete="off" spellcheck="false" aria-label="Search channels and videos" aria-controls="gsearch-list" aria-expanded="false">
          <kbd class="gsearch-kbd" aria-hidden="true">/</kbd>
          <div class="gsearch-list" id="gsearch-list" role="listbox" hidden></div>
        </div>
        <button class="btn-icon top-search-btn" type="button" data-act="open-search" aria-label="Search">${icon('search')}</button>
        <div class="top-ind">
          <span class="ind ind-reconn" data-slot="ind-reconn" role="status" hidden><span class="spin"></span><span class="ind-text">Reconnecting…</span></span>
          <a class="ind ind-today" href="#/timeline" data-slot="ind-today" title="Videos that landed in Plex today"></a>
          <a class="ind ind-act" href="#/activity" data-slot="ind-act"></a>
          <a class="ind ind-net is-muted" href="#/settings/network" data-slot="ind-net"></a>
          <a class="ind ind-store" href="#/storage" data-slot="ind-store"></a>
          ${T.api.mode === 'mock' ? html`<button class="mock-pill mock-pill-top" type="button" data-act="mock-menu" title="Preview mode: the data is made up">Mock</button>` : ''}
        </div>
      </header>
      <div class="conn-banner" data-slot="conn" role="status" hidden>${icon('offline')}<span>Can't reach Tubarr. Retrying…</span></div>
      <div class="conn-banner worker-banner" data-slot="worker" role="status" hidden>${icon('alert')}<span>Tubarr's downloader isn't running, so nothing new downloads. This page shows the last known state.</span></div>
      <main class="main" id="main"><div id="view" class="view" tabindex="-1"></div></main>
      <nav class="tabbar" aria-label="Main">
        ${NAV.map((n) => html`<a class="tab" href="${n.href}" data-nav="${n.id}">${icon(n.icon)}<span>${n.label}</span><i class="tab-dot" data-tabdot="${n.id}"></i></a>`)}
      </nav>`;
  }

  /* ---------- Router ---------- */
  function parseRoute() {
    const raw = location.hash.replace(/^#\/?/, '');
    const [path, qs] = raw.split('?');
    const parts = (path || '').split('/').filter(Boolean).map(decodeURIComponent);
    const query = new URLSearchParams(qs || '');
    const name = parts[0] || 'timeline';
    if (name === 'channel' && parts[1]) return { view: 'channel', nav: 'channels', id: parts[1], query };
    if (name === 'settings') return { view: 'settings', nav: 'settings', section: parts[1] || 'plex', query };
    if (name === 'setup' && T.views.setup) return { view: 'setup', nav: 'settings', query };
    if (T.views[name]) return { view: name, nav: name, query };
    return { view: 'timeline', nav: 'timeline', query };
  }
  app.navigate = (hash) => { if (location.hash === hash) go(); else location.hash = hash; };
  app.reload = () => go(true);

  async function go(force) {
    const r = parseRoute();
    const prev = app.route;
    // Settings sections are one page: switching section just scrolls, no re-render.
    if (!force && prev && prev.view === 'settings' && r.view === 'settings' && app.view && app.view.section) {
      app.route = r; markNav(r); app.view.section(r.section, true); return;
    }
    if (app.view && app.view.leave) try { app.view.leave(); } catch (e) { console.error(e); }
    const V = T.views[r.view];
    app.view = V; app.route = r;
    markNav(r);
    const root = document.getElementById('view');
    root.classList.remove('enter'); void root.offsetWidth; root.classList.add('enter');
    if (!prev || prev.view !== r.view || prev.id !== r.id) window.scrollTo(0, 0);
    try { await V.enter(root, r); } catch (e) { console.error(e); T.setHTML(root, html`<div class="page">${T.ui.errorState(e, 'reload')}</div>`); }
    document.title = (V.docTitle ? V.docTitle(r) : V.title) + ' · Tubarr';
  }
  function markNav(r) {
    T.$$('[data-nav]').forEach((a) => { const on = a.dataset.nav === r.nav; a.classList.toggle('active', on); if (on) a.setAttribute('aria-current', 'page'); else a.removeAttribute('aria-current'); });
    const sub = T.$('[data-navsub="settings"]');
    if (sub) { sub.classList.toggle('open', r.view === 'settings'); T.$$('a', sub).forEach((a) => a.classList.toggle('active', r.view === 'settings' && a.dataset.sub === r.section)); }
  }

  /* ---------- Fill state wording (top bar, dashboard, Storage) ---------- */
  T.fillLine = function (st, short) {
    const f = st.fill || {};
    if (f.state === 'rolling') {
      const lr = f.last_removed;
      if (short) return html`Rolling${lr ? html` · last out ${T.tAgo(lr.at)}` : ''}`;
      return html`Full and rolling${lr ? html`: last out was “${lr.video.title}” (${lr.reason === 'watched' ? 'watched' : 'making room'}, ${T.tAgo(lr.at)})` : ''}`;
    }
    const left = Math.max(0, st.target_bytes - st.used_bytes);
    if (short) return html`${f.reach_date ? html`Back to ${T.fmtDay(f.reach_date, true)} · ` : ''}${T.fmtBytes(left)} to go`;
    return html`Filling the backlog${f.reach_date ? html`, reaching back to ${T.fmtDay(f.reach_date, true)}` : ''} · ${T.fmtBytes(left)} to go`;
  };
  /* The downloader runs many jobs at once (self-tuning, 3 to 24). `active` lists them; an older server only sends
     `current`, which becomes a list of one. */
  T.dlJobs = function (d) {
    if (!d) return [];
    if (Array.isArray(d.active)) return d.active;
    const c = d.current;
    return c ? [{ video_id: c.video_id, video: null, channel_title: c.channel_title, title: c.title, thumbnail_url: c.thumbnail_url, stage: c.stage, progress: c.progress, speed_bps: c.speed_bps, eta_seconds: c.eta_seconds, new_upload: false }] : [];
  };
  T.dlWaitLabel = function (d) {
    const p = d.pacing || {};
    if (d.wait_reason === 'pace' && p.next_download_at) return 'Next download in ' + T.fmtUntil(p.next_download_at) + (p.slowed_by_account ? ' (slowed down)' : '');
    if (d.wait_reason === 'daily_cap') return 'Daily limit reached (' + p.started_last_24h + ' of ' + p.cap_24h + '): new uploads only';
    if (d.wait_reason === 'older') return 'Waiting: older than everything in your library';
    if (d.backoff && d.backoff.active) return 'YouTube asked to slow down';
    if (d.backlog_paused) return 'Library full: new uploads only';
    if (d.overnight && d.overnight.start) return 'Waiting for ' + T.fmtHHMM(d.overnight.start);
    return 'Waiting';
  };
  /* The same words as dlWaitLabel, but the "Next download in 6:42" countdown ticks every second. */
  T.dlWaitHtml = function (d) {
    const p = d.pacing || {};
    if (d.wait_reason === 'pace' && p.next_download_at) return T.tUntil(p.next_download_at, { pre: 'Next download in ', post: p.slowed_by_account ? ' (slowed down)' : '', zero: 'Starting…' });
    return T.dlWaitLabel(d);
  };
  /* Severity is only about the hard space limit (quota); being at the fill target (rolling) is normal. */
  function sev(ratio) { return ratio >= 0.985 ? 'crit' : ratio >= 0.97 ? 'warn' : 'ok'; }
  app.storageSev = sev;

  /* ---------- Live indicators (top bar, sidebar, tab bar) ---------- */
  function renderIndicators(s) {
    if (!s) return;
    const st = s.storage, d = s.downloader, fl = st.fill || { state: 'filling' };
    const ratio = st.used_bytes / st.cap_bytes, tgt = st.target_bytes / st.cap_bytes;
    // storage mini meter: used of the quota, with the fill target marked
    const store = T.$('[data-slot="ind-store"]');
    if (store) {
      store.className = 'ind ind-store is-' + fl.state + ' sev-' + sev(ratio);
      store.title = T.fmtBytes(st.used_bytes) + ' used · target ' + T.fmtBytes(st.target_bytes) + ' of ' + T.fmtBytes(st.cap_bytes) + '. ' + T.textOf(T.fillLine(st));
      T.setHTML(store, html`<span class="mini-meter" aria-hidden="true"><i style="width:${(Math.min(1, ratio) * 100).toFixed(1)}%"></i><em style="left:${(tgt * 100).toFixed(1)}%"></em></span><span class="ind-text"><b>${T.fmtBytes(st.used_bytes, { digits: 2 })}</b><span class="ind-dim"> / ${T.fmtBytes(st.target_bytes, { digits: 1 })}</span><span class="ind-fill">${fl.state === 'rolling' ? 'Rolling' : 'Filling'}</span></span>`);
    }
    // activity
    const act = T.$('[data-slot="ind-act"]');
    const jobs = T.dlJobs(d);
    if (act) {
      const c = jobs.length === 1 ? jobs[0] : null;
      let cls = 'idle', label = d.queue_count ? 'Starting the next download' : 'All caught up', extra = '', labelHtml = null;
      if (d.state === 'paused' || (d.paused && !jobs.length)) { cls = 'paused'; label = 'Paused'; }
      else if (d.state === 'stopped') { cls = 'stopped'; label = 'Worker stopped'; }
      else if (jobs.length > 1) { cls = 'live'; label = jobs.length + ' downloading'; extra = d.speed_bps ? T.fmtSpeed(d.speed_bps) : ''; }
      else if (c) {
        cls = 'live';
        label = c.stage === 'download' ? 'Downloading' : T.ui.STAGES[c.stage];
        extra = c.stage === 'download' && c.progress != null ? Math.floor(c.progress * 100) + '%' : '';
      } else if (d.state === 'waiting') { cls = 'waiting'; label = T.dlWaitLabel(d); labelHtml = T.dlWaitHtml(d); }
      if (d.paused && jobs.length) label += ' · then paused';
      act.className = 'ind ind-act is-' + cls;
      act.title = c ? c.channel_title + ': ' + c.title : jobs.length > 1 ? jobs.slice(0, 4).map((j) => j.channel_title + ': ' + j.title).join('\n') + (jobs.length > 4 ? '\n…and ' + (jobs.length - 4) + ' more' : '') : label;
      act.setAttribute('aria-label', label + (extra ? ' ' + extra : '') + (d.queue_count ? ', ' + d.queue_count + ' queued' : ''));
      T.setHTML(act, html`<span class="tally" aria-hidden="true"></span><span class="ind-text"><span class="ind-label">${labelHtml || label}</span>${extra ? html` <b class="num" ${c && c.video_id ? T.raw('data-ppct="' + T.esc(c.video_id) + '"') : ''}>${extra}</b>` : ''}${!jobs.length && d.pacing && d.pacing.cap_24h ? html`<span class="ind-dim"> · ${d.pacing.started_last_24h} of ${d.pacing.cap_24h} in 24 h</span>` : ''}</span>${c && c.progress != null ? html`<i class="ind-prog" ${c.video_id ? T.raw('data-pbar="' + T.esc(c.video_id) + '"') : ''} style="width:${(c.progress * 100).toFixed(1)}%"></i>` : ''}`);
    }
    // today
    const today = T.$('[data-slot="ind-today"]');
    if (today && s.today) {
      today.title = s.today.added + ' new uploads and ' + s.today.backfill + ' backlog videos landed in Plex today' + (s.today.removed ? '; ' + s.today.removed + ' rolled out' : '') + '.' +
        (s.feed ? ' Feeds are checked every ' + s.feed.interval_minutes + ' min (last ' + T.fmtTime(s.feed.last_check_at).replace('\u00a0', ' ') + ')' + (s.feed.typical_minutes_to_plex != null ? '; new uploads are usually in Plex ~' + s.feed.typical_minutes_to_plex + ' min after they go up.' : '.') : '');
      T.setHTML(today, html`<b class="num">+${s.today.added}</b><span class="ind-dim"> today</span>`);
    }
    // sidebar badges + tab dots
    const badge = (id, raw, cls) => { const b = T.$(`[data-badge="${id}"]`); if (b) { b.className = 'nav-badge ' + (cls || ''); T.setHTML(b, raw || ''); } };
    badge('timeline', s.today && s.today.added ? html`+${s.today.added}` : '', 'is-brand');
    badge('channels', T.fmtNum(s.counts.channels) + (s.counts.channel_errors ? '' : ''), s.counts.channel_errors ? 'is-crit' : '');
    badge('activity', jobs.length ? html`<span class="tally" aria-hidden="true"></span>${jobs.length > 1 ? jobs.length : ''}` : '', d.failed_count ? 'is-crit' : jobs.length ? 'is-live' : '');
    badge('storage', fl.state === 'rolling' ? 'Full' : Math.round((st.used_bytes / st.target_bytes) * 100) + '%', 'sev-' + sev(ratio) + (fl.state === 'rolling' ? ' is-rolling' : ''));
    badge('settings', '');
    const dot = (id, on, cls) => { const e = T.$(`[data-tabdot="${id}"]`); if (e) e.className = 'tab-dot' + (on ? ' on ' + (cls || '') : ''); };
    dot('activity', jobs.length > 0, 'live');
    // sidebar "now downloading" card: the lead job (new uploads first), plus how many more run beside it
    const now = T.$('[data-slot="side-now"]');
    if (now) {
      const c = jobs.find((j) => j.new_upload && j.stage === 'download') || jobs.find((j) => j.stage === 'download') || jobs[0];
      now.hidden = !c;
      if (c) {
        const p = c.progress == null ? null : c.progress;
        const more = jobs.length - 1;
        const same = now.dataset.vid === (c.video_id || '') && now.dataset.stage === c.stage && now.dataset.more === String(more);
        const pctText = c.stage === 'download' ? Math.floor((p || 0) * 100) + '%' : T.ui.STAGES[c.stage];
        if (same) {
          const bar = now.querySelector('.bar > i'); if (bar && p != null) bar.style.width = (p * 100).toFixed(2) + '%';
          const pc = now.querySelector('[data-live="side-pct"]'); if (pc) pc.textContent = pctText;
          const sp = now.querySelector('[data-live="side-speed"]'); if (sp) sp.textContent = d.speed_bps ? T.fmtSpeed(d.speed_bps) : '';
        } else {
          now.dataset.vid = c.video_id || ''; now.dataset.stage = c.stage; now.dataset.more = String(more);
          const v = c.video || { id: c.video_id, title: c.title, thumbnail_url: c.thumbnail_url };
          T.setHTML(now, html`<span class="side-now-eyebrow"><span class="tally" aria-hidden="true"></span>${more ? jobs.length + ' downloading' : 'Now downloading'}<span class="side-now-speed" data-live="side-speed">${d.speed_bps ? T.fmtSpeed(d.speed_bps) : ''}</span></span>
            ${T.thumb(v, 'side-now-thumb')}
            <span class="side-now-title">${c.title}</span>
            <span class="side-now-sub"><span>${c.channel_title}</span><b data-live="side-pct">${pctText}</b></span>
            ${T.ui.bar(c.stage === 'download' ? p : null, 'bar-sm')}
            ${more ? html`<span class="side-now-more">+${more} more at the same time</span>` : ''}`);
        }
      } else { now.dataset.vid = ''; }
    }
    const wb = T.$('[data-slot="worker"]');
    if (wb) wb.hidden = d.state !== 'stopped';
    const ver = T.$('[data-slot="version"]');
    if (ver) ver.textContent = 'Tubarr ' + s.version;
  }

  /* ---------- Network chip: which line YouTube traffic uses (Settings -> Network) ---------- */
  const MODE_NAMES = { direct: 'Direct', single: 'One proxy', failover: 'Failover', rotate: 'Rotate' };
  function renderNetChip(n) {
    const el = T.$('[data-slot="ind-net"]');
    if (!el) return;
    const s = (n && n.status) || null;
    let cls = 'muted', text = 'Network: unknown', tip = 'Couldn’t read the network status.';
    if (n && s) {
      const direct = (s.active || 'direct') === 'direct';
      text = direct && (n.mode === 'direct' || !(n.proxies || []).length) ? html`Network: Direct` : html`Network: ${s.active_name || 'Direct'}${s.ip ? html`<span class="net-chip-ip"> · ${s.ip}</span>` : ''}`;
      cls = s.paused ? 'warn' : s.stale ? 'muted' : 'ok';
      if (s.paused) text = html`Network: paused`;
      const ls = s.last_switch;
      tip = 'YouTube traffic uses ' + (s.active_name || 'Direct') + (s.ip ? ' (' + s.ip + ')' : '') + '. Mode: ' + (MODE_NAMES[n.mode] || n.mode) + '.' +
        (ls ? ' Last switch ' + T.fmtTime(ls.at) + ': ' + ls.from_name + ' → ' + ls.to_name + (ls.reason ? ' (' + ls.reason + ')' : '') + '.' : '') +
        (s.stale ? ' The status hasn’t updated for a while.' : '') +
        (s.paused ? ' Downloads are paused: no proxy is answering' + (s.reason ? ' (' + s.reason + ')' : '') + '.' : '');
    }
    el.className = 'ind ind-net is-' + cls;
    el.title = tip;
    T.setHTML(el, html`${icon(cls === 'warn' ? 'alert' : 'server')}<span class="ind-text">${text}</span>`);
  }
  let netTimer = null;
  /* Pass a fresh GET /api/network answer to show it at once (Settings does after a change); otherwise it fetches. */
  app.refreshNetChip = function (n) {
    clearTimeout(netTimer);
    const next = () => { if (!T.live.stopped) netTimer = setTimeout(() => app.refreshNetChip(), 30000); };
    if (n) { renderNetChip(n); next(); return; }
    T.api.get('/api/network').then(renderNetChip, () => renderNetChip(null)).finally(next);
  };

  /* ---------- Global search (channels + a jump into the Timeline) ---------- */
  let channelCache = null, channelCacheAt = 0;
  async function channelsForSearch() {
    if (channelCache && Date.now() - channelCacheAt < 60000) return channelCache;
    const r = await T.api.get('/api/channels');
    channelCache = r.channels; channelCacheAt = Date.now();
    return channelCache;
  }
  app.invalidateChannels = () => { channelCache = null; };
  function setupSearch() {
    const input = T.$('#gsearch'), list = T.$('#gsearch-list');
    let items = [], active = -1;
    const close = () => { list.hidden = true; input.setAttribute('aria-expanded', 'false'); active = -1; };
    const paint = () => T.$$('.gs-item', list).forEach((el, i) => el.classList.toggle('active', i === active));
    const run = T.debounce(async () => {
      const q = input.value.trim().toLowerCase();
      if (!q) { close(); return; }
      let chs = [];
      try { chs = await channelsForSearch(); } catch (e) { /* ignore */ }
      const hits = chs.filter((c) => (c.title + ' ' + c.handle).toLowerCase().includes(q)).slice(0, 6);
      items = hits.map((c) => ({ href: '#/channel/' + encodeURIComponent(c.id), c })).concat([{ href: '#/timeline?q=' + encodeURIComponent(input.value.trim()), videos: true }]);
      T.setHTML(list, items.map((it, i) => it.videos
        ? html`<a class="gs-item gs-videos" role="option" href="${it.href}" data-i="${i}">${icon('search')}<span>Search videos for <b>“${input.value.trim()}”</b></span></a>`
        : html`<a class="gs-item" role="option" href="${it.href}" data-i="${i}">${T.poster(it.c, 'poster-xs')}<span class="gs-main"><span class="gs-title">${it.c.title}</span><span class="gs-sub">${it.c.handle} · ${T.plural(it.c.video_count, 'video')}</span></span></a>`));
      list.hidden = false; input.setAttribute('aria-expanded', 'true'); active = 0; paint();
    }, 120);
    input.addEventListener('input', run);
    input.addEventListener('focus', () => { if (input.value.trim()) run(); });
    input.addEventListener('keydown', (e) => {
      if (list.hidden) { if (e.key === 'Enter' && input.value.trim()) { app.navigate('#/timeline?q=' + encodeURIComponent(input.value.trim())); input.blur(); } return; }
      if (e.key === 'ArrowDown') { e.preventDefault(); active = Math.min(items.length - 1, active + 1); paint(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); active = Math.max(0, active - 1); paint(); }
      else if (e.key === 'Enter') { e.preventDefault(); const it = items[active]; if (it) { app.navigate(it.href); close(); input.blur(); document.body.classList.remove('search-open'); } }
      else if (e.key === 'Escape') { close(); input.blur(); document.body.classList.remove('search-open'); }
    });
    list.addEventListener('click', () => { close(); input.value = ''; document.body.classList.remove('search-open'); });
    document.addEventListener('pointerdown', (e) => { if (!e.target.closest('.gsearch')) { close(); if (!e.target.closest('.top-search-btn')) document.body.classList.remove('search-open'); } });
    document.addEventListener('keydown', (e) => {
      if (e.key === '/' && !e.ctrlKey && !e.metaKey && !/^(INPUT|TEXTAREA|SELECT)$/.test(document.activeElement.tagName) && !document.querySelector('.modal-wrap')) { e.preventDefault(); document.body.classList.add('search-open'); input.focus(); }
    });
  }

  /* ---------- Global click actions (views get first pick via view.act) ---------- */
  const globalActs = {
    reload: () => app.reload(),
    logout: () => app.signOut(),
    'open-search': () => { document.body.classList.add('search-open'); T.$('#gsearch').focus(); },
    'mock-menu': (el) => T.menu(el, [
      { label: 'Filling the backlog (default)', icon: 'tv', fn: () => setScenario('') },
      { label: 'Full and rolling', icon: 'storage', fn: () => setScenario('full') },
      { label: 'Empty library (first run)', icon: 'inbox', fn: () => setScenario('fresh') },
      { label: 'Things going wrong', icon: 'alert', fn: () => setScenario('trouble') },
      '-',
      { label: 'The data here is made up. The real server replaces it automatically.', icon: 'info', disabled: true },
    ]),
  };
  function setScenario(s) {
    const q = new URLSearchParams(location.search);
    if (s) q.set('scenario', s); else q.delete('scenario');
    if (T.api.mode === 'mock' && location.protocol !== 'file:') q.set('mock', '1');
    location.search = q.toString();
  }
  document.addEventListener('click', (e) => {
    const el = e.target.closest('[data-act]');
    if (!el || el.closest('.modal-wrap') || el.closest('.menu') || el.closest('.auth')) return;
    const name = el.dataset.act;
    if (app.view && app.view.act && app.view.act(name, el, e) !== false) { e.preventDefault(); return; }
    if (T.videoActs && T.videoActs.handle(name, el, e)) { e.preventDefault(); return; }
    if (globalActs[name]) { e.preventDefault(); globalActs[name](el, e); }
  });

  /* ---------- Sign-in gate ----------
     Live mode: GET /api/auth/state before anything else. No admin yet -> "Create your admin account";
     not signed in -> sign-in screen. Any 401 later (api.js emits 'unauthorized') -> the sign-in screen again.
     Mock mode skips all of this (always signed in). */
  const MIN_PW = 10;
  function authCard(inner) {
    return html`<main class="auth" id="main">
      <div class="auth-card card">
        <div class="auth-brand">${T.logoMark()}<span class="wordmark">Tubarr</span></div>
        ${inner}
      </div>
      <p class="auth-foot dim">Your YouTube subscriptions, downloaded into Plex.</p>
    </main>`;
  }
  function field(id, label, type, auto, extra) {
    return html`<div class="field"><label class="label" for="${id}">${label}</label><input class="input" id="${id}" name="${id}" type="${type}" autocomplete="${auto}" autocapitalize="off" spellcheck="false" ${T.raw(extra || '')}></div>`;
  }
  function showAuth(markup, onSubmit) {
    const root = document.getElementById('app');
    if (app.view && app.view.leave) try { app.view.leave(); } catch (e) { console.error(e); }
    app.view = null; app.route = null;
    document.body.classList.remove('search-open', 'has-modal');
    T.$$('.modal-wrap').forEach((m) => m.remove());
    root.classList.add('is-auth');
    T.setHTML(root, authCard(markup));
    document.title = 'Sign in · Tubarr';
    const form = root.querySelector('form');
    const err = form.querySelector('[data-slot="auth-err"]');
    const btn = form.querySelector('button[type="submit"]');
    const say = (msg) => { err.hidden = !msg; err.textContent = msg || ''; };
    form.addEventListener('submit', async (e) => {
      e.preventDefault();
      say('');
      const v = {}; Array.from(form.elements).forEach((i) => { if (i.name) v[i.name] = i.value; });
      btn.disabled = true;
      try { await onSubmit(v, say); } catch (ex) { say(ex.message || 'Something went wrong.'); }
      finally { btn.disabled = false; }
    });
    const first = form.querySelector('input'); if (first) first.focus();
  }
  function showLogin(note) {
    showAuth(html`<h1>Sign in</h1>
      ${note ? html`<p class="auth-note">${icon('info')}<span>${note}</span></p>` : html`<p class="auth-lead">Welcome back.</p>`}
      <form class="auth-form" novalidate>
        ${field('username', 'Username', 'text', 'username', 'required')}
        ${field('password', 'Password', 'password', 'current-password', 'required')}
        <p class="auth-err" data-slot="auth-err" role="alert" hidden></p>
        <button class="btn btn-primary btn-lg btn-block" type="submit">${icon('login')}Sign in</button>
      </form>`, async (v, say) => {
      if (!v.username.trim() || !v.password) { say('Enter your username and password.'); return; }
      try {
        const s = await T.api.post('/api/auth/login', { username: v.username.trim(), password: v.password });
        signedIn(s);
      } catch (e) {
        if (e.status === 401) say(e.message || 'Wrong username or password.');
        else if (e.status === 429) say(e.message || 'Too many attempts. Try again later.');
        else say(e.message);
      }
    });
  }
  function showCreateAccount(st) {
    showAuth(html`<h1>Create your admin account</h1>
      <p class="auth-lead">This is the account you sign in to Tubarr with. There is only one, and it can change everything.</p>
      <form class="auth-form" novalidate>
        ${field('username', 'Username', 'text', 'username', 'required')}
        ${field('password', 'Password', 'password', 'new-password', 'required minlength="' + MIN_PW + '"')}
        <p class="hint auth-hint">At least ${MIN_PW} characters.</p>
        ${field('confirm', 'Confirm password', 'password', 'new-password', 'required')}
        ${st.setup_code_required ? html`${field('setup_code', 'Setup code', 'text', 'one-time-code', 'required')}
          <p class="hint auth-hint">${icon('info')}<span>Find it in the container log: <code>docker logs tubarr</code></span></p>` : ''}
        <p class="auth-err" data-slot="auth-err" role="alert" hidden></p>
        <button class="btn btn-primary btn-lg btn-block" type="submit">${icon('check')}Create account</button>
      </form>`, async (v, say) => {
      if (!v.username.trim()) { say('Choose a username.'); return; }
      if (v.password.length < MIN_PW) { say('The password needs at least ' + MIN_PW + ' characters.'); return; }
      if (v.password !== v.confirm) { say('The two passwords don’t match.'); return; }
      if (st.setup_code_required && !String(v.setup_code || '').trim()) { say('Enter the setup code from the container log.'); return; }
      try {
        const s = await T.api.post('/api/auth/setup', { username: v.username.trim(), password: v.password, setup_code: String(v.setup_code || '').trim() });
        signedIn(s);
      } catch (e) {
        if (e.status === 403) say(e.message || 'That setup code isn’t right. Check docker logs tubarr.');
        else if (e.status === 409) { gate(); }
        else say(e.message);
      }
    });
  }
  function showGateError(e) {
    const root = document.getElementById('app');
    root.classList.add('is-auth');
    T.setHTML(root, authCard(html`<h1>Can’t reach Tubarr</h1><p class="auth-lead">${e && e.message ? e.message : 'The server didn’t answer.'}</p>
      <form class="auth-form"><p class="auth-err" data-slot="auth-err" hidden></p><button class="btn btn-primary btn-lg btn-block" type="submit">${icon('refresh')}Try again</button></form>`));
    root.querySelector('form').addEventListener('submit', (ev) => { ev.preventDefault(); gate(); });
  }
  async function gate(note) {
    let st;
    try { st = await T.api.authState(); } catch (e) { showGateError(e); return; }
    if (st.setup_required) { showCreateAccount(st); return; }
    if (!st.authenticated) { showLogin(note); return; }
    signedIn(st);
  }
  let started = false;
  function signedIn(st) {
    T.api.signedIn(st);
    // After a mid-session sign-out the old shell is gone: a fresh page is the cleanest restart.
    if (started) { location.reload(); return; }
    startApp();
  }
  app.signOut = async function () {
    if (T.api.mode === 'mock') { T.toast('Preview mode: there is nothing to sign out of.', { type: 'info' }); return; }
    try { await T.api.post('/api/auth/logout'); } catch (e) { /* signed out either way */ }
    T.live.stop();
    T.api.auth = Object.assign({}, T.api.auth || {}, { authenticated: false, csrf: null, user: null });
    showLogin('You’re signed out.');
  };
  T.on('unauthorized', () => { if (T.api.mode === 'live' && !document.getElementById('app').classList.contains('is-auth')) showLogin('Please sign in again.'); });

  /* ---------- Boot ---------- */
  async function startApp() {
    const root = document.getElementById('app');
    root.classList.remove('is-auth');
    T.setHTML(root, shell());
    document.body.classList.toggle('is-mock', T.api.mode === 'mock');
    started = true;
    setupSearch();
    T.on('status', (s) => { renderIndicators(s); if (app.view && app.view.onStatus) app.view.onStatus(s); });
    T.on('reconnecting', (v) => { const e = T.$('[data-slot="ind-reconn"]'); if (e) e.hidden = !v; });
    // download progress between server updates (tick.js interpolates and re-emits video.progress every second)
    T.on('ev:video.progress', (p) => {
      if (!p || p.stage !== 'download' || p.progress == null) return;
      const w = (p.progress * 100).toFixed(2) + '%', pc = Math.floor(p.progress * 100) + '%';
      T.$$(`[data-pbar="${CSS.escape(p.id)}"]`).forEach((e) => { e.style.width = w; });
      T.$$(`[data-ppct="${CSS.escape(p.id)}"]`).forEach((e) => { if (e.textContent !== pc) e.textContent = pc; });
      const now = T.$('[data-slot="side-now"]');
      if (now && now.dataset.vid === p.id && now.dataset.stage === 'download') {
        const bar = now.querySelector('.bar > i'); if (bar) bar.style.width = w;
        const t = now.querySelector('[data-live="side-pct"]'); if (t && t.textContent !== pc) t.textContent = pc;
      }
    });
    T.on('connection', (online) => { const b = T.$('[data-slot="conn"]'); if (b) b.hidden = online; });
    T.on('stream-reconnected', () => { if (app.view && app.view.refresh) app.view.refresh(); });
    T.on('ev:channel.added', () => app.invalidateChannels());
    T.on('ev:channel.removed', () => app.invalidateChannels());
    // First run: send a fresh install to the setup wizard (without a hashchange, so the view renders once).
    try {
      const su = await T.api.get('/api/setup');
      app.setupDone = !!su.done;
      if (!su.done && parseRoute().view !== 'setup') history.replaceState(null, '', location.pathname + location.search + '#/setup');
    } catch (e) { /* an older server without /api/setup: carry on */ }
    window.addEventListener('hashchange', () => go());
    T.live.start();
    app.refreshNetChip();
    T.trimarr.load();
    await go();
    if (T.api.mode === 'mock') console.info('Tubarr: mock mode (' + T.api.reason + '). Scenarios: ?scenario=full | fresh | trouble');
  }
  async function boot() {
    document.body.insertAdjacentHTML('afterbegin', T.iconSprite());
    await T.api.detect();
    if (T.api.mode === 'mock') {
      T.api.signedIn({ setup_required: false, setup_code_required: false, authenticated: true, user: 'admin', csrf: null, via: 'session' });
      startApp();
      return;
    }
    gate();
  }
  document.addEventListener('DOMContentLoaded', boot);
})();
