/* Activity: the running download (stage by stage), what needs attention, the queue, and recent history. Live. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon, ui = T.ui;
  const ALL_STAGES = ['download', 'plex'];   // Tubarr never cuts videos: download, then finalize into Plex
  const QUEUE_SHOW = 25;

  const V = (T.views.activity = { nav: 'activity', title: 'Activity' });
  let root, data = null, sig = {}, unsubs = [], showAllQueue = false, refetch = null, settings = null;

  function nowCard(a) {
    const d = a.downloader, c = a.current;
    if (!c) {
      const win = d.overnight ? T.fmtHHMM(d.overnight.start) + '–' + T.fmtHHMM(d.overnight.end) : 'overnight';
      const every = settings && settings.downloads.check_interval_minutes;
      let title, body, act = '';
      if (d.state === 'paused') { title = 'Downloads are paused'; body = 'Nothing new downloads until you resume. The queue keeps its order.'; act = html`<button class="btn btn-primary" type="button" data-act="resume">${icon('play')}Resume downloads</button>`; }
      else if (d.state === 'stopped') { title = 'The downloader isn’t running'; body = 'Tubarr’s worker process is stopped, so nothing downloads right now. The queue keeps its order and picks up where it left off when the worker starts again.'; }
      else if (d.state === 'waiting' && d.backoff && d.backoff.active) { title = 'YouTube asked Tubarr to slow down'; body = html`Tubarr backed off and resumes on its own${d.backoff.resume_in_seconds ? html` in about ${T.fmtSpan(d.backoff.resume_in_seconds)}` : ''}. Nothing is lost; the queue keeps its order.`; }
      else if (d.state === 'waiting' && d.wait_reason === 'pace') { title = T.dlWaitHtml(d); body = paceLine(d); }
      else if (d.state === 'waiting' && d.backlog_paused) { title = 'The library is full'; body = html`It reached its fill target, so ${T.plural(d.waiting_count, 'backlog video')} ${d.waiting_count === 1 ? 'waits' : 'wait'} for room. New uploads still download: each one rolls out the next video in line.`; }
      else if (d.state === 'waiting' && d.overnight) { title = 'Waiting for tonight'; body = html`${T.plural(d.waiting_count, 'backlog video')} ${d.waiting_count === 1 ? 'waits' : 'wait'} for the overnight window (${win}). New uploads still download as soon as they’re found.`; }
      else if (d.state === 'waiting' && d.wait_reason === 'daily_cap') { title = 'Today’s download limit is reached'; body = html`${paceLine(d)} The next one starts when the oldest of those is 24 hours old.`; }
      else if (d.state === 'waiting' && d.wait_reason === 'older') { title = 'Waiting: older than everything in your library'; body = 'The library is full, and the next video in line is older than every video the roll could remove, so Tubarr doesn’t swap a newer video out for it. A newer upload, or “Download first”, starts right away.'; }
      else if (d.queue_count && d.pacing && d.pacing.next_up && d.pacing.next_up.download_now) { title = 'Starting your download now'; body = html`“${d.pacing.next_up.title}” starts within a few seconds. Download now skips the pause and the daily limit; it’s still one video at a time.`; }
      else if (d.queue_count) { title = 'Starting the next download'; body = paceLine(d); }
      else { title = 'All caught up'; body = 'Nothing is downloading. New uploads start as soon as Tubarr finds them' + (every ? ' (it checks every ' + every + ' min).' : '.'); }
      return html`<section class="now-card is-idle"><div class="now-idle">${T.logoMark('now-idle-mark')}<div><h2>${title}</h2><p>${body}</p>${act}</div></div>${paceExtra(d)}</section>`;
    }
    const v = c.video, stages = c.stages || ALL_STAGES, cur = stages.indexOf(c.stage);
    const p = c.progress == null ? null : c.progress;
    const res = c.resolution || v.resolution;
    const stageRow = stages.filter((s) => s !== 'sponsorblock').map((s) => {
      const i = stages.indexOf(s);
      const state = i < 0 ? 'skip' : i < cur ? 'done' : i === cur ? 'active' : 'todo';
      return html`<li class="stage is-${state}"><span class="stage-dot">${state === 'done' ? icon('check') : icon(ui.STAGE_ICONS[s])}</span><span class="stage-l">${ui.STAGES[s]}${state === 'skip' ? html`<small>No segments</small>` : state === 'done' && s === 'sponsorblock' && c.sponsorblock_cut_seconds ? html`<small>Cut ${T.fmtClock(c.sponsorblock_cut_seconds)}</small>` : ''}</span></li>`;
    });
    return html`<section class="now-card is-live" data-now="${v.id}" data-stage="${c.stage}">
      <div class="now-bg" aria-hidden="true">${T.thumbArt(v)}</div>
      <div class="now-media">${T.thumb(v, 'thumb-now', html`<span class="dur">${T.fmtClock(v.duration_seconds)}</span>`)}</div>
      <div class="now-body">
        <div class="now-eyebrow"><span class="tally" aria-hidden="true"></span>${(a.active || []).length > 1 ? 'Downloading now · 1 of ' + a.active.length : 'Now downloading'}<span class="now-kind">${v.backfill ? html`${icon('history')}Backlog${v.upload_date ? ' · uploaded ' + T.fmtDay(v.upload_date, true) : ''}` : html`${icon('sparkles')}New upload${v.published_at ? html` · ${T.tAgo(v.published_at)}` : ''}`}</span></div>
        <a class="now-ch" href="#/channel/${encodeURIComponent(v.channel_id)}">${v.channel_title}</a>
        <h2 class="now-title">${v.title}</h2>
        <ol class="stages" aria-label="Steps">${stageRow}</ol>
        <div class="now-progress">
          <div class="bar bar-lg ${c.stage === 'download' ? '' : 'is-ind'}" role="progressbar" aria-label="${ui.STAGES[c.stage]}" aria-valuemin="0" aria-valuemax="100" ${p != null ? T.raw('aria-valuenow="' + Math.round(p * 100) + '"') : ''}><i data-live="now-bar" style="width:${c.stage === 'download' ? ((p || 0) * 100).toFixed(2) : '100'}%"></i></div>
          <b class="now-pct" data-live="now-pct">${c.stage === 'download' ? Math.floor((p || 0) * 100) + '%' : ui.STAGES[c.stage] + '…'}</b>
        </div>
        <div class="now-stats">${c.stage === 'download'
          ? html`<span data-live="now-bytes">${T.fmtBytes(c.bytes_done)} of ${T.fmtBytes(c.bytes_total)}</span><span class="sep">·</span><span data-live="now-speed">${T.fmtSpeed(c.speed_bps)}</span><span class="sep">·</span><span data-live="now-eta">${c.eta_seconds != null ? 'about ' + T.fmtSpan(c.eta_seconds) + ' left' : ''}</span>${res ? html`<span class="sep">·</span><span>${T.fmtRes(res)}</span>` : ''}${c.started_at ? html`<span class="sep">·</span><span>running ${T.tElapsed(Date.parse(c.started_at))}</span>` : ''}`
          : html`<span>${T.fmtBytes(c.bytes_total)}</span>${res ? html`<span class="sep">·</span><span>${T.fmtRes(res)}</span>` : ''}<span class="sep">·</span><span>Almost there</span>`}</div>
      </div>
    </section>`;
  }
  /* Human pace: one video, a short random pause, a daily cap. Plain words, live numbers from the worker. */
  function paceLine(d) {
    const p = d.pacing || {};
    if (p.cap_24h == null) return '';
    const gap = !p.gap_max_minutes ? 'no pause' : (p.gap_min_minutes === p.gap_max_minutes ? T.fmtNum(p.gap_min_minutes) : fmtMin(p.gap_min_minutes) + '–' + fmtMin(p.gap_max_minutes)) + ' min pause';
    return html`Human pace: ${p.one_at_a_time ? 'one video at a time' : 'a few at a time'}, then a ${gap}, at most ${p.cap_24h} a day. <b>${p.started_last_24h} of ${p.cap_24h}</b> started in the last 24 hours. The limit is for the backlog: new uploads (last 2 days) and videos you push to the front still come in after it.${p.slowed_by_account ? ' YouTube asked Tubarr to slow down, so pauses are longer until 3 downloads in a row work.' : ''}`;
  }
  const fmtMin = (m) => (Math.round(m * 10) / 10).toString();
  function paceExtra(d) {
    const p = d.pacing || {};
    const next = p.next_up, done = p.recent_done || [];
    if (!next && !done.length) return '';
    return html`<div class="pace-extra">
      ${next ? html`<div class="pace-col"><h3>${icon('download')}Next in line</h3><div class="vlist">${ui.videoRow(next, { when: 'date', select: false })}</div>${p.next_up_waits_older ? html`<p class="hint">${icon('storage')}Waiting: older than everything in your library.</p>` : ''}</div>` : ''}
      ${done.length ? html`<div class="pace-col"><h3>${icon('check-circle')}Just finished</h3><div class="vlist">${done.slice(0, 3).map((x) => ui.videoRow(x.video, { when: 'date', select: false }))}</div></div>` : ''}
    </div>`;
  }
  /* Everything else running at the same time (the real downloader runs many jobs at once). */
  function jobsCard(a) {
    const d = a.downloader, lead = a.current && a.current.video ? a.current.video.id : null;
    const jobs = (a.active || []).filter((j) => j.video_id !== lead || !lead);
    if (!jobs.length) return '';
    const dl = jobs.filter((j) => j.stage === 'download'), fin = jobs.length - dl.length;
    return html`<section class="card jobs-card">
      <header class="card-head"><h2>${icon('download')}Also running<span class="count-pill">${jobs.length}</span></h2>
        <span class="card-sub">${T.fmtSpeed(d.speed_bps || 0)} combined${d.concurrency ? html` · ${d.concurrency} download slots now (self-tuning, up to ${d.concurrency_max})` : ''}${fin ? html` · ${fin} adding to Plex` : ''}</span></header>
      <ul class="jobs">${jobs.map((j) => {
        const v = j.video || { id: j.video_id || j.title, title: j.title, channel_title: j.channel_title, thumbnail_url: null };
        if (j.video) T.cacheVideos([j.video]);
        const pct = j.progress != null ? Math.floor(j.progress * 100) + '%' : '';
        return html`<li class="job" ${j.video_id ? T.raw('data-job="' + T.esc(j.video_id) + '" data-vid="' + T.esc(j.video_id) + '"') : ''}>
          ${T.thumb(v, 'thumb-xs')}
          <span class="job-main"><button class="job-title" type="button" data-act="v-open" ${j.video_id ? '' : T.raw('disabled')}>${j.new_upload ? html`<span class="new-mark">NEW</span>` : ''}${j.title}</button>
            <span class="job-meta">${j.channel_title}${j.video && j.video.backfill ? html`<span class="sep">·</span>Backlog${j.video.upload_date ? ' · ' + T.fmtDay(j.video.upload_date, true) : ''}` : ''}</span></span>
          <span class="job-state">${j.stage === 'download' ? html`<b class="num" data-live="job-pct">${pct}</b><small data-live="job-speed">${j.speed_bps ? T.fmtSpeed(j.speed_bps) : ''}</small>` : html`<b>${ui.STAGES[j.stage] || 'Finishing'}</b>`}</span>
          <span class="bar bar-xs ${j.stage === 'download' ? '' : 'is-ind'} job-bar"><i data-live="job-bar" style="width:${j.stage === 'download' ? ((j.progress || 0) * 100).toFixed(2) : 100}%"></i></span>
        </li>`;
      })}</ul>
    </section>`;
  }
  function patchJob(p) {
    const row = root && root.querySelector(`[data-job="${CSS.escape(p.id)}"]`);
    if (!row || p.stage !== 'download') return;
    const q = (k) => row.querySelector(`[data-live="${k}"]`);
    if (q('job-bar')) q('job-bar').style.width = ((p.progress || 0) * 100).toFixed(2) + '%';
    if (q('job-pct')) q('job-pct').textContent = p.progress != null ? Math.floor(p.progress * 100) + '%' : '';
    if (q('job-speed')) q('job-speed').textContent = p.speed_bps ? T.fmtSpeed(p.speed_bps) : '';
  }
  function patchNow(p) {
    patchJob(p);
    const card = root && root.querySelector(`[data-now="${CSS.escape(p.id)}"]`);
    if (!card) return;
    if (card.dataset.stage !== p.stage) { scheduleRefetch(0); return; }
    if (p.stage !== 'download') return;
    const q = (k) => card.querySelector(`[data-live="${k}"]`);
    if (q('now-bar')) q('now-bar').style.width = ((p.progress || 0) * 100).toFixed(2) + '%';
    if (q('now-pct')) q('now-pct').textContent = Math.floor((p.progress || 0) * 100) + '%';
    if (q('now-bytes') && p.bytes_total) q('now-bytes').textContent = T.fmtBytes(p.bytes_done) + ' of ' + T.fmtBytes(p.bytes_total);
    if (q('now-speed') && p.speed_bps) q('now-speed').textContent = T.fmtSpeed(p.speed_bps);
    if (q('now-eta') && p.eta_seconds != null) q('now-eta').textContent = 'about ' + T.fmtSpan(p.eta_seconds) + ' left';
  }

  function failedCard(a) {
    if (!a.failed.length) return '';
    const retryable = a.failed.filter((f) => !f.error || f.error.retryable).length;
    return html`<section class="card attn-card">
      <header class="card-head"><h2>${icon('alert')}Needs attention<span class="count-pill is-crit">${a.failed.length}</span></h2>
        ${retryable ? html`<button class="btn btn-sm btn-crit-soft" type="button" data-act="retry-all">${icon('refresh')}Retry ${retryable > 1 ? 'all ' + retryable : ''}</button>` : ''}</header>
      <div class="vlist">${a.failed.map((f) => ui.videoRow(f.video, { when: 'date', select: false }))}</div>
    </section>`;
  }

  const REASON = { new_upload: ['New upload', 'sparkles'], backfill: ['Backlog', 'history'], retry: ['Retry', 'refresh'], manual: ['By hand', 'download'], upgrade: ['Better copy (4K)', 'tv'] };
  function queueCard(a) {
    const d = a.downloader;
    const rows = showAllQueue ? a.queue : a.queue.slice(0, QUEUE_SHOW);
    const bytes = a.queue.reduce((s, q) => s + (q.estimated_bytes || 0), 0);
    const total = a.queue_total != null ? a.queue_total : a.queue.length;
    return html`<section class="card queue-card">
      <header class="card-head"><h2>${icon('list')}Queue<span class="count-pill">${T.fmtNum(total)}</span></h2><span class="card-sub">${a.queue.length ? html`In download order: “Download now” first (skips the pause), then “Download first” marks, then newest uploads · one at a time` : ''}</span></header>
      ${!a.queue.length ? ui.empty({ icon: 'check-circle', cls: 'empty-sm', title: 'Nothing queued', body: 'New uploads land here the moment they’re found.' })
        : html`<ol class="qlist">${rows.map((q) => {
          const v = q.video; T.cacheVideos([v]);
          const [rl, ri] = REASON[q.reason] || [q.reason, 'download'];
          return html`<li class="qrow" data-vid="${v.id}">
            <span class="q-pos num">${q.position}</span>
            <button class="q-thumb" type="button" data-act="v-open" aria-label="Open ${v.title}">${T.thumb(v, 'thumb-sm', html`<span class="dur">${T.fmtClock(v.duration_seconds)}</span>`)}</button>
            <span class="q-main"><button class="q-title" type="button" data-act="v-open">${v.title}</button>
              <span class="q-meta"><a href="#/channel/${encodeURIComponent(v.channel_id)}">${v.channel_title}</a><span class="sep">·</span>${q.estimated_bytes ? '~' + T.fmtBytes(q.estimated_bytes) : '—'}${v.backfill && v.upload_date ? html`<span class="sep">·</span>uploaded ${T.fmtDay(v.upload_date, true)}` : ''}</span></span>
            <span class="q-tags"><span class="tag tag-sm">${icon(ri)}${rl}</span>${q.waits_for === 'older' ? html`<span class="tag tag-sm tag-night" title="The library is full and this is older than everything the roll could remove">${icon('storage')}Waiting: older than your library</span>` : ''}${q.waits_for === 'overnight' ? html`<span class="tag tag-sm tag-night" title="Backlog downloads run in the overnight window">${icon('moon')}Tonight</span>` : q.waits_for === 'paused' ? html`<span class="tag tag-sm">${icon('pause')}Paused</span>` : q.waits_for === 'space' ? html`<span class="tag tag-sm tag-night" title="The library is at its fill target; backlog waits for room">${icon('storage')}Waits for room</span>` : ''}${v.download_now ? html`<span class="tag tag-sm tag-now"><span class="spin" aria-hidden="true"></span>Starting…</span>` : ui.nowButton()}</span>
            <button class="btn-icon vrow-more" type="button" data-act="v-menu" aria-label="Actions for ${v.title}">${icon('more')}</button>
          </li>`;
        })}</ol>
        ${a.queue.length > QUEUE_SHOW ? html`<button class="btn btn-ghost btn-block" type="button" data-act="queue-all">${showAllQueue ? 'Show fewer' : 'Show the next ' + a.queue.length}</button>` : ''}`}
      ${d.waiting_count && d.state !== 'paused' && d.overnight ? html`<p class="card-foot">${icon('moon')}Backlog runs overnight (${T.fmtHHMM(d.overnight.start)}–${T.fmtHHMM(d.overnight.end)}), one at a time, so the day stays quiet.</p>` : ''}
      ${d.backlog_paused ? html`<p class="card-foot">${icon('storage')}The library is at its fill target, so it rolls: each new upload replaces the oldest video. Older backlog videos wait for room.</p>` : ''}
    </section>`;
  }

  const EV = {
    downloaded: ['check-circle', 's-good', (h) => html`${T.fmtBytes(h.size_bytes)}${h.sponsorblock_cut_seconds > 0 ? html`<span class="sep">·</span><span class="sb-chip">${icon('scissors')}SponsorBlock cut ${T.fmtClock(h.sponsorblock_cut_seconds)}</span>` : ''}${h.video && h.video.backfill ? html`<span class="sep">·</span><span class="bf-tag">${icon('history')}Backlog</span>` : ''}`],
    failed: ['alert-circle', 's-crit', (h) => h.detail || 'Failed'],
    skipped: ['minus-circle', 's-dim', (h) => ({ short: 'Skipped: a Short', live: 'Skipped: a livestream replay', too_long: 'Skipped: too long', members: 'Skipped: members only' }[h.detail] || 'Skipped')],
    removed: ['trash', 's-dim', (h) => (ui.REMOVED_CHIP[h.detail] || 'Removed')],
    gone: ['archive', 's-gold', (h) => 'Removed from YouTube · kept in Plex' + (ui.GONE_WHY && ui.GONE_WHY[h.detail] ? ' (' + ui.GONE_WHY[h.detail] + ')' : '')],
  };
  function historyCard(a) {
    const days = [], by = {};
    a.history.forEach((h) => { const d = T.fmtDay(h.at); if (!by[d]) { by[d] = []; days.push(d); } by[d].push(h); });
    const todayLabel = T.fmtDay(new Date().toISOString());
    const yd = new Date(); yd.setDate(yd.getDate() - 1);
    const label = (d) => (d === todayLabel ? 'Today' : d === T.fmtDay(yd.toISOString()) ? 'Yesterday' : d);
    return html`<section class="card hist-card">
      <header class="card-head"><h2>${icon('history')}History</h2><a class="card-link" href="#/timeline">Full timeline ${icon('chev-right')}</a></header>
      ${!a.history.length ? ui.empty({ icon: 'history', cls: 'empty-sm', title: 'No history yet', body: 'Every download, skip and removal is logged here.' }) :
        days.map((d) => html`<div class="hist-day"><h3>${label(d)}</h3><ul class="hlist">${by[d].map((h) => {
          const [ic, cls, detail] = EV[h.event] || ['info', 's-dim', () => h.event];
          const v = h.video;
          if (v) T.cacheVideos([v]);
          return html`<li class="hrow" ${v ? T.raw('data-vid="' + T.esc(v.id) + '"') : ''}><span class="h-ic ${cls}">${icon(ic)}</span>
            <span class="h-main">${v ? html`<button class="h-title" type="button" data-act="v-open">${v.title}</button><span class="h-meta"><a href="#/channel/${encodeURIComponent(v.channel_id)}">${v.channel_title}</a><span class="sep">·</span>${detail(h)}</span>` : html`<span class="h-title">${h.event}</span>`}</span>
            <time class="h-time" datetime="${h.at}">${T.fmtTime(h.at)}</time></li>`;
        })}</ul></div>`)}
    </section>`;
  }

  function render(a) {
    const d = a.downloader;
    T.setHTML(root.querySelector('[data-slot="actions"]'), html`
      ${d.paused || d.state === 'paused' ? html`<button class="btn btn-primary" type="button" data-act="resume">${icon('play')}Resume downloads</button>` : html`<button class="btn" type="button" data-act="pause">${icon('pause')}Pause downloads</button>`}`);
    const parts = { now: nowCard(a), jobs: jobsCard(a), failed: failedCard(a), queue: queueCard(a), hist: historyCard(a) };
    const key = {
      now: a.current ? a.current.video.id + a.current.stage + d.state + ((a.active || []).length > 1) : 'idle' + d.state + d.waiting_count + d.wait_reason + JSON.stringify(d.pacing || {}),
      jobs: (a.active || []).map((j) => (j.video_id || j.title) + j.stage).join(),
      failed: a.failed.map((f) => f.video.id).join(),
      queue: a.queue.map((q) => q.video.id + (q.waits_for || '') + (q.video.download_now ? '!' : '')).join() + showAllQueue + d.state,
      hist: a.history.map((h) => h.id).join(),
    };
    Object.keys(parts).forEach((k) => {
      if (sig[k] === key[k]) return;
      sig[k] = key[k];
      T.setHTML(root.querySelector(`[data-slot="${k}"]`), parts[k]);
    });
    const sub = root.querySelector('[data-slot="sub"]');
    if (sub) sub.textContent = d.paused || d.state === 'paused' ? (T.dlJobs(d).length ? 'Paused: the running download finishes, nothing new starts.' : 'Paused. The queue keeps its order.')
      : d.pacing && d.pacing.cap_24h != null ? T.textOf(paceLine(d))
      : d.overnight ? 'One download at a time · new uploads first · backlog overnight (' + T.fmtHHMM(d.overnight.start) + '–' + T.fmtHHMM(d.overnight.end) + ')'
      : 'New uploads first, then the backlog';
  }
  async function load() {
    const a = await T.api.get('/api/activity');
    data = a;
    render(a);
  }
  function scheduleRefetch(ms) {
    clearTimeout(refetch);
    refetch = setTimeout(() => load().catch(() => {}), ms == null ? 400 : ms);
  }

  V.enter = async function (el) {
    root = el; sig = {}; showAllQueue = false; data = null;
    T.setHTML(root, html`<div class="page page-activity">
      <header class="page-head">
        <div class="page-title"><h1>Activity</h1><p class="page-sub" data-slot="sub">&nbsp;</p></div>
        <div class="page-actions" data-slot="actions"></div>
      </header>
      <div data-slot="now"><section class="now-card skel-now"><div class="thumb skel"></div><div class="skel-lines"><span class="skel skel-line"></span><span class="skel skel-line"></span><span class="skel skel-line short"></span></div></section></div>
      <div data-slot="jobs"></div>
      <div data-slot="failed"></div>
      <div class="act-grid"><div data-slot="queue"></div><div data-slot="hist"></div></div>
    </div>`);
    T.api.get('/api/settings').then((s) => { settings = s; }).catch(() => {});
    unsubs = [
      T.on('ev:video.progress', patchNow),
      T.on('ev:video.state', (d) => { ui.patchRows(d.video); scheduleRefetch(); }),
      T.on('ev:video.added', () => scheduleRefetch()),
      T.on('ev:history', () => scheduleRefetch()),
    ];
    try { await load(); } catch (e) { T.setHTML(root.querySelector('[data-slot="now"]'), ui.errorState(e)); }
    clearInterval(tick);
    tick = setInterval(() => { if (data && !data.current && data.downloader.wait_reason === 'pace') { sig.now = null; render(data); } }, 20000);
  };
  let tick = null;
  V.leave = function () { unsubs.forEach((u) => u()); unsubs = []; clearTimeout(refetch); clearInterval(tick); };
  V.refresh = () => load().catch(() => {});
  V.onStatus = function (s) {
    if (!data) return;
    const d = s.downloader, q = data.queue_total != null ? data.queue_total : data.queue.length, f = data.failed_total != null ? data.failed_total : data.failed.length;
    const jobsNow = T.dlJobs(d).map((j) => j.video_id).join(), jobsThen = (data.active || []).map((j) => j.video_id).join();
    if (d.state !== data.downloader.state || !!d.paused !== !!data.downloader.paused || d.wait_reason !== data.downloader.wait_reason || JSON.stringify(d.pacing || {}) !== JSON.stringify(data.downloader.pacing || {}) || d.queue_count !== q || d.failed_count !== f || (data.active && jobsNow !== jobsThen)) scheduleRefetch(1500);
  };
  V.act = function (name) {
    if (name === 'pause') { T.api.post('/api/downloader/pause').then(() => { T.toast('Downloads paused. A download that’s running finishes first.'); load(); if (T.live && T.live.poll) T.live.poll(); }).catch(T.toastError); return; }
    if (name === 'resume') { T.api.post('/api/downloader/resume').then(() => { T.toast('Downloads resumed.'); load(); if (T.live && T.live.poll) T.live.poll(); }).catch(T.toastError); return; }
    if (name === 'retry-all') { T.api.post('/api/downloader/retry-failed').then((r) => { T.toast(T.plural(r.queued, 'video') + ' queued again.'); load(); }).catch(T.toastError); return; }
    if (name === 'queue-all') { showAllQueue = !showAllQueue; sig.queue = null; render(data); return; }
    return false;
  };
})();
