/* Storage: how full the library is (fill and roll), what goes next, the biggest channels, and daily activity. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon, ui = T.ui;
  const TB = 1099511627776;

  const V = (T.views.storage = { nav: 'storage', title: 'Storage' });
  let root, data = null, unsubs = [], ro = null, tableView = false, refetch = null;

  function hero(s) {
    const f = s.fill, used = s.used_bytes, cap = s.cap_bytes, tgt = s.target_bytes;
    const pres = (s.preserved && s.preserved.bytes) || 0;
    const uR = Math.min(1, used / cap), tR = tgt / cap, pR = Math.min(uR, pres / cap);
    const rolling = f.state === 'rolling';
    const days = s.projection.days_to_target;
    return html`<section class="card store-hero is-${f.state}">
      <div class="sh-top">
        <div class="sh-fig">
          <span class="sh-label">In Plex now</span>
          <span class="hero-num">${(used / TB).toFixed(2)}<small>TB</small></span>
          <span class="sh-of">of the ${T.fmtBytes(tgt, { digits: 1 })} fill target · ${T.fmtBytes(cap, { digits: 1 })} quota</span>
        </div>
        <div class="sh-state">
          <span class="fill-pill is-${f.state} fill-pill-lg">${rolling ? icon('refresh') : icon('download')}${rolling ? 'Full and rolling' : 'Filling the backlog'}</span>
          <p class="sh-line">${rolling
            ? html`Each new video pushes one out. ${f.last_removed ? html`Last out: <b>${f.last_removed.video.title}</b> <span class="dim">(${f.last_removed.video.channel_title}, ${f.last_removed.reason === 'watched' ? 'watched' : 'making room'}, ${T.tAgo(f.last_removed.at)})</span>` : ''}`
            : html`Reaching back to <b>${f.reach_date ? T.fmtDay(f.reach_date, true) : '—'}</b> across your channels${days != null ? html` · full in about <b>${T.plural(days, 'day')}</b> at this pace` : ''}.`}</p>
        </div>
      </div>
      <div class="big-meter" role="img" aria-label="${T.fmtBytes(used) + ' used of ' + T.fmtBytes(cap) + '; fill target ' + T.fmtBytes(tgt)}">
        ${pres ? html`<i class="bm-kept" style="width:${Math.max(0.35, pR * 100).toFixed(2)}%" title="Kept after leaving YouTube: ${T.fmtBytes(pres)}"></i>` : ''}
        <i class="bm-used" style="left:${(pres ? Math.max(0.35, pR * 100) : 0).toFixed(2)}%;width:${Math.max(0, (uR - (pres ? Math.max(0.0035, pR) : 0)) * 100).toFixed(2)}%"></i>
        ${!rolling ? html`<i class="bm-tofill" style="left:${(uR * 100).toFixed(2)}%;width:${(Math.max(0, tR - uR) * 100).toFixed(2)}%"></i>` : ''}
        <em class="bm-target" style="left:${(tR * 100).toFixed(2)}%"><span>Fill target ${T.fmtBytes(tgt, { digits: 1 })}</span></em>
      </div>
      <div class="bm-legend">
        ${pres ? html`<span><i class="lg lg-kept"></i>Kept after leaving YouTube <b>${T.fmtBytes(pres)}</b> <span class="dim">(${T.plural(s.preserved.count, 'video')}, never rolled out)</span></span>` : ''}
        <span><i class="lg lg-used"></i>${pres ? 'Rolling library' : 'In Plex'} <b>${T.fmtBytes(used - pres)}</b></span>
        ${!rolling ? html`<span><i class="lg lg-tofill"></i>Still to fill <b>${T.fmtBytes(Math.max(0, tgt - used))}</b></span>` : ''}
        <span><i class="lg lg-head"></i>Headroom to the quota <b>${T.fmtBytes(cap - Math.max(used, tgt))}</b></span>
      </div>
      <details class="how">
        <summary>${icon('info')}How Tubarr uses the space</summary>
        <ol>
          <li><b>New uploads come first.</b> They download as soon as they’re found.</li>
          <li><b>The backlog fills in whenever there’s room</b>, round-robin across every channel, reaching further back in time, until the library reaches ${T.fmtBytes(tgt, { digits: 1 })}.</li>
          <li><b>Then it rolls.</b> Each new video pushes one out: watched videos first (oldest watched first), then the oldest backlog video from the biggest channel.</li>
          <li><b>Never removed automatically:</b> anything you marked Keep forever, channels set to Keep forever, each channel’s newest 3, and anything that has disappeared from YouTube.</li>
          <li><b>New uploads arrive within minutes:</b> every channel’s feed is checked every few minutes.</li>
        </ol>
        <a class="btn btn-sm" href="#/settings/downloads">${icon('sliders')}Change the fill target</a>
      </details>
    </section>`;
  }
  function tiles(s) {
    const tile = (label, value, sub, ic) => html`<div class="stat-tile"><span class="st-label">${icon(ic)}${label}</span><span class="st-value">${value}</span>${sub ? html`<span class="st-sub">${sub}</span>` : ''}</div>`;
    const days = s.projection.days_to_target;
    return html`<div class="tiles">
      ${tile('Videos in Plex', T.fmtNum(s.video_count), T.fmtNum(s.backfill_count) + ' from the backlog', 'film')}
      ${tile('Growth per day', T.fmtBytes(Math.max(0, s.projection.avg_bytes_per_day)), 'net, last 7 days', 'activity')}
      ${tile(s.fill.state === 'rolling' ? 'Fill state' : 'Full in', s.fill.state === 'rolling' ? 'Full' : days != null ? '~' + T.plural(days, 'day') : '—', s.fill.state === 'rolling' ? 'rolling at ' + T.fmtBytes(s.target_bytes, { digits: 1 }) : 'at this pace', 'calendar')}
      ${tile('Never removed', T.fmtNum(s.protected_count), s.kept_count + ' kept by you · newest 3 per channel', 'shield')}
      ${tile('Gone from YouTube, kept', T.fmtNum((s.preserved && s.preserved.count) || 0), (s.preserved && s.preserved.bytes ? T.fmtBytes(s.preserved.bytes) + ' · ' : '') + 'only here now', 'archive')}
      ${tile('Watched', T.fmtNum(s.watched_count), T.fmtBytes(s.watched_bytes) + ' · first to go', 'eye')}
      ${s.sponsorblock_saved_seconds ? tile('SponsorBlock saved', T.fmtSpan(s.sponsorblock_saved_seconds), 'of sponsor reads skipped', 'scissors') : ''}
    </div>`;
  }
  function nextList(s) {
    const rolling = s.fill.state === 'rolling';
    return html`<section class="card">
      <header class="card-head"><h2>${icon('trash')}Next in line to be removed</h2></header>
      <p class="card-lead">${rolling ? 'When the next video arrives, these go first, in this order.' : 'Nothing is removed while the library is filling. Once it reaches the target, these go first.'}</p>
      ${!s.next_to_remove.length ? ui.empty({ icon: 'shield', cls: 'empty-sm', title: 'Nothing can be removed', body: 'Everything in Plex is protected.' }) :
        html`<ol class="rm-list">${s.next_to_remove.map((x, i) => { T.cacheVideos([x.video]); return html`<li class="rm-row" data-vid="${x.video.id}">
          <span class="rm-n num">${i + 1}</span>${T.thumb(x.video, 'thumb-xs')}
          <span class="rm-main"><button class="rm-title" type="button" data-act="v-open">${x.video.title}</button>
            <span class="rm-meta"><a href="#/channel/${encodeURIComponent(x.video.channel_id)}">${x.video.channel_title}</a><span class="sep">·</span>uploaded ${T.fmtDay(x.video.upload_date, true)}<span class="sep">·</span>${T.fmtBytes(x.size_bytes)}</span></span>
          <span class="tag tag-sm ${x.reason === 'watched' ? '' : 'tag-warm'}">${x.reason === 'watched' ? html`${icon('eye')}Watched ${T.tAgo(x.watched_at)}` : html`${icon('storage')}Making room`}</span>
          <button class="btn-icon" type="button" data-act="v-keep-quick" title="Keep forever" aria-label="Keep ${x.video.title} forever">${icon('infinity')}</button>
        </li>`; })}</ol>`}
      ${s.recently_removed.length ? html`<h3 class="sub-h">Recently rolled out</h3><ul class="rm-list rm-past">${s.recently_removed.slice(0, 6).map((x) => html`<li class="rm-row" data-vid="${x.video.id}">
          <span class="rm-n">${icon('trash')}</span>${T.thumb(x.video, 'thumb-xs')}
          <span class="rm-main"><span class="rm-title">${x.video.title}</span><span class="rm-meta">${x.video.channel_title}<span class="sep">·</span>${x.reason === 'watched' ? 'watched' : 'making room'}<span class="sep">·</span>${T.tAgo(x.at)}</span></span>
          <button class="btn btn-xs" type="button" data-act="v-download">${icon('download')}Bring back</button></li>`)}</ul>` : ''}
    </section>`;
  }
  function biggest(s) {
    const max = Math.max(1, ...s.by_channel.map((c) => c.size_bytes));
    return html`<section class="card">
      <header class="card-head"><h2>${icon('tv')}Biggest channels</h2><a class="card-link" href="#/channels">All channels ${icon('chev-right')}</a></header>
      <p class="card-lead">When room is needed, the oldest backlog of the biggest channel goes first.</p>
      <ul class="big-list">${s.by_channel.map((c) => html`<li><a class="bl-row" href="#/channel/${encodeURIComponent(c.channel_id)}" title="${c.title}: ${T.fmtBytes(c.size_bytes)} · ${T.plural(c.video_count, 'video')}">
        ${T.poster({ title: c.title, poster_url: c.poster_url }, 'poster-xs')}
        <span class="bl-main"><span class="bl-name">${c.title}${c.retention_mode && c.retention_mode !== 'fill' ? html`<span class="tag tag-xs">${c.retention_mode === 'forever' ? 'Keep forever' : c.retention_mode === 'new_only' ? 'New only' : 'Own rule'}</span>` : ''}</span>
          <span class="bl-track"><i style="width:${((c.size_bytes / max) * 100).toFixed(1)}%"></i></span></span>
        <span class="bl-val num">${T.fmtBytes(c.size_bytes)}</span></a></li>`)}</ul>
    </section>`;
  }

  /* ---------- Daily chart: landed (new + backlog, stacked) above the baseline, rolled out below ---------- */
  function niceMax(v) { if (v <= 5) return 5; const p = Math.pow(10, Math.floor(Math.log10(v))); const n = v / p; return (n <= 1 ? 1 : n <= 2 ? 2 : n <= 2.5 ? 2.5 : n <= 5 ? 5 : 10) * p; }
  function chart(days, width) {
    const W = Math.max(280, width), H = 220, padL = 40, padR = 8, padT = 12, axisH = 24;
    const up = days.map((d) => d.new_videos + d.backfill_videos), down = days.map((d) => d.removed_videos || 0);
    const maxUp = niceMax(Math.max(1, ...up)), maxDown = Math.max(...down);
    const downShare = maxDown ? Math.min(0.3, Math.max(0.14, maxDown / (maxUp + maxDown))) : 0;
    const plotH = H - padT - axisH, upH = plotH * (1 - downShare), base = padT + upH;
    const scale = upH / maxUp;
    const band = (W - padL - padR) / days.length, bw = Math.max(3, Math.min(20, band * 0.62));
    const ticks = [0, maxUp / 2, maxUp];
    const x0 = (i) => padL + i * band + (band - bw) / 2;
    const r = Math.min(4, bw / 2);
    const colTop = (x, y, w, h) => h <= 0 ? '' : `M${x},${y + h}V${y + Math.min(r, h)}Q${x},${y} ${x + Math.min(r, w / 2)},${y}H${x + w - Math.min(r, w / 2)}Q${x + w},${y} ${x + w},${y + Math.min(r, h)}V${y + h}Z`;
    const colBot = (x, y, w, h) => h <= 0 ? '' : `M${x},${y}V${y + h - Math.min(r, h)}Q${x},${y + h} ${x + Math.min(r, w / 2)},${y + h}H${x + w - Math.min(r, w / 2)}Q${x + w},${y + h} ${x + w},${y + h - Math.min(r, h)}V${y}Z`;
    let svg = `<svg class="chart" viewBox="0 0 ${W} ${H}" width="${W}" height="${H}" role="img" aria-label="Videos per day for the last 30 days">`;
    ticks.forEach((t) => { const y = base - t * scale; svg += `<line class="grid" x1="${padL}" x2="${W - padR}" y1="${y.toFixed(1)}" y2="${y.toFixed(1)}"/><text class="tick" x="${padL - 8}" y="${(y + 4).toFixed(1)}" text-anchor="end">${T.fmtNum(Math.round(t))}</text>`; });
    days.forEach((d, i) => {
      const x = x0(i), hb = d.backfill_videos * scale, hn = d.new_videos * scale, gap = hb > 0 && hn > 0 ? 2 : 0;
      // backlog sits on the baseline; new uploads stack on top with a 2px surface gap; the top segment gets the rounded end
      if (hb > 0) svg += hn > 0 ? `<rect class="s-backfill" x="${x.toFixed(1)}" y="${(base - hb).toFixed(1)}" width="${bw.toFixed(1)}" height="${hb.toFixed(1)}"/>` : `<path class="s-backfill" d="${colTop(x, base - hb, bw, hb)}"/>`;
      if (hn > 0) svg += `<path class="s-new" d="${colTop(x, base - hb - gap - hn, bw, hn)}"/>`;
      const hr = (d.removed_videos || 0) * scale;
      if (hr > 0) svg += `<path class="s-removed" d="${colBot(x, base + 2, bw, Math.max(2, hr))}"/>`;
      svg += `<rect class="hit" x="${(padL + i * band).toFixed(1)}" y="${padT}" width="${band.toFixed(1)}" height="${plotH}" data-i="${i}" tabindex="0" aria-label="${T.fmtDay(d.date, false)}: ${d.new_videos} new, ${d.backfill_videos} backlog, ${d.removed_videos || 0} rolled out"/>`;
      if (i % 5 === 0 || i === days.length - 1) svg += `<text class="tick" x="${(padL + i * band + band / 2).toFixed(1)}" y="${H - 6}" text-anchor="middle">${i === days.length - 1 ? 'Today' : T.fmtDay(d.date, false)}</text>`;
    });
    svg += `<line class="axis" x1="${padL}" x2="${W - padR}" y1="${base.toFixed(1)}" y2="${base.toFixed(1)}"/></svg>`;
    return svg;
  }
  function chartCard(s) {
    const tot = s.daily.reduce((a, d) => { a.n += d.new_videos; a.b += d.backfill_videos; a.r += d.removed_videos || 0; return a; }, { n: 0, b: 0, r: 0 });
    return html`<section class="card chart-card">
      <header class="card-head"><h2>${icon('activity')}Videos per day</h2>
        <div class="seg seg-sm" role="radiogroup" aria-label="Chart or table"><label class="seg-opt"><input type="radio" name="cv" value="chart" ${tableView ? '' : T.raw('checked')}><span>Chart</span></label><label class="seg-opt"><input type="radio" name="cv" value="table" ${tableView ? T.raw('checked') : ''}><span>Table</span></label></div></header>
      <p class="card-lead">Last 30 days: ${T.fmtNum(tot.n)} new uploads and ${T.fmtNum(tot.b)} backlog videos landed${tot.r ? html`, ${T.fmtNum(tot.r)} rolled out` : ''}.</p>
      <div class="legend"><span><i class="sw sw-new"></i>New uploads</span><span><i class="sw sw-backfill"></i>Backlog</span><span><i class="sw sw-removed"></i>Rolled out (below the line)</span></div>
      <div class="chart-box" data-slot="chart"></div>
      <div class="chart-tip" data-slot="tip" role="status" hidden></div>
    </section>`;
  }
  function drawChart() {
    const box = root && root.querySelector('[data-slot="chart"]');
    if (!box || !data) return;
    if (tableView) {
      T.setHTML(box, html`<div class="table-wrap"><table class="table table-sm"><thead><tr><th>Day</th><th class="num">New</th><th class="num">Backlog</th><th class="num">Rolled out</th><th class="num">Landed</th></tr></thead>
        <tbody>${data.daily.slice().reverse().map((d) => html`<tr><td>${T.fmtDay(d.date, false)}</td><td class="num">${d.new_videos}</td><td class="num">${d.backfill_videos}</td><td class="num">${d.removed_videos || 0}</td><td class="num">${T.fmtBytes(d.bytes)}</td></tr>`)}</tbody></table></div>`);
      return;
    }
    box.innerHTML = chart(data.daily, box.clientWidth);
  }
  function bindChart() {
    const card = root.querySelector('.chart-card');
    if (!card) return;
    const tip = card.querySelector('[data-slot="tip"]');
    const show = (el) => {
      const d = data.daily[Number(el.dataset.i)];
      T.setHTML(tip, html`<b>${T.fmtDay(d.date, false)}</b><span><i class="sw sw-new"></i><b>${d.new_videos}</b> new</span><span><i class="sw sw-backfill"></i><b>${d.backfill_videos}</b> backlog</span><span><i class="sw sw-removed"></i><b>${d.removed_videos || 0}</b> rolled out</span><span class="dim">${T.fmtBytes(d.bytes)} landed</span>`);
      tip.hidden = false;
      const r = el.getBoundingClientRect(), c = card.getBoundingClientRect();
      const left = T.clamp(r.left - c.left + r.width / 2 - tip.offsetWidth / 2, 8, c.width - tip.offsetWidth - 8);
      tip.style.left = left + 'px'; tip.style.top = (r.top - c.top - tip.offsetHeight + 18) + 'px';
      card.querySelectorAll('.hit.on').forEach((h) => h.classList.remove('on')); el.classList.add('on');
    };
    const hide = () => { tip.hidden = true; card.querySelectorAll('.hit.on').forEach((h) => h.classList.remove('on')); };
    card.addEventListener('pointerover', (e) => { const h = e.target.closest('.hit'); if (h) show(h); });
    card.addEventListener('pointerleave', hide);
    card.addEventListener('focusin', (e) => { const h = e.target.closest('.hit'); if (h) show(h); });
    card.addEventListener('focusout', hide);
    card.addEventListener('change', (e) => { if (e.target.name === 'cv') { tableView = e.target.value === 'table'; drawChart(); } });
  }

  function render(s) {
    T.setHTML(root.querySelector('[data-slot="body"]'), html`
      ${hero(s)}
      ${tiles(s)}
      <div class="store-grid">${nextList(s)}${biggest(s)}</div>
      ${chartCard(s)}`);
    const sub = root.querySelector('[data-slot="sub"]');
    if (sub) sub.textContent = s.path || 'The library folder';
    drawChart(); bindChart();
  }
  async function load() {
    data = await T.api.get('/api/storage');
    render(data);
  }

  V.enter = async function (el) {
    root = el; data = null;
    T.setHTML(root, html`<div class="page page-storage">
      <header class="page-head"><div class="page-title"><h1>Storage</h1><p class="page-sub"><span data-slot="sub">The library folder</span> · updates live</p></div>
        <div class="page-actions"><a class="btn" href="#/settings/downloads">${icon('sliders')}Fill target &amp; rules</a></div></header>
      <div data-slot="body"><section class="card store-hero skel-hero"><span class="skel skel-line"></span><span class="skel skel-bar"></span></section></div>
    </div>`);
    ro = new ResizeObserver(T.debounce(drawChart, 80));
    unsubs = [
      T.on('ev:storage', () => { clearTimeout(refetch); refetch = setTimeout(() => load().catch(() => {}), 600); }),
    ];
    try { await load(); ro.observe(root.querySelector('[data-slot="body"]')); } catch (e) { T.setHTML(root.querySelector('[data-slot="body"]'), ui.errorState(e)); }
  };
  V.leave = function () { unsubs.forEach((u) => u()); unsubs = []; if (ro) ro.disconnect(); ro = null; clearTimeout(refetch); };
  V.refresh = () => load().catch(() => {});
  V.act = function (name, el) {
    if (name === 'v-keep-quick') {
      const id = el.closest('[data-vid]').dataset.vid;
      T.api.patch('/api/videos/' + id, { keep: true }).then((v) => { T.toast('Kept forever: ' + v.title); load(); }).catch(T.toastError);
      return;
    }
    return false;
  };
})();
