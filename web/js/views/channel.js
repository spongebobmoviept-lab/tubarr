/* Channel page: hero header, episode list (by season, live), and the channel's own rules with a live estimate. */
(function () {
  'use strict';
  const UNDATED = 'undated';
  const T = window.T;
  const html = T.html, icon = T.icon, ui = T.ui;

  const GROUPS = [
    ['all', 'All', null], ['plex', 'In Plex', ['downloaded']], ['progress', 'In progress', ['downloading', 'queued', 'waiting_sponsorblock', 'upcoming']],
    ['failed', 'Failed', ['failed']], ['skipped', 'Skipped', ['skipped_short', 'skipped_live', 'skipped_too_long', 'skipped_members']], ['removed', 'Removed', ['removed']], ['kept', 'Kept forever', null], ['gone', 'Gone from YouTube', null],
  ];
  const MAXLEN = [[0, 'No limit'], [20, '20 min'], [30, '30 min'], [60, '1 hour'], [90, '90 min'], [120, '2 hours'], [180, '3 hours']];
  const maxLabel = (m) => (MAXLEN.find((x) => x[0] === m) || [m, m + ' min'])[1];

  const V = (T.views.channel = { nav: 'channels', title: 'Channel' });
  let root, ch, videos = [], defaults = null, st = { group: 'all', q: '' }, unsubs = [], collapsed = new Set(), tick = null;

  V.docTitle = () => (ch ? ch.title : 'Channel');

  /* ---------- Hero ---------- */
  function hero() {
    const eff = ch.effective_settings, s = ch.stats;
    const mode = eff.retention.mode;
    const stat = (label, value, sub) => html`<div class="hstat"><span class="hstat-v">${value}</span><span class="hstat-l">${label}${sub ? html` <span class="dim">${sub}</span>` : ''}</span></div>`;
    return html`<section class="ch-hero">
      <div class="ch-hero-bg">${T.bannerArt(ch)}${ch.banner_url ? html`<img src="${ch.banner_url}" alt="" data-fallback>` : ''}</div>
      <div class="ch-hero-inner">
        <a class="back-link" href="#/channels">${icon('arrow-left')}Channels</a>
        <div class="ch-hero-main">
          <div class="ch-hero-poster">${T.poster(ch, 'poster-lg')}<button class="poster-edit" type="button" data-act="edit" aria-label="Change poster and info">${icon('wand')}</button></div>
          <div class="ch-hero-text">
            <div class="ch-eyebrow"><span>${ch.handle}</span><span class="sep">·</span><span>${T.fmtCompact(ch.subscribers)} subscribers</span>${ch.genre ? html`<span class="sep">·</span><span>${ch.genre}</span>` : ''}<span class="sep">·</span><a href="${ch.url}" target="_blank" rel="noopener noreferrer">YouTube ${icon('ext')}</a></div>
            <h1 class="ch-title">${ch.title}${ch.locked_fields.includes('title') ? html`<span class="lock-note" title="Your name for this show (YouTube calls it ${ch.youtube_title})">${icon('lock')}</span>` : ''}</h1>
            <div class="ch-badges">${ui.channelBadge(ch)}
              <span class="tag">${icon('tag')}${ui.topicLabel(ch.topic)}</span>
              <span class="tag ${mode === 'fill' ? '' : 'tag-brand'}">${mode === 'forever' ? icon('infinity') : icon('storage')}${T.retention.describe(ch.settings.retention)}</span>
              <span class="tag">${icon('tv')}${eff.quality === '2160p' ? 'Up to 4K' : eff.quality}</span>
              ${(eff.sponsorblock || []).length ? html`<span class="tag">${icon('scissors')}SponsorBlock</span>` : ''}
              ${!ch.subscribed ? html`<span class="tag tag-warn" title="Not in your YouTube subscriptions">${icon('info')}Not subscribed</span>` : ''}
            </div>
            ${ch.summary ? html`<p class="ch-about" data-slot="about">${ch.summary}</p>` : ''}
            <div class="hero-stats">
              ${stat('in Plex', T.fmtNum(ch.video_count), T.fmtBytes(ch.size_bytes))}
              ${stat('queued', T.fmtNum(ch.queued_count))}
              ${(eff.sponsorblock || []).length || s.sponsorblock_saved_seconds ? stat('SponsorBlock saved', s.sponsorblock_saved_seconds ? T.fmtSpan(s.sponsorblock_saved_seconds) : '—') : stat('watched', T.fmtNum(s.watched_count))}
              ${stat('latest upload', ch.last_upload_at ? T.fmtAgo(ch.last_upload_at) : '—')}
              ${stat('reaches back to', s.oldest_upload_date ? T.fmtDay(s.oldest_upload_date, true) : '—')}
              ${stat('never removed', T.fmtNum(s.protected_count || 0), s.kept_count ? s.kept_count + ' kept by you' : '')}
            </div>
            <div class="ch-actions">
              <button class="btn" type="button" data-act="refresh" ${ch.status === 'pending_removal' || ch.status === 'gone' ? T.raw('disabled') : ''}>${icon('refresh')}Refresh now</button>
              <button class="btn" type="button" data-act="repolish">${icon('sparkles')}Re-polish in Plex</button>
              <button class="btn" type="button" data-act="edit">${icon('wand')}Edit info &amp; poster</button>
              ${ch.plex_url ? html`<a class="btn" href="${ch.plex_url}" target="_blank" rel="noopener noreferrer">${icon('film')}Open in Plex</a>` : ''}
              ${ch.status === 'pending_removal' ? html`<button class="btn btn-warn" type="button" data-act="restore">${icon('undo')}Undo removal</button>` : html`<button class="btn btn-danger-ghost" type="button" data-act="remove">${icon('trash')}Remove channel</button>`}
            </div>
          </div>
        </div>
      </div>
    </section>`;
  }
  function banners() {
    if (ch.status === 'pending_removal' && ch.removal) {
      const why = ch.removal.reason === 'unsubscribed' ? 'You unsubscribed on YouTube, so Tubarr will remove it.' : 'You asked Tubarr to remove it.';
      return html`<div class="banner banner-warn">${icon('timer')}<div><b>Removing in ${T.tUntil(ch.removal.delete_at, { fmt: 'span', zero: 'a moment' })}</b> (${T.fmtDay(ch.removal.delete_at)} at ${T.fmtTime(ch.removal.delete_at)}). ${why} Downloads have stopped; its ${T.plural(ch.video_count, 'video')} (${T.fmtBytes(ch.size_bytes)}) leave Plex then.</div><button class="btn btn-sm btn-warn" type="button" data-act="restore">${icon('undo')}Undo</button></div>`;
    }
    if (ch.status === 'gone') return html`<div class="banner banner-gone">${icon('archive')}<div><b>Gone from YouTube</b>${ch.gone ? html` since ${T.fmtDay(ch.gone.at, true)} (${ch.gone.reason === 'terminated' ? 'the channel was terminated' : 'the channel was deleted'})` : ''}. Nothing new can arrive. Its ${T.plural(ch.video_count, 'video')} (${T.fmtBytes(ch.size_bytes)}) stay in Plex for good and are never rolled out.</div></div>`;
    if (ch.status === 'error') return html`<div class="banner banner-crit">${icon('alert')}<div><b>The last check failed.</b> ${ch.status_detail || ''}</div><button class="btn btn-sm" type="button" data-act="refresh">${icon('refresh')}Try now</button></div>`;
    if (ch.status === 'syncing') {
      const sp = ch.sync_progress;
      return html`<div class="banner banner-info">${icon('refresh', 'spin-ic')}<div><b>${sp ? 'Reading this channel’s uploads' : 'Checking for new uploads'}</b>${sp && sp.total ? html` · ${T.fmtNum(sp.done || 0)} of ${T.fmtNum(sp.total)}` : ''}${ui.bar(sp && sp.total ? (sp.done || 0) / sp.total : null, 'bar-sm')}</div></div>`;
    }
    return '';
  }

  /* ---------- Episodes ---------- */
  function visibleVideos() {
    const g = GROUPS.find((x) => x[0] === st.group);
    const q = st.q.toLowerCase();
    return videos.filter((v) => (!g[2] || g[2].includes(v.state)) && (st.group !== 'kept' || v.keep) && (st.group !== 'gone' || v.youtube_gone) && (!q || v.title.toLowerCase().includes(q)));
  }
  function renderEpisodes() {
    const box = root.querySelector('[data-slot="eps"]');
    if (!box) return;
    const counts = Object.fromEntries(GROUPS.map(([k, , s]) => [k, videos.filter((v) => (!s || s.includes(v.state)) && (k !== 'kept' || v.keep) && (k !== 'gone' || v.youtube_gone)).length]));
    T.setHTML(root.querySelector('[data-slot="ep-chips"]'), GROUPS.filter(([k]) => k === 'all' || counts[k]).map(([k, label]) => html`<button class="chip ${st.group === k ? 'active' : ''}" type="button" data-group="${k}" aria-pressed="${st.group === k}">${label}<span class="chip-n">${counts[k]}</span></button>`));
    const vis = visibleVideos();
    if (!videos.length) {
      T.setHTML(box, ui.empty({ icon: ch.status === 'syncing' ? 'refresh' : 'inbox', title: ch.status === 'syncing' ? 'Reading this channel’s uploads…' : 'No videos yet', body: ch.status === 'syncing' ? 'They appear here as soon as Tubarr has the list. The newest few download right away.' : 'New uploads show up here as soon as Tubarr finds them.' }));
      return;
    }
    if (!vis.length) { T.setHTML(box, ui.empty({ icon: 'search', title: 'Nothing here', body: 'No videos match this filter.', actions: html`<button class="btn" type="button" data-act="clear-eps">${icon('x')}Show all</button>` })); return; }
    const seasons = [], by = {};
    // backlog rows the worker has listed but not downloaded yet have no upload date: they get their own group, last
    vis.forEach((v) => { const y = v.state === 'upcoming' ? 'Coming up' : v.upload_date ? v.upload_date.slice(0, 4) : UNDATED; if (!by[y]) { by[y] = []; seasons.push(y); } by[y].push(v); });
    seasons.sort((a, b) => (a === 'Coming up' ? -1 : b === 'Coming up' ? 1 : a === UNDATED ? 1 : b === UNDATED ? -1 : b.localeCompare(a)));
    T.setHTML(box, seasons.map((y) => {
      const rows = by[y], inPlex = rows.filter((v) => v.state === 'downloaded');
      const open = !collapsed.has(y);
      return html`<section class="season ${open ? '' : 'is-collapsed'}" data-season="${y}">
        <button class="season-head" type="button" data-act="season" aria-expanded="${open}">
          ${icon('chev-down', 'season-chev')}<h3>${y === 'Coming up' ? y : y === UNDATED ? 'Queued, not dated yet' : 'Season ' + y}</h3>
          <span class="season-sub">${T.plural(rows.length, 'video')}${inPlex.length ? html` · ${inPlex.length} in Plex · ${T.fmtBytes(inPlex.reduce((s, v) => s + (v.size_bytes || 0), 0))}` : ''}</span>
        </button>
        <div class="season-rows">${rows.map((v) => ui.videoRow(v, { channel: false, when: 'date', selected: T.bulk.sel.has(v.id) }))}</div>
      </section>`;
    }));
  }

  /* ---------- Rules side panel ---------- */
  function rulesPanel() {
    const s = ch.settings, eff = ch.effective_settings, d = defaults || {};
    const def = (v) => html`<span class="def-note">${v == null ? 'Default' : 'Custom'}</span>`;
    return html`
      <section class="card side-card" data-slot="topic">
        <header class="side-head"><h2>Topic</h2><span class="side-sub">${ch.topic_source === 'user' ? 'Set by you' : 'Tubarr’s guess'}</span><span class="saved-tick" data-slot="topic-saved">${icon('check')}Saved</span></header>
        ${ui.select('topic', (ch.topic ? [] : [['', 'Not sorted yet']]).concat(ui.TOPICS), ch.topic || '', { label: 'Topic', attrs: 'data-topic=""', cls: 'select-block' })}
        <p class="hint">${icon('layers')}${ch.topic ? html`Plex shows it in the “${ui.topicLabel(ch.topic)}” collection of the YouTube library.` : 'Tubarr hasn’t sorted this channel into a topic yet.'}</p>
      </section>
      <section class="card side-card">
        <header class="side-head"><h2>How much to keep</h2><span class="side-sub">Overrides on top of fill and roll</span></header>
        ${T.retention.render(s.retention)}
      </section>
      <section class="card side-card" data-slot="rules">
        <header class="side-head"><h2>Download rules</h2><span class="saved-tick" data-slot="saved">${icon('check')}Saved</span></header>
        <div class="field"><div class="field-top"><span class="label">Quality</span>${def(s.quality)}</div>
          ${ui.select('quality', [['', 'Default (' + T.fmtRes(d.quality || eff.quality, true) + ')'], ['2160p', 'Best (up to 4K)'], ['1080p', '1080p'], ['720p', '720p (about half the space)']], s.quality || '', { label: 'Quality', attrs: 'data-rule="quality"' })}</div>
        <div class="field"><div class="field-top"><span class="label">Maximum length</span>${def(s.max_duration_minutes)}</div>
          ${ui.select('maxlen', [['', 'Default (' + maxLabel(d.max_duration_minutes != null ? d.max_duration_minutes : eff.max_duration_minutes) + ')']].concat(MAXLEN.map(([m, l]) => [String(m), l])), s.max_duration_minutes == null ? '' : String(s.max_duration_minutes), { label: 'Maximum length', attrs: 'data-rule="max_duration_minutes"' })}
          <p class="hint">Longer videos are skipped; you can still grab one with “Download anyway”.</p></div>
        <div class="field"><div class="field-top"><span class="label">Livestream replays</span>${def(s.include_live)}</div>
          ${ui.select('live', [['', 'Default (' + ((d.include_live != null ? d.include_live : eff.include_live) ? 'include' : 'skip') + ')'], ['1', 'Include'], ['0', 'Skip']], s.include_live == null ? '' : s.include_live ? '1' : '0', { label: 'Livestream replays', attrs: 'data-rule="include_live"' })}</div>
        ${(eff.sponsorblock || []).length || s.sponsorblock != null ? html`<div class="field"><div class="field-top"><span class="label">SponsorBlock: cut these</span>${s.sponsorblock != null ? html`<button class="link-btn" type="button" data-act="sb-default">${icon('undo')}Use default</button>` : def(null)}</div>
          <div class="sb-chips" role="group" aria-label="SponsorBlock categories">${ui.SB.map(([k, l]) => html`<label class="chip chip-check"><input type="checkbox" data-sb="${k}" ${(eff.sponsorblock || []).includes(k) ? T.raw('checked') : ''}><span>${icon('check')}${l}</span></label>`)}</div>
          <p class="hint">${icon('info')}Intros are never cut: they become a chapter in Plex.</p></div>`
          : html`<p class="hint">${icon('film')}Videos stay exactly as uploaded: nothing is cut out.</p>`}
        <div class="field field-trim" data-slot="trim"></div>
      </section>
      <section class="card side-card side-facts">
        <dl>
          <div><dt>Added</dt><dd>${T.fmtDay(ch.added_at, true)}</dd></div>
          <div><dt>Last checked</dt><dd>${T.tAgo(ch.last_checked_at)}</dd></div>
          <div><dt>On YouTube</dt><dd>${T.fmtNum(ch.youtube_video_count)} uploads</dd></div>
          <div><dt>Watched</dt><dd>${T.fmtNum(ch.stats.watched_count)}</dd></div>
          <div><dt>Skipped</dt><dd>${T.fmtNum(ch.stats.skipped_count)}</dd></div>
        </dl>
      </section>`;
  }
  async function saveRule(patch, msg) {
    try {
      ch = await T.api.patch('/api/channels/' + encodeURIComponent(ch.id), { settings: patch });
      ui.flashSaved(root.querySelector('[data-slot="saved"]'));
      if (msg) T.toast(msg);
      renderHero();
    } catch (e) { T.toastError(e); renderSide(); }
  }
  function renderSide() {
    const side = root.querySelector('[data-slot="side"]');
    T.setHTML(side, rulesPanel());
    T.retention.bind(side, { channelId: ch.id, initial: ch.settings.retention, onApply: async (r) => {
      ch = await T.api.patch('/api/channels/' + encodeURIComponent(ch.id), { settings: { retention: r } });
      T.toast(ch.title + ': ' + T.retention.describe(r));
      renderHero();
      loadVideos();
    } });
    loadTrim(side);
    side.querySelector('[data-slot="topic"]').addEventListener('change', async (e) => {
      if (e.target.dataset.topic == null) return;
      const t = e.target.value;
      try { ch = await T.api.patch('/api/channels/' + encodeURIComponent(ch.id), { meta: { topic: t } }); T.toast(ch.title + ' is now in “' + ui.topicLabel(t) + '” in Plex.'); renderHero(); renderSide(); }
      catch (err) { T.toastError(err); renderSide(); }
    });
    side.querySelector('[data-slot="rules"]').addEventListener('change', (e) => {
      if (e.target.dataset.trim != null) { toggleTrim(e.target); return; }
      const k = e.target.dataset.rule;
      if (k === 'quality') saveRule({ quality: e.target.value || null });
      else if (k === 'max_duration_minutes') saveRule({ max_duration_minutes: e.target.value === '' ? null : Number(e.target.value) });
      else if (k === 'include_live') saveRule({ include_live: e.target.value === '' ? null : e.target.value === '1' });
      else if (e.target.dataset.sb) {
        const cats = Array.from(side.querySelectorAll('[data-sb]')).filter((x) => x.checked).map((x) => x.dataset.sb);
        saveRule({ sponsorblock: cats });
      }
    });
  }
  /* Optional Trimarr: trims ads out of this channel's videos that are already in Plex. Kept low-key. */
  async function loadTrim(side) {
    const box = side.querySelector('[data-slot="trim"]');
    if (!box) return;
    if (!T.trimarr.loaded) await T.trimarr.load();
    const st = T.trimarr.status;
    if (!st.available) {
      T.setHTML(box, html`<div class="field-top"><span class="label label-soft">Trim ads in videos already in Plex</span></div>${ui.switch('trim', false, { disabled: true, text: 'Trimarr isn’t installed' })}`);
      return;
    }
    let on = false;
    try { on = !!(await T.trimarr.channel(ch.id)).enabled; } catch (e) { /* show off */ }
    T.setHTML(box, html`<div class="field-top"><span class="label label-soft">Trim ads in videos already in Plex</span><span class="def-note">Trimarr</span></div>
      ${ui.switch('trim', on, { text: on ? 'On for this channel' : 'Off', attrs: 'data-trim=""' })}
      <p class="hint">${icon('scissors')}${st.enabled ? 'Cuts sponsor reads out of this channel’s older videos using SponsorBlock. Each one can be undone.' : 'Trimarr is switched off in Settings, so nothing is trimmed until it’s on.'}</p>`);
  }
  async function toggleTrim(input) {
    const on = input.checked;
    try {
      await T.trimarr.setChannel(ch.id, on);
      const t = input.closest('.switch').querySelector('.switch-text'); if (t) t.textContent = on ? 'On for this channel' : 'Off';
      T.toast(on ? 'Trimarr will trim ads from ' + ch.title + '.' : 'Trimarr won’t touch ' + ch.title + '.');
    } catch (e) { input.checked = !on; T.toastError(e); }
  }
  function renderHero() {
    const h = root.querySelector('[data-slot="hero"]');
    if (h) T.setHTML(h, hero());
    const b = root.querySelector('[data-slot="banners"]');
    if (b) T.setHTML(b, banners());
    document.title = ch.title + ' · Tubarr';
  }

  /* ---------- Series (playlists detected as real series) ---------- */
  let series = null;
  async function loadSeries() {
    try { series = await T.api.get('/api/channels/' + encodeURIComponent(ch.id) + '/series'); } catch (e) { series = null; }
    renderSeries();
  }
  function renderSeries() {
    const box = root.querySelector('[data-slot="series"]');
    if (!box) return;
    if (!series || (!series.series.length && !series.ignored.length)) { T.setHTML(box, ''); return; }
    const range = (s) => (s.first_date ? T.fmtDay(s.first_date, true) + (s.last_date && s.last_date !== s.first_date ? ' – ' + T.fmtDay(s.last_date, true) : '') : '');
    T.setHTML(box, html`<section class="card series-card">
      <header class="card-head"><h2>${icon('layers')}Series${series.series.length ? html`<span class="count-pill">${series.series.length}</span>` : ''}</h2><span class="card-sub">Playlists that tell one story, shown as their own series in Plex</span></header>
      ${series.series.length ? html`<ul class="series-list">${series.series.map((s) => html`<li class="series-row ${s.enabled ? '' : 'is-off'}" data-pl="${s.id}">
          <span class="series-ic">${icon('layers')}</span>
          <span class="series-main"><b>${s.title}</b><small>${T.plural(s.episode_count, 'episode')} · ${s.in_plex_count} in Plex${range(s) ? ' · ' + range(s) : ''}</small></span>
          ${ui.select('order-' + s.id, [['playlist', 'Playlist order'], ['upload', 'Upload order']], s.order, { label: 'Episode order', attrs: 'data-series-order=""', cls: 'select-sm' })}
          ${ui.switch('series-' + s.id, s.enabled, { label: 'Show as a series in Plex', text: 'Show as a series in Plex', attrs: 'data-series-on=""' })}
        </li>`)}</ul>` : html`<p class="card-lead">No playlist on this channel looks like a series.</p>`}
      ${series.ignored.length ? html`<details class="ignored"><summary>${icon('chev-right')}${T.plural(series.ignored.length, 'playlist')} ignored</summary>
        <ul class="ignored-list">${series.ignored.map((p) => html`<li data-pl="${p.id}"><span class="ignored-main"><b>${p.title}</b><small>${T.plural(p.video_count, 'video')} · ${ui.PLAYLIST_WHY[p.reason] || p.reason}</small></span><button class="link-btn" type="button" data-act="series-promote">Use as a series anyway</button></li>`)}</ul></details>` : ''}
    </section>`);
  }
  async function patchSeries(id, body, msg) {
    try {
      await T.api.patch('/api/channels/' + encodeURIComponent(ch.id) + '/series/' + encodeURIComponent(id), body);
      if (msg) T.toast(msg);
      await loadSeries(); loadVideos();
    } catch (e) { T.toastError(e); loadSeries(); }
  }

  async function loadVideos() {
    const r = await T.api.get('/api/channels/' + encodeURIComponent(ch.id) + '/videos');
    videos = T.cacheVideos(r.videos);
    renderEpisodes();
  }

  /* ---------- Lifecycle ---------- */
  V.enter = async function (el, route) {
    root = el; st = { group: 'all', q: '' }; collapsed = new Set(); ch = null; videos = [];
    T.setHTML(root, html`<div class="page page-channel"><div class="ch-hero skel-hero"><div class="ch-hero-inner"><div class="ch-hero-main"><div class="poster poster-lg skel"></div><div class="skel-lines"><span class="skel skel-line"></span><span class="skel skel-line"></span><span class="skel skel-line short"></span></div></div></div></div></div>`);
    let settings;
    try {
      [ch, settings] = await Promise.all([T.api.get('/api/channels/' + encodeURIComponent(route.id)), T.api.get('/api/settings').catch(() => null)]);
      defaults = settings && settings.downloads;
    } catch (e) {
      T.setHTML(root, html`<div class="page">${e.status === 404 ? ui.empty({ icon: 'tv', title: 'That channel isn’t in Tubarr', body: 'It may have been removed.', actions: html`<a class="btn" href="#/channels">${icon('arrow-left')}All channels</a>` }) : ui.errorState(e)}</div>`);
      return;
    }
    T.setHTML(root, html`<div class="page page-channel">
      <div data-slot="hero"></div>
      <div data-slot="banners"></div>
      <div class="tabs-narrow" role="tablist"><button class="tab-n active" type="button" data-tab="eps" role="tab" aria-selected="true">Videos</button><button class="tab-n" type="button" data-tab="side" role="tab" aria-selected="false">Rules &amp; space</button></div>
      <div class="ch-body" data-show="eps">
        <section class="ch-eps">
          <div data-slot="series"></div>
          <div class="toolbar toolbar-tight">
            <label class="search-field">${icon('search')}<input type="search" data-f="q" placeholder="Search this channel" aria-label="Search this channel's videos"></label>
            <div class="chips" data-slot="ep-chips" role="group" aria-label="Filter"></div>
            <div class="toolbar-right"><button class="btn btn-ghost btn-sm select-toggle" type="button" data-act="select-mode">${icon('check-circle')}Select</button></div>
          </div>
          <div data-slot="eps"><div class="skel-rows">${Array.from({ length: 5 }, () => html`<div class="vrow skel-row"><span class="thumb skel"></span><span class="skel-lines"><span class="skel skel-line"></span><span class="skel skel-line short"></span></span></div>`)}</div></div>
        </section>
        <aside class="ch-side" data-slot="side" aria-label="Rules for this channel"></aside>
      </div>
    </div>`);
    renderHero(); renderSide();
    root.querySelector('[data-f="q"]').addEventListener('input', T.debounce((e) => { st.q = e.target.value.trim(); renderEpisodes(); }, 150));
    root.querySelector('.page-channel').addEventListener('click', (e) => {
      const g = e.target.closest('[data-group]');
      if (g) { st.group = g.dataset.group; renderEpisodes(); return; }
      const t = e.target.closest('[data-tab]');
      if (t) { root.querySelector('.ch-body').dataset.show = t.dataset.tab; root.querySelectorAll('[data-tab]').forEach((b) => { b.classList.toggle('active', b === t); b.setAttribute('aria-selected', String(b === t)); }); }
    });
    const mine = (v) => v && ch && v.channel_id === ch.id;
    unsubs = [
      T.on('ev:video.added', (d) => { if (!mine(d.video)) return; videos.unshift(d.video); videos.sort((a, b) => Date.parse(b.published_at) - Date.parse(a.published_at)); T.cacheVideos([d.video]); renderEpisodes(); const r = root.querySelector(`.vrow[data-vid="${CSS.escape(d.video.id)}"]`); if (r) r.classList.add('is-new'); }),
      T.on('ev:video.state', (d) => { if (!mine(d.video)) return; const i = videos.findIndex((x) => x.id === d.video.id); if (i >= 0) videos[i] = d.video; else videos.unshift(d.video); if (st.group !== 'all') renderEpisodes(); else ui.patchRows(d.video, { channel: false, when: 'date' }); }),
      T.on('ev:video.progress', (p) => ui.patchProgress(p)),
      T.on('ev:channel.updated', (d) => { if (d.channel.id !== ch.id) return; const was = ch.status; Object.assign(ch, d.channel); renderHero(); if (was === 'syncing' && ch.status !== 'syncing') loadVideos(); }),
      T.on('ev:channel.removed', (d) => { if (d.id === ch.id) { T.toast(ch.title + ' was removed.', { type: 'info' }); T.app.navigate('#/channels'); } }),
    ];
    tick = setInterval(() => { if (ch && ch.status === 'pending_removal') { const b = root.querySelector('[data-slot="banners"]'); if (b) T.setHTML(b, banners()); } }, 30000);
    root.querySelector('[data-slot="series"]').addEventListener('change', (e) => {
      const row = e.target.closest('[data-pl]'); if (!row) return;
      if (e.target.dataset.seriesOn != null) patchSeries(row.dataset.pl, { enabled: e.target.checked }, e.target.checked ? 'Shown as a series in Plex.' : 'Back to the channel’s year seasons in Plex.');
      if (e.target.dataset.seriesOrder != null) patchSeries(row.dataset.pl, { order: e.target.value }, 'Episode order updated in Plex.');
    });
    loadSeries();
    try { await loadVideos(); } catch (e) { T.setHTML(root.querySelector('[data-slot="eps"]'), ui.errorState(e)); }
  };
  V.leave = function () { unsubs.forEach((u) => u()); unsubs = []; clearInterval(tick); T.bulk.clear(); document.body.classList.remove('select-mode'); };
  V.refresh = () => { if (ch) loadVideos().catch(() => {}); };
  V.act = function (name, el) {
    const id = ch && encodeURIComponent(ch.id);
    switch (name) {
      case 'refresh':
        T.api.post('/api/channels/' + id + '/refresh').then((r) => { T.toast(r.message || 'Checking for new uploads.'); ch.status = 'syncing'; renderHero(); }).catch(T.toastError);
        return;
      case 'repolish':
        T.api.post('/api/channels/' + id + '/repolish').then((r) => T.toast(r.message || 'Re-polishing in Plex.')).catch(T.toastError);
        return;
      case 'edit':
        T.openChannelEdit(ch, (res) => { ch = res; renderHero(); });
        return;
      case 'restore':
        T.api.post('/api/channels/' + id + '/restore').then((c) => { ch = c; renderHero(); T.toast('Kept ' + c.title + '. Nothing will be removed.'); }).catch(T.toastError);
        return;
      case 'remove': removeChannel(); return;
      case 'season': {
        const sec = el.closest('.season'), y = sec.dataset.season;
        if (collapsed.has(y)) collapsed.delete(y); else collapsed.add(y);
        sec.classList.toggle('is-collapsed'); el.setAttribute('aria-expanded', String(!collapsed.has(y)));
        return;
      }
      case 'series-promote': { const row = el.closest('[data-pl]'); patchSeries(row.dataset.pl, { enabled: true }, 'Now shown as a series in Plex.'); return; }
      case 'clear-eps': st.group = 'all'; st.q = ''; root.querySelector('[data-f="q"]').value = ''; renderEpisodes(); return;
      case 'sb-default': saveRule({ sponsorblock: null }).then(renderSide); return;
      case 'select-mode': { const on = !document.body.classList.contains('select-mode'); document.body.classList.toggle('select-mode', on); el.classList.toggle('active', on); if (!on) T.bulk.clear(); return; }
      default: return false;
    }
  };
  async function removeChannel() {
    const stt = ch.stats || {}, kept = stt.kept_count || 0, gone = stt.gone_count || 0, special = kept + gone;
    const ok = await T.confirm({
      title: 'Remove ' + ch.title + '?', danger: true, confirm: 'Remove channel',
      body: html`<p>Tubarr stops downloading it now. In <b>3 days</b> its ${T.plural(ch.video_count, 'video')} (${T.fmtBytes(ch.size_bytes)}) and the show leave Plex. You can undo any time before then.${special ? ' Videos kept forever or removed from YouTube stay unless you delete everything right away.' : ''}</p>
        <label class="check-row"><input type="checkbox" name="immediate"><span>Delete everything right away instead${special ? html`, <b>including ${[kept ? T.plural(kept, 'video') + ' kept forever' : '', gone ? T.plural(gone, 'video') + ' removed from YouTube (they can’t be downloaded again)' : ''].filter(Boolean).join(' and ')}</b>` : ''}</span></label>`,
    });
    if (!ok) return;
    try {
      const r = await T.api.del('/api/channels/' + encodeURIComponent(ch.id), { immediate: !!ok.immediate });
      T.app.invalidateChannels();
      if (r.deleted) { T.toast(ch.title + ' was deleted.'); T.app.navigate('#/channels'); return; }
      ch = r; renderHero();
      T.toast(ch.title + ' will be removed in 3 days.', { type: 'info', action: { label: 'Undo', fn: () => V.act('restore') } });
    } catch (e) { T.toastError(e); }
  }
})();
