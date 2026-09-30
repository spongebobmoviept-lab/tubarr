/* Trim ads: what Trimarr (the ad trimmer, installed with Tubarr) is doing right now, what it has trimmed, what it would trim, and the
   originals it keeps so every trim can be undone. Everything comes from Trimarr's own API through Tubarr's proxy
   (/api/trimarr/raw/…, see trimarr/API.md). Trimarr has no event stream: this page polls every 4 s while it works,
   every 15 s otherwise. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon, ui = T.ui;
  const V = (T.views.trim = { nav: 'trim', title: 'Trim ads' });
  const R = (p) => '/api/trimarr/raw/' + p;
  const STEPS = ['planning', 'cutting', 'verifying', 'captions', 'swapping', 'plex'];
  const STEP_L = { starting: 'Starting', planning: 'Planning the cuts', cutting: 'Cutting', verifying: 'Verifying', captions: 'Shifting captions', swapping: 'Swapping the file in', plex: 'Updating Plex', restoring: 'Restoring the original' };
  const STEP_S = { planning: 'Plan', cutting: 'Cut', verifying: 'Verify', captions: 'Captions', swapping: 'Swap', plex: 'Plex' };
  const STATE_L = { starting: 'Starting up', scanning: 'Scanning the library', checking: 'Checking SponsorBlock', trimming: 'Trimming', sleeping: 'Idle', off: 'Off', stopped: 'Stopped', not_running: 'Not running' };
  const CAT_L = { sponsor: 'sponsor', selfpromo: 'self-promo', interaction: 'like/subscribe', outro: 'end cards', preview: 'preview', filler: 'filler', music_offtopic: 'non-music', hook: 'hook' };
  const FOUND_SHOW = 25;

  let root, st = null, trimmed = [], found = [], foundTotal = 0, hist = [], timer = null, sig = {}, err = null, showAllFound = false, alive = false;

  const thumbOf = (id, title) => ({ id, title: title || '', thumbnail_url: '/api/img/video/' + encodeURIComponent(id) });
  const ago = (iso) => T.tAgo(iso);                        // ticking spans (tick.js), not plain text
  const until = (iso) => T.tUntil(iso, { fmt: 'span', zero: 'a moment' });
  const cats = (v) => Array.from(new Set((v.cuts || []).flatMap((c) => c.categories || []))).map((c) => CAT_L[c] || c).join(', ');
  const titleOf = (id) => { const v = trimmed.concat(found).find((x) => x.id === id); return v ? v.title : id; };

  function stateOf(s) {
    const w = s.worker || {};
    if (w.current) return { key: w.state === 'checking' ? 'checking' : 'working', label: w.current.action === 'undo' ? 'Restoring an original' : w.current.action === 'check' ? 'Checking SponsorBlock' : 'Trimming' };
    if (s.paused) return { key: 'paused', label: 'Paused' };
    if (w.state === 'stopped' || w.state === 'not_running') return { key: 'stopped', label: 'Stopped' };
    if (w.state === 'off') return { key: 'idle', label: 'Off: nothing runs until you switch automatic trimming on' };
    if (w.state === 'sleeping') return { key: 'idle', label: s.enabled ? 'Idle until the next pass' : 'Idle' };
    return { key: 'working', label: STATE_L[w.state] || w.state };
  }

  /* ---------- the "what it's doing" hero ---------- */
  function hero(s) {
    const w = s.worker || {}, c = w.current, sv = stateOf(s), lp = w.last_pass;
    const passLine = lp ? html`${icon('history')}<span>${lp.finished_at ? html`Last pass ${ago(lp.finished_at)}` : html`This pass started ${ago(lp.started_at)}`}: checked ${T.fmtNum(lp.checked)} · trimmed ${T.fmtNum(lp.trimmed)}${lp.failed ? html` · <span class="t-crit">${lp.failed} failed</span>` : ''}${lp.skipped ? ' · ' + lp.skipped + ' postponed' : ''}${w.next_pass_at ? html` · next pass in ${until(w.next_pass_at)}` : ''}</span>` : '';
    const sbErr = s.sponsorblock && s.sponsorblock.last_error;
    const queue = w.queue || [];
    if (!c) {
      const body = sv.key === 'paused' ? 'No new trims start while paused. SponsorBlock checks keep going, so the lists below stay current.'
        : sv.key === 'stopped' ? 'Trimarr’s worker isn’t running. Nothing is trimmed until it starts again.'
        : !s.enabled ? 'Automatic trimming is off. Trimarr still checks SponsorBlock, so you can see what it would trim, and you can trim any video by hand.'
        : s.counts && s.counts.cuttable ? html`${T.plural(s.counts.cuttable, 'video')} with sponsor segments wait for the next pass${w.next_pass_at ? html` (in ${until(w.next_pass_at)})` : ''}. You can also start one now.`
        : 'Nothing to trim right now. New videos are checked once they’re a day old, when SponsorBlock has segments for them.';
      return html`<section class="trim-hero is-${sv.key}">
        <div class="trim-idle">${T.logoMark('now-idle-mark')}<div><span class="trim-state is-${sv.key}">${sv.key === 'paused' ? icon('pause') : icon('check-circle')}${sv.label}</span>
          <p>${body}</p>${passLine ? html`<p class="trim-pass">${passLine}</p>` : ''}</div></div>
        ${queue.length ? queueBox(queue) : ''}
        ${sbErr ? html`<div class="note note-warn">${icon('alert')}<span>SponsorBlock couldn’t be reached ${ago(sbErr.at)} (${sbErr.message}). Checks resume on the next pass.</span></div>` : ''}
      </section>`;
    }
    const steps = c.action === 'undo' ? ['restoring', 'plex'] : c.action === 'check' ? [] : STEPS;
    const at = steps.indexOf(c.step);
    return html`<section class="trim-hero is-live" data-cur="${c.video_id}" data-step="${c.step}">
      <div class="trim-media">${T.thumb(thumbOf(c.video_id, c.title), 'thumb-now')}</div>
      <div class="trim-body">
        <div class="now-eyebrow"><span class="tally" aria-hidden="true"></span>${sv.label}${c.manual ? html`<span class="now-kind">${icon('user')}You asked for this one</span>` : ''}</div>
        <span class="now-ch">${c.channel}</span>
        <h2 class="now-title">${c.title}</h2>
        ${steps.length ? html`<ol class="stages trim-steps" aria-label="Steps">${steps.map((p, i) => html`<li class="stage is-${i < at ? 'done' : i === at ? 'active' : 'todo'}"><span class="stage-dot">${i < at ? icon('check') : html`<b>${i + 1}</b>`}</span><span class="stage-l">${STEP_S[p] || STEP_L[p] || p}</span></li>`)}</ol>` : ''}
        <div class="now-progress">${ui.bar(steps.length && at >= 0 ? (at + 0.5) / steps.length : null, 'bar-lg')}<b class="now-pct">${STEP_L[c.step] || c.step || 'Working'}…</b></div>
        <div class="now-stats"><span>Started ${ago(c.started_at)}${c.started_at ? html` · running ${T.tElapsed(Date.parse(c.started_at))}` : ''}</span>${passLine ? html`<span class="sep">·</span><span class="trim-pass-inline">${passLine}</span>` : ''}</div>
        ${queue.length ? queueBox(queue) : ''}
      </div>
    </section>`;
  }
  function queueBox(queue) {
    const A = { trim: 'Trim', undo: 'Undo', check: 'Check' };
    return html`<div class="trim-queue"><span class="trim-queue-h">${icon('list')}Next up (${queue.length})</span>
      <ol>${queue.slice(0, 5).map((q) => html`<li><span class="tag tag-sm">${A[q.action] || q.action}</span><span class="trim-queue-t">${titleOf(q.video_id)}</span></li>`)}</ol></div>`;
  }

  /* ---------- totals ---------- */
  function tiles(s) {
    const n = s.counts || {}, sec = s.seconds || {}, o = s.originals || {};
    const tile = (label, value, sub, ic, cls) => html`<div class="stat-tile ${cls || ''}"><span class="st-label">${icon(ic)}${label}</span><span class="st-value">${value}</span>${sub ? html`<span class="st-sub">${sub}</span>` : ''}</div>`;
    return html`<div class="tiles trim-tiles">
      ${tile('Trimmed', T.fmtNum(sec.trimmed_videos || n.trimmed || 0), T.fmtSpan(sec.removed || 0) + ' of ads removed', 'scissors', 'is-good')}
      ${tile('Found, not trimmed yet', T.fmtNum(sec.would_trim_videos || n.cuttable || 0), T.fmtSpan(sec.would_remove || 0) + ' would come out', 'search')}
      ${tile('Clean', T.fmtNum(n.clean || 0), 'no sponsor segments', 'check-circle')}
      ${tile('Too new', T.fmtNum(n.waiting || 0), 'under a day old: SponsorBlock is still collecting', 'hourglass')}
      ${tile('Not checked yet', T.fmtNum(n.new || 0), 'checked on the next pass', 'clock')}
      ${tile('Needs a look', T.fmtNum((n.suspicious || 0) + (n.failed || 0)), (n.suspicious || 0) + ' would lose over 40% · ' + (n.failed || 0) + ' failed', 'alert', (n.suspicious || n.failed) ? 'is-warn' : '')}
      ${tile('Originals kept', T.fmtNum(o.count || 0), T.fmtBytes(o.bytes || 0) + ' of ' + T.fmtBytes(o.cap_bytes || 0), 'archive')}
    </div>`;
  }

  /* ---------- lists ---------- */
  function trimmedCard() {
    const rows = trimmed.slice().sort((a, b) => Date.parse((b.trimmed || {}).at || 0) - Date.parse((a.trimmed || {}).at || 0));
    return html`<section class="card tr-card">
      <header class="card-head"><h2>${icon('scissors')}Trimmed<span class="count-pill">${rows.length}</span></h2><span class="card-sub">Newest first · Undo puts the original back while it’s kept</span></header>
      ${!rows.length ? ui.empty({ icon: 'scissors', cls: 'empty-sm', title: 'Nothing trimmed yet', body: st && st.enabled ? 'Trimmed videos show up here with what came out.' : 'Turn on automatic trimming, or trim a video by hand from the list below.' })
        : html`<ul class="tlist">${rows.map((v) => {
          const t = v.trimmed || {};
          return html`<li class="trow" data-tid="${v.id}">
            <button class="trow-thumb" type="button" data-act="tr-open" aria-label="Open ${v.title}">${T.thumb(thumbOf(v.id, v.title), 'thumb-sm', html`<span class="dur">${T.fmtClock(t.duration_after)}</span>`)}</button>
            <span class="trow-main"><button class="trow-title" type="button" data-act="tr-open">${v.title}</button>
              <span class="trow-meta"><a href="#/channel/${encodeURIComponent(v.channel_id)}">${v.channel}</a><span class="sep">·</span>trimmed ${ago(t.at)}${cats(v) ? html`<span class="sep">·</span>${cats(v)}` : ''}</span>
              <span class="trow-len"><span class="len-before">${T.fmtClock(t.duration_before)}</span>${icon('chev-right')}<b>${T.fmtClock(t.duration_after)}</b><span class="trim-chip">${icon('scissors')}−${T.fmtClock(t.removed_seconds)}</span></span></span>
            ${t.undo_available ? html`<button class="btn btn-sm" type="button" data-act="tr-undo">${icon('undo')}Undo</button>` : html`<span class="trow-note">Original deleted</span>`}
          </li>`;
        })}</ul>`}
    </section>`;
  }
  function originalsCard() {
    const o = (st && st.originals) || {}, rows = trimmed.filter((v) => v.trimmed && v.trimmed.original_kept_until && v.trimmed.undo_available)
      .sort((a, b) => Date.parse(a.trimmed.original_kept_until) - Date.parse(b.trimmed.original_kept_until));
    const cap = o.cap_bytes || 0, used = o.bytes || 0, ratio = cap ? used / cap : 0;
    return html`<section class="card orig-card">
      <header class="card-head"><h2>${icon('archive')}Saved originals<span class="count-pill">${o.count || 0}</span></h2><span class="card-sub">${o.keep_days != null ? 'Each is deleted ' + T.plural(o.keep_days, 'day') + ' after its trim' : ''}</span></header>
      <div class="orig-meter"><span class="meter-sm"><i style="width:${(Math.min(1, ratio) * 100).toFixed(2)}%"></i></span>
        <span class="orig-fig"><b>${T.fmtBytes(used)}</b> of the ${T.fmtBytes(cap)} cap${ratio >= 0.9 ? html` · <span class="t-warn">trimming waits above the cap</span>` : ''}</span></div>
      ${!rows.length ? html`<p class="card-lead">No originals kept right now.</p>` : html`<ul class="olist">${rows.map((v) => html`<li data-tid="${v.id}">
          <span class="o-main"><button class="trow-title" type="button" data-act="tr-open">${v.title}</button><span class="trow-meta">${v.channel}<span class="sep">·</span>${T.fmtBytes(v.trimmed.size_before)}</span></span>
          <span class="o-exp">${icon('clock')}deleted in ${until(v.trimmed.original_kept_until)}</span>
          <button class="link-btn link-danger" type="button" data-act="tr-approve">Delete now</button></li>`)}</ul>
        <div class="btn-row"><button class="btn btn-sm btn-crit-soft" type="button" data-act="tr-approve-all">${icon('trash')}Delete all originals now</button></div>`}
      <p class="hint">${icon('info')}<span>Originals live in the library’s <code>.trim-originals</code> folder and count against the library’s space, so Trimarr keeps them under its limit.</span></p>
    </section>`;
  }
  function foundCard() {
    const rows = found.slice().sort((a, b) => (b.cut_seconds || 0) - (a.cut_seconds || 0));
    const list = showAllFound ? rows : rows.slice(0, FOUND_SHOW);
    const secs = rows.reduce((s, v) => s + (v.cut_seconds || 0), 0);
    return html`<section class="card found-card">
      <header class="card-head"><h2>${icon('search')}Found, not trimmed yet<span class="count-pill">${T.fmtNum(foundTotal)}</span></h2><span class="card-sub">${rows.length ? T.fmtSpan(secs) + ' in total · biggest first' : ''}</span></header>
      ${rows.length ? html`<p class="card-lead">${st && st.enabled ? 'Trimmed on the next pass, for channels that are on. Or trim one now.' : 'Automatic trimming is off: these stay as they are unless you trim one.'}</p>` : ''}
      ${!rows.length ? ui.empty({ icon: 'check-circle', cls: 'empty-sm', title: 'Nothing found', body: 'No checked video has sponsor segments waiting.' })
        : html`<ul class="tlist">${list.map((v) => html`<li class="trow" data-tid="${v.id}">
            <button class="trow-thumb" type="button" data-act="tr-open" aria-label="Open ${v.title}">${T.thumb(thumbOf(v.id, v.title), 'thumb-sm', html`<span class="dur">${T.fmtClock(v.duration_seconds)}</span>`)}</button>
            <span class="trow-main"><button class="trow-title" type="button" data-act="tr-open">${v.title}</button>
              <span class="trow-meta"><a href="#/channel/${encodeURIComponent(v.channel_id)}">${v.channel}</a>${v.upload_date ? html`<span class="sep">·</span>${T.fmtDay(v.upload_date)}` : ''}${!v.auto_trim ? html`<span class="sep">·</span>channel off` : ''}</span>
              <span class="trow-len"><span class="trim-chip is-plan">${icon('scissors')}${T.fmtClock(v.cut_seconds)}</span><span class="dim">${cats(v)}${v.cuts && v.cuts.length > 1 ? ' · ' + v.cuts.length + ' cuts' : ''}</span></span></span>
            <button class="btn btn-sm" type="button" data-act="tr-trim">${icon('scissors')}Trim now</button>
          </li>`)}</ul>
          ${rows.length > FOUND_SHOW ? html`<button class="btn btn-ghost btn-block" type="button" data-act="tr-more">${showAllFound ? 'Show fewer' : 'Show all ' + rows.length}</button>` : ''}`}
    </section>`;
  }
  function historyCard() {
    return html`<section class="card hist-card">
      <header class="card-head"><h2>${icon('history')}History</h2></header>
      ${!hist.length ? html`<p class="card-lead">Nothing yet.</p>` : html`<ul class="hlist">${hist.map((h) => html`<li class="hrow">
        <span class="h-ic ${h.level === 'error' ? 's-crit' : /^trimmed/.test(h.message) ? 's-good' : 's-dim'}">${icon(h.level === 'error' ? 'alert-circle' : /^trimmed/.test(h.message) ? 'scissors' : /^restored|undo/i.test(h.message) ? 'undo' : 'info')}</span>
        <span class="h-main"><span class="h-title h-msg">${h.message}</span></span>
        <time class="h-time" datetime="${h.at}">${T.fmtTime(h.at)}</time></li>`)}</ul>`}
    </section>`;
  }

  /* ---------- render + data ---------- */
  function renderHead() {
    const a = root.querySelector('[data-slot="actions"]');
    if (!a || !st) return;
    T.setHTML(a, html`${ui.switch('tr-enabled', !!st.enabled, { text: st.enabled ? 'Automatic trimming on' : 'Automatic trimming off', label: 'Automatic trimming', attrs: 'data-tr-enabled=""' })}
      ${st.paused ? html`<button class="btn btn-primary" type="button" data-act="tr-resume">${icon('play')}Resume</button>` : html`<button class="btn" type="button" data-act="tr-pause">${icon('pause')}Pause</button>`}
      <button class="btn" type="button" data-act="tr-pass" aria-label="Run a pass now" title="Scan, check SponsorBlock and trim now">${icon('refresh')}<span class="hide-sm">Run a pass now</span></button>`);
  }
  function render() {
    if (!root) return;
    if (err) { T.setHTML(root.querySelector('[data-slot="body"]'), missing(err)); T.setHTML(root.querySelector('[data-slot="actions"]'), ''); sig = {}; return; }
    if (!st) return;
    renderHead();
    const w = st.worker || {};
    const parts = { hero: hero(st), tiles: tiles(st), trimmed: trimmedCard(), orig: originalsCard(), found: foundCard(), hist: historyCard() };
    const key = {
      hero: JSON.stringify([w.state, w.current, w.queue, w.last_pass, w.next_pass_at, st.paused, st.enabled, st.sponsorblock]),
      tiles: JSON.stringify([st.counts, st.seconds, st.originals]),
      trimmed: trimmed.map((v) => v.id + (v.trimmed && v.trimmed.undo_available)).join() + (st.enabled ? 1 : 0),
      orig: trimmed.map((v) => v.id + (v.trimmed && v.trimmed.original_kept_until)).join() + JSON.stringify(st.originals) + Math.floor(Date.now() / 60000),
      found: found.map((v) => v.id).join() + showAllFound + (st.enabled ? 1 : 0),
      hist: hist.map((h) => h.id).join(),
    };
    const body = root.querySelector('[data-slot="body"]');
    if (!body.querySelector('[data-part="hero"]')) {
      T.setHTML(body, html`<div data-part="hero"></div><div data-part="tiles"></div>
        <div class="trim-grid"><div data-part="trimmed"></div><div data-part="orig"></div></div>
        <div class="trim-grid"><div data-part="found"></div><div data-part="hist"></div></div>`);
      sig = {};
    }
    Object.keys(parts).forEach((k) => { if (sig[k] !== key[k]) { sig[k] = key[k]; T.setHTML(body.querySelector(`[data-part="${k}"]`), parts[k]); } });
  }
  function missing(e) {
    const gone = e.status === 404 || e.code === 'not_found';
    return ui.empty({
      icon: 'scissors', title: gone ? 'Trimarr isn’t installed' : 'Can’t reach Trimarr',
      body: gone ? 'Trimarr is the ad trimmer that cuts sponsor segments out of videos after they’re in Plex. Tubarr itself keeps every video exactly as uploaded.'
        : (e.message || 'Trimarr didn’t answer.') + ' Tubarr keeps trying.',
      actions: gone ? '' : html`<button class="btn" type="button" data-act="tr-reload">${icon('refresh')}Try again</button>`,
    });
  }
  let lastCounts = '';
  async function loadStatus() {
    try { st = await T.api.get(R('status')); err = null; } catch (e) { err = e; st = null; }
  }
  async function loadLists() {
    const [t, f, h] = await Promise.all([
      T.api.get(R('videos?state=trimmed&limit=500')).catch(() => ({ videos: [] })),
      T.api.get(R('videos?state=cuttable,suspicious&limit=500')).catch(() => ({ videos: [], total: 0 })),
      T.api.get(R('history?limit=40')).catch(() => ({ history: [] })),
    ]);
    trimmed = t.videos || []; found = f.videos || []; foundTotal = f.total != null ? f.total : found.length; hist = h.history || [];
  }
  async function tick(forceLists) {
    if (!alive) return;
    await loadStatus();
    const counts = st ? JSON.stringify([st.counts, st.originals && st.originals.count, st.worker && st.worker.last_pass && st.worker.last_pass.trimmed]) : '';
    if (st && (forceLists || counts !== lastCounts)) { lastCounts = counts; await loadLists(); }
    render();
    clearTimeout(timer);
    if (!alive) return;
    const busy = st && st.worker && (st.worker.current || (st.worker.queue || []).length);
    timer = setTimeout(() => tick(false), document.visibilityState === 'hidden' ? 60000 : busy ? 4000 : 15000);
  }
  const soon = (ms) => { clearTimeout(timer); timer = setTimeout(() => tick(true), ms || 600); };

  V.enter = async function (el) {
    root = el; alive = true; st = null; err = null; sig = {}; lastCounts = ''; showAllFound = false;
    T.setHTML(root, html`<div class="page page-trim">
      <header class="page-head">
        <div class="page-title"><h1>Trim ads</h1><p class="page-sub">Trimarr cuts sponsor segments out of videos that are already in Plex, using SponsorBlock. It keeps each original for a few days, so every trim can be undone.</p></div>
        <div class="page-actions trim-actions" data-slot="actions"></div>
      </header>
      <div data-slot="body"><section class="now-card skel-now"><div class="thumb skel"></div><div class="skel-lines"><span class="skel skel-line"></span><span class="skel skel-line"></span><span class="skel skel-line short"></span></div></section></div>
    </div>`);
    root.querySelector('[data-slot="actions"]').addEventListener('change', async (e) => {
      if (e.target.dataset.trEnabled == null) return;
      const on = e.target.checked;
      try {
        await T.api.patch(R('settings'), { enabled: on });
        T.toast(on ? 'Automatic trimming is on. Channels that are on get trimmed on each pass.' : 'Automatic trimming is off. Nothing is trimmed unless you ask.', { type: on ? 'success' : 'info' });
        if (T.trimarr) T.trimarr.load();
      } catch (x) { T.toastError(x); }
      soon(200);
    });
    await tick(true);
  };
  V.leave = function () { alive = false; clearTimeout(timer); root = null; };
  V.refresh = () => tick(true);

  async function post(path, body, ok) {
    try { const r = await T.api.post(R(path), body || {}); if (ok) T.toast(typeof ok === 'function' ? ok(r) : (r && r.message) || ok); }
    catch (x) { T.toastError(x); }
    soon();
  }
  V.act = function (name, el) {
    const row = el.closest('[data-tid]'), id = row && row.dataset.tid;
    const v = id && (trimmed.find((x) => x.id === id) || found.find((x) => x.id === id));
    switch (name) {
      case 'tr-reload': tick(true); return;
      case 'tr-pause': post('pause', null, 'Trimarr is paused. A trim that’s running finishes first.'); return;
      case 'tr-resume': post('resume', null, 'Trimarr resumed; a pass starts now.'); return;
      case 'tr-pass': post('pass', null, (r) => (r && r.message) || 'A pass starts now.'); return;
      case 'tr-more': showAllFound = !showAllFound; sig.found = null; render(); return;
      case 'tr-open': if (id) T.openVideo(id); return;
      case 'tr-trim': if (id) post('videos/' + encodeURIComponent(id) + '/trim', {}, (r) => (r && r.message) || 'Trimming queued.'); return;
      case 'tr-undo':
        if (!v) return;
        T.confirm({ title: 'Put the original back?', confirm: 'Restore original', body: html`<p class="confirm-lead">${v.title}</p><p>The trimmed file is replaced by the original (${T.fmtClock(v.trimmed.duration_before)}), captions included, and Plex is refreshed. Trimarr won’t trim this video again on its own.</p>` })
          .then((yes) => { if (yes) post('videos/' + encodeURIComponent(id) + '/undo', {}, (r) => (r && r.message) || 'Restoring the original.'); });
        return;
      case 'tr-approve':
        if (!v) return;
        T.confirm({ title: 'Delete the saved original?', danger: true, confirm: 'Delete original', body: html`<p class="confirm-lead">${v.title}</p><p>This frees ${T.fmtBytes(v.trimmed.size_before)}. The trim stays, but it can’t be undone any more.</p>` })
          .then((yes) => { if (yes) post('videos/' + encodeURIComponent(id) + '/approve', {}, 'Original deleted.'); });
        return;
      case 'tr-approve-all': {
        const o = (st && st.originals) || {};
        T.confirm({ title: 'Delete every saved original?', danger: true, confirm: 'Delete all', body: html`<p>This frees ${T.fmtBytes(o.bytes || 0)} (${T.plural(o.count || 0, 'original')}). The trims stay, but none of them can be undone afterwards.</p>` })
          .then((yes) => { if (yes) post('originals/approve', {}, (r) => T.plural((r && r.deleted) || 0, 'original') + ' deleted.'); });
        return;
      }
      default: return false;
    }
  };
})();
