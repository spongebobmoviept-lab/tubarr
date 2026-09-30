/* Tubarr web UI: the shared video row (Timeline, channel page, Activity), its live patching, row actions,
   and multi-select bulk actions. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon, ui = T.ui;

  /* Every video a screen shows is cached by id so actions and live patches know its current shape. */
  const cache = (T.vcache = new Map());
  T.cacheVideos = (list) => { list.forEach((v) => cache.set(v.id, v)); return list; };

  ui.REMOVED_CHIP = { watched: 'Removed · watched', making_room: 'Removed · making room', retention: 'Removed · outside window', manual: 'Removed · deleted', unavailable: 'Removed · gone from YouTube' };
  ui.REMOVED_WHY = {
    watched: 'The library was full: watched videos go first, oldest watched first',
    making_room: 'The library was full: the oldest backlog video from the biggest channel made room',
    retention: "It fell outside this channel's own window", manual: 'You deleted it', unavailable: 'It was removed from YouTube',
  };
  ui.PROTECTED = { keep: 'Kept forever', channel: 'This channel is kept forever', newest: "One of the channel's newest 3: never rolled out", gone: 'Removed from YouTube, so Tubarr keeps it forever' };
  ui.GONE_WHY = { deleted: 'deleted by the uploader', private: 'made private', terminated: 'channel terminated', unavailable: 'no longer available' };
  /* A fresh new upload (not backlog) gets a small NEW marker so it reads differently from backlog fill. */
  const isFresh = (v) => !v.backfill && ['downloaded', 'downloading', 'queued', 'waiting_sponsorblock'].includes(v.state) && Date.now() - Date.parse(v.published_at) < 3 * 864e5;

  /* The status chip, in plain words. */
  ui.videoState = function (v) {
    if (v.state === 'downloaded' && v.youtube_gone) return html`<span class="chip-s s-gone" title="${'Gone from YouTube ' + T.fmtDay(v.youtube_gone.at, true) + ' (' + (ui.GONE_WHY[v.youtube_gone.reason] || v.youtube_gone.reason) + '). Tubarr keeps it forever.'}">${icon('archive')}Removed from YouTube · kept</span>`;
    switch (v.state) {
      case 'downloaded': return html`<span class="chip-s s-good">${icon('check')}In Plex</span>`;
      case 'downloading': {
        if (!v.stage || v.stage === 'download') {
          const p = v.progress || 0;
          return html`<span class="chip-s s-brand">${ui.ring(p)}Downloading <b class="num" data-live="pct">${Math.floor(p * 100)}%</b></span>`;
        }
        return html`<span class="chip-s s-brand"><span class="spin" aria-hidden="true"></span>${ui.STAGES[v.stage] || 'Processing'}</span>`;
      }
      case 'queued': return v.download_now ? html`<span class="chip-s s-brand" title="Download now: it starts as soon as nothing else is downloading"><span class="spin" aria-hidden="true"></span>Starting…</span>` : html`<span class="chip-s s-muted">${icon('clock')}Queued</span>`;
      case 'waiting_sponsorblock': return html`<span class="chip-s s-info" title="${v.wait_until ? 'Tubarr waits until ' + T.fmtTime(v.wait_until).replace(' ', ' ') + ' at most' : ''}">${icon('scissors')}Waiting for SponsorBlock</span>`;
      case 'upcoming': return html`<span class="chip-s s-info">${icon('calendar')}Premieres ${T.tAgo(v.published_at)}</span>`;
      case 'skipped_short': return html`<span class="chip-s s-dim">${icon('short')}Skipped (Short)</span>`;
      case 'skipped_live': return html`<span class="chip-s s-dim">${icon('live')}Skipped (livestream)</span>`;
      case 'skipped_too_long': return html`<span class="chip-s s-dim">${icon('hourglass')}Skipped (too long)</span>`;
      case 'skipped_members': return html`<span class="chip-s s-dim" title="${v.detail || 'Members only'}">${icon('lock')}Skipped (members only)</span>`;
      case 'failed': return html`<span class="chip-s s-crit">${icon('alert')}Failed</span>`;
      case 'removed': return html`<span class="chip-s s-dim" title="${ui.REMOVED_WHY[v.removed_reason] || ''}">${icon('trash')}${ui.REMOVED_CHIP[v.removed_reason] || 'Removed'}</span>`;
      default: return html`<span class="chip-s s-dim">${v.state}</span>`;
    }
  };

  /* "Download now": the very next download, skipping the pause and the daily limit (still one at
     a time: if something is downloading, it starts right after it). Anything not in Plex yet can be pressed. */
  const NOW_STATES = ['queued', 'failed', 'skipped_live', 'skipped_too_long', 'removed'];
  ui.canDownloadNow = (v) => !!v && NOW_STATES.includes(v.state) && !v.download_now && !v.youtube_gone && (v.state !== 'failed' || !v.error || v.error.retryable);
  ui.nowButton = (cls) => html`<button class="btn ${cls || 'btn-xs'} btn-now" type="button" data-act="v-now" title="Start this as the very next download: skips the pause and the daily limit (still one at a time)">${icon('download')}Download now</button>`;

  /* The one inline action a row offers next to its chip. */
  function inlineAction(v) {
    if (v.state === 'failed' && (!v.error || v.error.retryable)) return html`<button class="btn btn-xs btn-crit-soft" type="button" data-act="v-retry">${icon('refresh')}Retry</button>`;
    if (['queued', 'skipped_live', 'skipped_too_long'].includes(v.state) && ui.canDownloadNow(v)) return ui.nowButton();
    return '';
  }

  const fmtCut = (s) => T.fmtClock(s);
  /* opts: { channel: true (show the channel name), select: true (checkbox), when: 'date'|'time', fresh: true } */
  ui.videoRow = function (v, o) {
    o = o || {};
    cache.set(v.id, v);
    const cls = ['vrow', 'st-' + v.state, v.keep ? 'is-kept' : '', o.fresh ? 'is-new' : '', o.selected ? 'is-selected' : ''].join(' ');
    const when = o.when === 'activity' ? T.fmtTime(v.activity_at || v.published_at) : o.when === 'time' ? T.fmtTime(v.published_at) : T.fmtDay(v.upload_date);
    const bfTag = o.when === 'activity' && v.backfill ? html`<span class="bf-tag" title="An older upload, fetched to fill the library">${icon('history')}Backlog · ${T.fmtDay(v.upload_date, true)}</span>` : '';
    const prot = v.protected && v.protected !== 'keep' ? html`<span class="prot" title="${ui.PROTECTED[v.protected]}">${icon(v.protected === 'channel' ? 'infinity' : 'shield')}</span>` : '';
    const prog = v.state === 'downloading' ? html`<span class="thumb-prog${v.stage && v.stage !== 'download' ? ' is-ind' : ''}"><i data-live="tbar" style="width:${((v.progress || 0) * 100).toFixed(1)}%"></i></span>` : '';
    return html`<div class="${cls}" data-vid="${v.id}">
      ${o.select !== false ? html`<label class="vrow-check"><input type="checkbox" data-sel ${o.selected ? T.raw('checked') : ''} aria-label="Select ${v.title}"><span aria-hidden="true">${icon('check')}</span></label>` : ''}
      <button class="vrow-thumb" type="button" data-act="v-open" aria-label="Open ${v.title}">${T.thumb(v, '', html`<span class="dur">${v.state === 'upcoming' ? 'Soon' : T.fmtClock(v.duration_seconds)}</span>${prog}`)}</button>
      <div class="vrow-main">
        <button class="vrow-title" type="button" data-act="v-open">${isFresh(v) ? html`<span class="new-mark" title="A new upload">NEW</span>` : ''}${v.title}${v.edited ? icon('lock', 'vrow-lock') : ''}</button>
        <div class="vrow-meta">
          ${o.channel !== false ? html`<a class="vrow-ch" href="#/channel/${encodeURIComponent(v.channel_id)}">${v.channel_title}</a><span class="sep">·</span>` : ''}
          <span>${when}</span>${bfTag ? html`<span class="sep">·</span>${bfTag}` : ''}<span class="sep">·</span><span class="num">${v.state === 'upcoming' ? '—' : T.fmtClock(v.duration_seconds)}</span>
          ${v.size_bytes ? html`<span class="sep">·</span><span class="num ${v.state === 'downloaded' ? '' : 'is-est'}" title="${v.state === 'downloaded' ? 'On disk' : 'Estimated'}">${v.state === 'downloaded' ? '' : '~'}${T.fmtBytes(v.size_bytes)}</span>` : ''}
          ${v.sponsorblock_cut_seconds > 0 ? html`<span class="sb-chip" title="SponsorBlock removed ${T.fmtSpan(v.sponsorblock_cut_seconds)}">${icon('scissors')}SponsorBlock cut ${fmtCut(v.sponsorblock_cut_seconds)}</span>` : ''}
          ${v.trim && v.trim.state === 'trimmed' ? html`<span class="trim-chip" title="Trimarr removed ${T.fmtSpan(v.trim.removed_seconds)} of ads from the file in Plex">${icon('scissors')}Trimmed −${fmtCut(v.trim.removed_seconds)}</span>` : v.trim && v.trim.state === 'queued' ? html`<span class="trim-chip is-queued">${icon('scissors')}Trimming…</span>` : ''}
          ${v.series ? html`<span class="series-chip" title="Part ${v.series.part} of the series “${v.series.title}” in Plex">${icon('layers')}${v.series.title} · Part ${v.series.part}</span>` : ''}
          ${v.keep ? html`<span class="keep-chip" title="Kept forever: never removed automatically">${icon('infinity')}Kept</span>` : ''}${prot}
          ${v.watched && v.state === 'downloaded' ? html`<span class="watched-chip" title="Watched in Plex">${icon('eye')}Watched</span>` : ''}
        </div>
        ${v.state === 'failed' && v.error ? html`<div class="vrow-error">${icon('alert-circle')}<span>${v.error.message}</span></div>` : ''}
        ${v.youtube_gone ? html`<div class="vrow-gone">${icon('archive')}<span>Gone from YouTube ${T.fmtDay(v.youtube_gone.at, true)} · ${ui.GONE_WHY[v.youtube_gone.reason] || v.youtube_gone.reason}${v.state === 'downloaded' ? ' · kept in Plex forever' : ''}</span></div>` : ''}
      </div>
      <div class="vrow-state">${ui.videoState(v)}${inlineAction(v)}</div>
      <button class="btn-icon vrow-more" type="button" data-act="v-menu" aria-label="Actions for ${v.title}" aria-haspopup="menu">${icon('more')}</button>
    </div>`;
  };

  /* Replace every rendered row of a video (the same video can be on screen twice, e.g. Activity queue + history). */
  ui.patchRows = function (v, o) {
    cache.set(v.id, v);
    T.$$(`.vrow[data-vid="${CSS.escape(v.id)}"]`).forEach((el) => {
      const selected = el.classList.contains('is-selected');
      const opts = Object.assign({}, JSON.parse(el.dataset.opts || '{}'), o || {}, { selected });
      const tmp = document.createElement('div');
      T.setHTML(tmp, ui.videoRow(v, opts));
      const n = tmp.firstElementChild;
      n.dataset.opts = el.dataset.opts || '{}';
      el.replaceWith(n);
    });
  };
  /* Cheap progress patch for video.progress events. */
  ui.patchProgress = function (p) {
    const v = cache.get(p.id);
    if (v) { v.progress = p.progress; if (v.stage !== p.stage) { v.stage = p.stage; ui.patchRows(v); return; } }
    T.$$(`.vrow[data-vid="${CSS.escape(p.id)}"]`).forEach((el) => {
      const pct = el.querySelector('[data-live="pct"]'); if (pct) pct.textContent = Math.floor((p.progress || 0) * 100) + '%';
      ui.setRing(el.querySelector('[data-live="ring"]'), p.progress || 0);
      const bar = el.querySelector('[data-live="tbar"]'); if (bar) bar.style.width = ((p.progress || 0) * 100).toFixed(1) + '%';
    });
  };

  /* ---------- Row actions ---------- */
  const rowVideo = (el) => { const row = el.closest('[data-vid]'); return row ? cache.get(row.dataset.vid) || { id: row.dataset.vid } : null; };
  async function run(promise, okMsg) {
    try { const r = await promise; if (okMsg) T.toast(typeof okMsg === 'function' ? okMsg(r) : okMsg); return r; } catch (e) { T.toastError(e); return null; }
  }
  /* What's special about videos the user is about to delete by hand (they always CAN delete them). */
  ui.deleteNotes = function (vs) {
    const keep = vs.filter((v) => v.keep).length, gone = vs.filter((v) => v.youtube_gone).length;
    const newest = vs.filter((v) => v.protected === 'newest').length, chan = vs.filter((v) => v.protected === 'channel').length;
    const one = vs.length === 1, n = (k, a, b) => (one ? a : T.plural(k, 'video') + ' ' + b);
    const notes = [];
    if (gone) notes.push(html`<div class="note note-warn">${icon('archive')}<span><b>${n(gone, 'Removed from YouTube.', 'are removed from YouTube.')}</b> Tubarr normally keeps ${one ? 'it' : 'them'} forever. Once deleted, ${one ? 'it' : 'they'} can’t be downloaded again.</span></div>`);
    if (keep) notes.push(html`<div class="note note-warn">${icon('infinity')}<span><b>${n(keep, 'Kept forever.', 'are kept forever.')}</b> Deleting overrides that.</span></div>`);
    if (chan) notes.push(html`<div class="note note-info">${icon('lock')}<span>${n(chan, 'Its channel is set to keep everything.', 'come from channels set to keep everything.')} Deleting overrides that.</span></div>`);
    if (newest) notes.push(html`<div class="note note-info">${icon('lock')}<span>${n(newest, 'One of its channel’s newest videos, which are never rolled out on their own.', 'are among their channel’s newest, which are never rolled out on their own.')}</span></div>`);
    return notes;
  };
  const acts = {
    async 'v-retry'(v) { const r = await run(T.api.post('/api/videos/' + v.id + '/download', {}), 'Queued again: ' + v.title); if (r) ui.patchRows(r); },
    async 'v-download'(v, next) { const r = await run(T.api.post('/api/videos/' + v.id + '/download', { next: !!next }), next ? 'First in line: ' + v.title : 'Queued: ' + v.title); if (r) ui.patchRows(r); },
    async 'v-now'(v) {
      T.$$(`[data-vid="${CSS.escape(v.id)}"] [data-act="v-now"]`).forEach((b) => { b.disabled = true; });
      const s = (T.live && T.live.status) || T.api.firstStatus, d = s && s.downloader;
      const busy = d && T.dlJobs(d).some((j) => j.stage === 'download');
      const msg = d && d.paused ? 'Downloads are paused: “' + v.title + '” starts first when you resume.'
        : busy ? '“' + v.title + '” is next: it starts as soon as the current download finishes.' : 'Starting now: ' + v.title;
      const r = await run(T.api.post('/api/videos/' + v.id + '/download', { now: true }), msg);
      if (r) { ui.patchRows(r); if (T.app.view && T.app.view.refresh) T.app.view.refresh(); }
      else T.$$(`[data-vid="${CSS.escape(v.id)}"] [data-act="v-now"]`).forEach((b) => { b.disabled = false; });
    },
    async 'v-keep'(v) { const r = await run(T.api.patch('/api/videos/' + v.id, { keep: true }), 'Kept forever: ' + v.title); if (r) ui.patchRows(r); },
    async 'v-unkeep'(v) { const r = await run(T.api.patch('/api/videos/' + v.id, { keep: false }), 'No longer kept forever'); if (r) ui.patchRows(r); },
    async 'v-delete'(v) {
      const inPlex = v.state === 'downloaded';
      const ok = await T.confirm({
        title: inPlex ? 'Delete this video now?' : "Don't download this video?", danger: true, confirm: inPlex ? 'Delete now' : "Don't download",
        body: html`<p class="confirm-lead">${v.title}</p><p>${inPlex ? html`The file (${T.fmtBytes(v.size_bytes)}) is deleted and the episode leaves Plex straight away.` : 'It leaves the queue. You can download it again later from its row.'}</p>${inPlex ? ui.deleteNotes([v]) : ''}`,
      });
      if (!ok) return;
      const r = await run(T.api.del('/api/videos/' + v.id), inPlex ? 'Deleted: ' + v.title : 'Removed from the queue');
      if (r) ui.patchRows(r);
    },
    async 'v-trim'(v) {
      const r = await run(T.trimarr.trim(v.id), (x) => x.state === 'trimmed' ? 'Trimmed ' + T.fmtClock(x.removed_seconds) + ' of ads from “' + v.title + '”' : 'Trimarr is trimming “' + v.title + '”');
      if (r) { v.trim = { state: r.state, removed_seconds: r.removed_seconds }; ui.patchRows(v); }
    },
    async 'v-untrim'(v) {
      const r = await run(T.trimarr.undo(v.id), 'Restored the original video');
      if (r) { v.trim = null; ui.patchRows(v); }
    },
    'v-open'(v) { T.openVideo(v.id); },
    'v-edit'(v) { T.openVideo(v.id, { edit: true }); },
    'v-menu'(v, el) {
      const s = v.state;
      const items = [];
      if (s === 'failed' && (!v.error || v.error.retryable)) items.push({ label: 'Retry', icon: 'refresh', fn: () => acts['v-retry'](v) });
      if (s === 'skipped_live' || s === 'skipped_too_long') items.push({ label: 'Download anyway', icon: 'download', fn: () => acts['v-download'](v) });
      if (s === 'waiting_sponsorblock') items.push({ label: "Download now (don't wait)", icon: 'download', fn: () => acts['v-download'](v, true) });
      if (ui.canDownloadNow(v)) items.push({ label: 'Download now', icon: 'download', fn: () => acts['v-now'](v) });
      if (s === 'queued' && !v.download_now) items.push({ label: 'Download first (next in line, normal pace)', icon: 'list', fn: () => acts['v-download'](v, true) });
      if (s === 'removed') items.push({ label: 'Download again', icon: 'download', fn: () => acts['v-download'](v) });
      if (s === 'downloaded') items.push(v.keep ? { label: 'Stop keeping forever', icon: 'infinity', fn: () => acts['v-unkeep'](v) } : { label: 'Keep forever', icon: 'infinity', fn: () => acts['v-keep'](v) });
      if (s === 'downloaded' && T.trimarr && T.trimarr.available()) items.push(v.trim && v.trim.state === 'trimmed' ? { label: 'Undo trim (restore the original)', icon: 'undo', fn: () => acts['v-untrim'](v) } : { label: 'Trim ads (Trimarr)', icon: 'scissors', fn: () => acts['v-trim'](v) });
      items.push({ label: 'Edit info', icon: 'wand', fn: () => acts['v-edit'](v) });
      if (items.length) items.push('-');
      if (v.plex_url) items.push({ label: 'Open in Plex', icon: 'film', href: v.plex_url, external: true });
      if (!v.youtube_gone) items.push({ label: 'Open on YouTube', icon: 'ext', href: v.youtube_url || 'https://www.youtube.com/watch?v=' + v.id, external: true });
      if (s === 'downloaded' || s === 'queued' || s === 'waiting_sponsorblock' || s === 'failed') { items.push('-'); items.push({ label: s === 'downloaded' ? 'Delete now' : "Don't download", icon: 'trash', danger: true, fn: () => acts['v-delete'](v) }); }
      T.menu(el, items);
    },
  };
  T.videoActs = {
    handle(name, el) {
      if (!acts[name]) return false;
      const v = rowVideo(el);
      if (!v) return false;
      acts[name](v, el);
      return true;
    },
    run: (name, v, el) => acts[name](v, el),
  };

  /* ---------- Multi-select + bulk bar ---------- */
  const bulk = (T.bulk = { sel: new Set(), scope: null });
  function bar() {
    let b = T.$('#bulkbar');
    if (!b) { b = document.createElement('div'); b.id = 'bulkbar'; b.className = 'bulkbar'; b.setAttribute('role', 'region'); b.setAttribute('aria-label', 'Bulk actions'); document.body.appendChild(b); }
    return b;
  }
  bulk.render = function () {
    const b = bar(), n = bulk.sel.size;
    b.classList.toggle('open', n > 0);
    document.body.classList.toggle('has-bulk', n > 0);
    if (!n) return;
    T.setHTML(b, html`<span class="bulk-count"><b>${n}</b> selected</span>
      <button class="btn btn-sm" type="button" data-bulk="keep">${icon('infinity')}Keep forever</button>
      <button class="btn btn-sm" type="button" data-bulk="download">${icon('download')}Download again</button>
      <button class="btn btn-sm btn-danger" type="button" data-bulk="delete">${icon('trash')}Delete now</button>
      <button class="btn-icon" type="button" data-bulk="clear" aria-label="Clear selection">${icon('x')}</button>`);
  };
  bulk.clear = function () {
    bulk.sel.clear();
    T.$$('.vrow.is-selected').forEach((r) => { r.classList.remove('is-selected'); const c = r.querySelector('[data-sel]'); if (c) c.checked = false; });
    document.body.classList.remove('selecting');
    bulk.render();
  };
  bulk.toggle = function (id, on) {
    if (on) bulk.sel.add(id); else bulk.sel.delete(id);
    T.$$(`.vrow[data-vid="${CSS.escape(id)}"]`).forEach((r) => { r.classList.toggle('is-selected', on); const c = r.querySelector('[data-sel]'); if (c) c.checked = on; });
    document.body.classList.toggle('selecting', bulk.sel.size > 0);
    bulk.render();
  };
  document.addEventListener('change', (e) => {
    const c = e.target.closest('[data-sel]');
    if (!c) return;
    const row = c.closest('[data-vid]');
    if (row) bulk.toggle(row.dataset.vid, c.checked);
  });
  document.addEventListener('click', async (e) => {
    const b = e.target.closest('[data-bulk]');
    if (!b) return;
    const action = b.dataset.bulk;
    if (action === 'clear') { bulk.clear(); return; }
    const ids = Array.from(bulk.sel);
    if (action === 'delete') {
      const inPlex = ids.map((id) => cache.get(id)).filter((v) => v && v.state === 'downloaded');
      const bytes = inPlex.reduce((s, v) => s + (v.size_bytes || 0), 0);
      const ok = await T.confirm({ title: 'Delete ' + T.plural(ids.length, 'video') + ' now?', danger: true, confirm: 'Delete now',
        body: html`<p>${inPlex.length ? html`${T.plural(inPlex.length, 'file')} (${T.fmtBytes(bytes)}) leave Plex straight away.` : 'They leave the queue.'} Only videos downloading right now are skipped.</p>${ui.deleteNotes(inPlex)}` });
      if (!ok) return;
    }
    try {
      const r = await T.api.post('/api/videos/bulk', { ids, action });
      const verb = { keep: 'Kept forever', download: 'Queued', delete: 'Deleted', unkeep: 'Unkept' }[action];
      T.toast(verb + ': ' + T.plural(r.done, 'video') + (r.skipped.length ? ' · ' + r.skipped.length + ' skipped (' + r.skipped[0].reason.replace(/\.$/, '') + (r.skipped.length > 1 ? ', …' : '') + ')' : ''), { type: r.done ? 'success' : 'warn' });
      bulk.clear();
      if (T.app.view && T.app.view.refresh) T.app.view.refresh();
    } catch (err) { T.toastError(err); }
  });
  document.addEventListener('keydown', (e) => { if (e.key === 'Escape' && bulk.sel.size && !document.querySelector('.modal-wrap,.menu')) bulk.clear(); });
})();
