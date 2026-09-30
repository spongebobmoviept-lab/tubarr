/* Channels: the poster wall (or a table), with search, filters, sort and live updates. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon, ui = T.ui;

  const FILTERS = [
    ['all', 'All', () => true],
    ['monitored', 'Monitored', (c) => c.status === 'monitored'],
    ['syncing', 'Syncing', (c) => c.status === 'syncing'],
    ['pending_removal', 'Pending removal', (c) => c.status === 'pending_removal'],
    ['error', 'Error', (c) => c.status === 'error'],
    ['gone', 'Gone from YouTube', (c) => c.status === 'gone'],
    ['forever', 'Keep forever', (c) => c.retention_mode === 'forever'],
    ['custom', 'Own rules', (c) => c.retention_mode && c.retention_mode !== 'fill'],
  ];
  const SORTS = [
    ['name', 'Name', (a, b) => a.title.localeCompare(b.title)],
    ['updated', 'Recently updated', (a, b) => (Date.parse(b.last_download_at) || 0) - (Date.parse(a.last_download_at) || 0)],
    ['size', 'Size on disk', (a, b) => b.size_bytes - a.size_bytes],
    ['videos', 'Videos', (a, b) => b.video_count - a.video_count],
    ['subscribers', 'Subscribers', (a, b) => (b.subscribers || 0) - (a.subscribers || 0)],
  ];
  const MODE_TAG = { count: (c) => 'Own rule', days: () => 'Own rule', since: () => 'Own rule', forever: () => 'Forever', new_only: () => 'New only' };

  const V = (T.views.channels = { nav: 'channels', title: 'Channels' });
  let root, list = [], st, unsubs = [], timer = null;

  function prefs() { return { q: '', filter: 'all', topic: '', sort: T.prefs.get('ch.sort', 'name'), view: T.prefs.get('ch.view', 'posters'), group: T.prefs.get('ch.group', false) }; }
  function visible() {
    const f = FILTERS.find((x) => x[0] === st.filter) || FILTERS[0];
    const s = SORTS.find((x) => x[0] === st.sort) || SORTS[0];
    const q = st.q.toLowerCase();
    return list.filter((c) => f[2](c) && (!st.topic || c.topic === st.topic) && (!q || (c.title + ' ' + c.handle).toLowerCase().includes(q))).sort(s[2]);
  }

  function card(c, i) {
    const tag = MODE_TAG[c.retention_mode];
    const sp = c.sync_progress;
    return html`<article class="ch-card st-${c.status}" data-cid="${c.id}" style="--i:${Math.min(i, 30)}">
      <a class="ch-link" href="#/channel/${encodeURIComponent(c.id)}">
        <div class="poster">${T.posterArt(c)}${c.poster_url ? html`<img src="${c.poster_url}" alt="" loading="lazy" decoding="async" data-fallback>` : ''}
          <span class="ch-badge">${ui.channelBadge(c, { compact: true })}</span>
          ${tag || c.failed_count ? html`<span class="ch-tr">
            ${tag ? html`<span class="ch-mode ${c.retention_mode === 'forever' ? 'is-forever' : ''}" title="${T.retention.describe({ mode: c.retention_mode })}">${c.retention_mode === 'forever' ? icon('infinity') : ''}${tag(c)}</span>` : ''}
            ${c.failed_count ? html`<span class="ch-failed" title="${T.plural(c.failed_count, 'failed download')}">${icon('alert')}${c.failed_count}</span>` : ''}</span>` : ''}
          ${c.status === 'syncing' ? html`<span class="ch-sync">${ui.bar(sp && sp.total ? (sp.done || 0) / sp.total : null, 'bar-xs')}</span>` : ''}
        </div>
        <div class="ch-meta">
          <span class="ch-name">${c.title}</span>
          <span class="ch-stats">${c.status === 'syncing' && !c.video_count ? (sp && sp.total ? 'Reading uploads · ' + sp.done + ' of ' + sp.total : 'Reading uploads…') : html`${T.plural(c.video_count, 'video')}<span class="sep">·</span>${T.fmtBytes(c.size_bytes)}`}</span>
        </div>
      </a>
      ${c.status === 'pending_removal' && c.removal ? html`<div class="ch-pending"><span class="ch-pending-t">${icon('timer')}<span>Removing in <b>${T.tUntil(c.removal.delete_at, { fmt: 'span', zero: 'a moment' })}</b></span></span><button class="btn btn-xs btn-warn" type="button" data-act="undo" data-cid="${c.id}">${icon('undo')}Undo</button></div>` : ''}
    </article>`;
  }
  function tableRow(c) {
    return html`<tr data-cid="${c.id}" class="st-${c.status}">
      <td><a class="t-ch" href="#/channel/${encodeURIComponent(c.id)}">${T.poster(c, 'poster-xs')}<span><b>${c.title}</b><small>${c.handle}</small></span></a></td>
      <td>${ui.channelBadge(c)}${c.status === 'pending_removal' && c.removal ? html` <button class="link-btn" type="button" data-act="undo" data-cid="${c.id}">Undo</button>` : ''}</td>
      <td class="num">${T.fmtNum(c.video_count)}</td>
      <td class="num">${T.fmtBytes(c.size_bytes)}</td>
      <td class="hide-md">${ui.topicLabel(c.topic)}</td>
      <td class="hide-md">${T.retention.describe({ mode: c.retention_mode === 'fill' ? 'fill' : c.retention_mode })}</td>
      <td class="hide-sm">${T.tAgo(c.last_upload_at)}</td>
      <td class="hide-md">${T.tAgo(c.last_download_at)}</td>
      <td class="num hide-sm">${T.fmtCompact(c.subscribers)}</td>
    </tr>`;
  }
  function renderResults() {
    const box = root.querySelector('[data-slot="results"]');
    const vis = visible();
    if (!list.length) {
      T.setHTML(box, ui.empty({
        art: html`<span class="empty-mark">${T.logoMark()}</span>`, title: 'No channels yet',
        body: 'Import your YouTube subscriptions (a Google Takeout file, or a list of channel links), or add channels one at a time.',
        actions: html`<a class="btn btn-primary" href="#/settings/import">${icon('clipboard')}Import subscriptions</a><button class="btn" type="button" data-act="add">${icon('plus')}Add a channel</button>`,
      }));
      return;
    }
    if (!vis.length) {
      T.setHTML(box, ui.empty({ icon: 'search', title: st.q ? 'No channels match “' + st.q + '”' : 'No channels in this view', body: 'Try another filter or clear the search.', actions: html`<button class="btn" type="button" data-act="clear">${icon('x')}Clear</button>` }));
      return;
    }
    if (st.view === 'table') {
      const th = (k, label, cls) => html`<th class="${cls || ''}" scope="col"><button class="th-sort ${st.sort === k ? 'active' : ''}" type="button" data-sort="${k}">${label}${st.sort === k ? icon('chev-down') : ''}</button></th>`;
      T.setHTML(box, html`<div class="table-wrap"><table class="table ch-table">
        <thead><tr>${th('name', 'Channel')}<th scope="col">Status</th>${th('videos', 'Videos', 'num')}${th('size', 'On disk', 'num')}<th scope="col" class="hide-md">Topic</th><th scope="col" class="hide-md">Keeps</th><th scope="col" class="hide-sm">Latest upload</th>${th('updated', 'Updated', 'hide-md')}${th('subscribers', 'Subscribers', 'num hide-sm')}</tr></thead>
        <tbody>${vis.map(tableRow)}</tbody></table></div>`);
    } else if (st.group) {
      const groups = ui.TOPICS.map(([id, label]) => [id, label, vis.filter((c) => c.topic === id)]).filter((g) => g[2].length);
      let i = 0;
      T.setHTML(box, groups.map(([id, label, chs]) => html`<section class="topic-group" data-topic="${id}">
        <header class="topic-head"><h2>${label}</h2><span class="topic-sub">${T.plural(chs.length, 'channel')} · ${T.fmtBytes(chs.reduce((s, c) => s + c.size_bytes, 0))}</span></header>
        <div class="poster-grid">${chs.map((c) => card(c, i++))}</div></section>`));
    } else {
      T.setHTML(box, html`<div class="poster-grid">${vis.map(card)}</div>`);
    }
  }
  function renderChips() {
    const counts = Object.fromEntries(FILTERS.map(([k, , fn]) => [k, list.filter(fn).length]));
    T.setHTML(root.querySelector('[data-slot="chips"]'), FILTERS.filter(([k]) => k === 'all' || counts[k] > 0 || st.filter === k).map(([k, label]) =>
      html`<button class="chip ${st.filter === k ? 'active' : ''}" type="button" data-filter="${k}" aria-pressed="${st.filter === k}">${label}<span class="chip-n">${counts[k]}</span></button>`));
    const tot = list.reduce((a, c) => { a.v += c.video_count; a.b += c.size_bytes; return a; }, { v: 0, b: 0 });
    const sub = root.querySelector('[data-slot="sub"]');
    if (sub) sub.textContent = T.plural(list.length, 'channel') + ' · ' + T.plural(tot.v, 'video') + ' · ' + T.fmtBytes(tot.b, { nbsp: false });
  }
  async function load() {
    const r = await T.api.get('/api/channels');
    list = r.channels;
    renderChips(); renderResults();
  }
  function upsert(c) {
    const i = list.findIndex((x) => x.id === c.id);
    if (i >= 0) list[i] = c; else list.push(c);
    const el = root.querySelector(`[data-cid="${CSS.escape(c.id)}"]`);
    const stillVisible = visible().some((x) => x.id === c.id);
    if (el && stillVisible && st.view === 'posters' && el.tagName === 'ARTICLE') {
      const tmp = document.createElement('div');
      T.setHTML(tmp, card(c, 0));
      const n = tmp.firstElementChild; n.style.animation = 'none';
      el.replaceWith(n);
    } else if (el && stillVisible && st.view === 'table') {
      const tmp = document.createElement('tbody'); T.setHTML(tmp, tableRow(c)); el.replaceWith(tmp.firstElementChild);
    } else renderResults();
    renderChips();
  }

  V.enter = async function (el) {
    root = el; st = prefs();
    T.setHTML(root, html`<div class="page page-channels">
      <header class="page-head">
        <div class="page-title"><h1>Channels</h1><p class="page-sub" data-slot="sub">Loading…</p></div>
        <div class="page-actions">
          <a class="btn" href="#/settings/import">${icon('clipboard')}<span class="hide-sm">Import</span></a>
          <button class="btn btn-primary" type="button" data-act="add">${icon('plus')}<span>Add channel</span></button>
        </div>
      </header>
      <div class="toolbar">
        <label class="search-field">${icon('search')}<input type="search" data-f="q" placeholder="Search channels" aria-label="Search channels"></label>
        <div class="chips" role="group" aria-label="Filter" data-slot="chips"></div>
        <div class="toolbar-right">
          ${ui.select('topic', [['', 'All topics']].concat(ui.TOPICS), st.topic, { label: 'Topic', attrs: 'data-f="topic"' })}
          <button class="btn btn-ghost group-toggle ${st.group ? 'active' : ''}" type="button" data-act="group" aria-pressed="${st.group}">${icon('layers')}<span class="hide-sm">Group by topic</span></button>
          ${ui.select('sort', SORTS.map(([k, l]) => [k, l]), st.sort, { label: 'Sort', attrs: 'data-f="sort"' })}
          ${ui.seg('view', [['posters', '', 'posters'], ['table', '', 'rows']], st.view, { label: 'View', cls: 'seg-icons', attrs: 'data-f="view"' })}
        </div>
      </div>
      <div data-slot="results"><div class="poster-grid">${Array.from({ length: 16 }, () => html`<div class="ch-card"><div class="poster skel"></div><div class="ch-meta"><span class="skel skel-line"></span><span class="skel skel-line short"></span></div></div>`)}</div></div>
    </div>`);
    const seg = root.querySelectorAll('.seg-icons .seg-opt');
    if (seg[0]) seg[0].title = 'Posters';
    if (seg[1]) seg[1].title = 'Table';
    const qi = root.querySelector('[data-f="q"]');
    qi.addEventListener('input', T.debounce(() => { st.q = qi.value.trim(); renderResults(); }, 120));
    root.querySelector('.toolbar').addEventListener('change', (e) => {
      const k = e.target.dataset.f || (e.target.name === 'view' ? 'view' : null);
      if (k === 'sort') { st.sort = e.target.value; T.prefs.set('ch.sort', st.sort); renderResults(); }
      if (k === 'topic') { st.topic = e.target.value; renderResults(); }
      if (k === 'view' || e.target.name === 'view') { st.view = e.target.value; T.prefs.set('ch.view', st.view); renderResults(); }
    });
    root.querySelector('.page-channels').addEventListener('click', (e) => {
      const f = e.target.closest('[data-filter]');
      if (f) { st.filter = f.dataset.filter; renderChips(); renderResults(); return; }
      const s = e.target.closest('[data-sort]');
      if (s) { st.sort = s.dataset.sort; T.prefs.set('ch.sort', st.sort); const sel = root.querySelector('[data-f="sort"]'); if (sel) sel.value = st.sort; renderResults(); }
    });
    unsubs = [
      T.on('ev:channel.updated', (d) => upsert(d.channel)),
      T.on('ev:channel.added', (d) => upsert(d.channel)),
      T.on('ev:channel.removed', (d) => { list = list.filter((c) => c.id !== d.id); renderChips(); renderResults(); }),
    ];
    // countdowns on pending removals tick with the shared 1 s ticker (tick.js)
    try { await load(); } catch (e) { T.setHTML(root.querySelector('[data-slot="results"]'), ui.errorState(e)); }
  };
  V.leave = function () { unsubs.forEach((u) => u()); unsubs = []; clearInterval(timer); };
  V.refresh = () => load().catch(() => {});
  V.act = function (name, el) {
    if (name === 'add') { T.openAddChannel(); return; }
    if (name === 'group') { st.group = !st.group; T.prefs.set('ch.group', st.group); el.classList.toggle('active', st.group); el.setAttribute('aria-pressed', String(st.group)); if (st.group && st.view === 'table') { st.view = 'posters'; const r = root.querySelector('input[name="view"][value="posters"]'); if (r) r.checked = true; } renderResults(); return; }
    if (name === 'clear') { st.q = ''; st.filter = 'all'; st.topic = ''; const ts = root.querySelector('[data-f="topic"]'); if (ts) ts.value = ''; const qi = root.querySelector('[data-f="q"]'); if (qi) qi.value = ''; renderChips(); renderResults(); return; }
    if (name === 'undo') {
      const id = el.dataset.cid;
      el.disabled = true;
      T.api.post('/api/channels/' + encodeURIComponent(id) + '/restore').then((c) => { upsert(c); T.toast('Kept ' + c.title + '. Nothing will be removed.'); }).catch((e) => { el.disabled = false; T.toastError(e); });
      return;
    }
    return false;
  };
})();
