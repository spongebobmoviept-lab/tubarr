/* Tubarr web UI: ONE shared 1-second ticker that keeps every time on screen alive between server updates.
   Views render times through T.tAgo / T.tUntil / T.tElapsed (a <span data-tick=...> with the timestamp in it);
   the ticker rewrites those spans every second from the timestamps alone, no server round trip.
   Download progress is interpolated from the last known speed and re-emitted as ordinary `ev:video.progress`
   events (flagged interp), so every view's existing progress patching animates it. The ticker pauses while the
   tab is hidden; coming back ticks at once and asks the live layer to resync from the server. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html;

  /* ---------- server clock (so countdowns agree with the server even if this device's clock is off) ---------- */
  T.clockOffset = 0;
  T.now = () => Date.now() + T.clockOffset;
  T.on('status', (s) => {
    const off = s && s.server_time ? Date.parse(s.server_time) - Date.now() : NaN;
    if (isFinite(off)) T.clockOffset = Math.abs(off) > 2000 ? off : 0;
  });

  /* 402 -> "6:42", 5400 -> "1h 30m" */
  T.fmtCountdown = (s) => {
    s = Math.max(0, Math.ceil(s));
    if (s < 3600) return Math.floor(s / 60) + ':' + String(s % 60).padStart(2, '0');
    if (s < 86400) return Math.floor(s / 3600) + 'h ' + String(Math.floor((s % 3600) / 60)).padStart(2, '0') + 'm';
    return T.fmtSpan(s);
  };

  /* ---------- ticking spans ---------- */
  /* sec: show seconds under a minute ("12s ago") instead of "just now" */
  const agoText = (iso, sec) => { const s = (T.now() - Date.parse(iso)) / 1000; return sec && s >= 0 && s < 60 ? Math.round(s) + 's ago' : T.fmtAgo(iso); };
  T.tAgo = (iso, sec) => (iso ? html`<span data-tick="ago" data-t="${iso}" data-sec="${sec ? 1 : ''}">${agoText(iso, sec)}</span>` : '—');
  /* opts: pre/post text around the countdown, zero = text once it reaches 0, fmt 'clock' (6:42) or 'span' (23h 51m) */
  T.tUntil = (iso, opts) => {
    if (!iso) return '—';
    const o = opts || {};
    return html`<span data-tick="until" data-t="${iso}" data-fmt="${o.fmt || 'clock'}" data-pre="${o.pre || ''}" data-post="${o.post || ''}" data-zero="${o.zero || 'now'}">${untilText(iso, o.fmt || 'clock', o.pre || '', o.post || '', o.zero || 'now')}</span>`;
  };
  T.tElapsed = (startMs) => html`<span data-tick="elapsed" data-t0="${Math.round(startMs)}">${T.fmtClock(Math.max(0, (T.now() - startMs) / 1000))}</span>`;
  /* "3:07 PM · 12m ago" */
  T.tClockAgo = (iso, sec) => (iso ? html`${T.fmtTime(iso)} · ${T.tAgo(iso, sec)}` : '—');

  function untilText(iso, fmt, pre, post, zero) {
    const s = (Date.parse(iso) - T.now()) / 1000;
    if (!(s > 0)) return zero;
    return pre + (fmt === 'span' ? (s < 60 ? Math.ceil(s) + 's' : T.fmtSpan(s)) : T.fmtCountdown(s)) + post;
  }

  /* ---------- download progress, interpolated between real updates ---------- */
  const MAX_AHEAD = 12;                     // never run more than 12 s ahead of the last real number
  const prog = new Map();                   // video id -> { p, total, speed, at, sig }
  function learn(id, p, total, speed, stage) {
    if (!id) return;
    if (stage !== 'download' || p == null) { prog.delete(id); return; }
    const sig = p + '|' + speed + '|' + total;
    const cur = prog.get(id);
    if (cur && cur.sig === sig) return;     // the same real numbers again: keep interpolating from the first time
    prog.set(id, { p, total, speed: speed || 0, at: T.now(), sig });
  }
  T.on('status', (s) => {
    const d = s && s.downloader;
    if (!d) return;
    const jobs = Array.isArray(d.active) ? d.active : [];
    const seen = new Set();
    jobs.forEach((j) => { if (j.video_id) { seen.add(j.video_id); learn(j.video_id, j.progress, j.bytes_total, j.speed_bps, j.stage); } });
    Array.from(prog.keys()).forEach((id) => { if (!seen.has(id)) prog.delete(id); });
  });
  T.on('ev:video.progress', (p) => { if (p && !p.interp) learn(p.id, p.progress, p.bytes_total, p.speed_bps, p.stage); });
  /* The interpolated progress for a video right now (or null). */
  T.liveProgress = (id) => {
    const b = prog.get(id);
    if (!b || !b.total || !b.speed) return b ? b.p : null;
    const dt = Math.min(MAX_AHEAD, Math.max(0, (T.now() - b.at) / 1000));
    return Math.min(0.999, b.p + (b.speed * dt) / b.total);
  };
  function tickProgress() {
    prog.forEach((b, id) => {
      if (!b.total || !b.speed) return;
      const p = T.liveProgress(id);
      const done = Math.round(p * b.total);
      T.emit('ev:video.progress', { id, stage: 'download', progress: p, bytes_done: done, bytes_total: b.total,
        speed_bps: b.speed, eta_seconds: Math.max(0, Math.round((b.total - done) / b.speed)), interp: true });
    });
  }

  /* ---------- the one ticker ---------- */
  const fns = new Set();
  T.onTick = (fn) => { fns.add(fn); return () => fns.delete(fn); };
  function tick() {
    const now = T.now();
    document.querySelectorAll('[data-tick]').forEach((el) => {
      const k = el.dataset.tick;
      let txt = null;
      if (k === 'ago') txt = agoText(el.dataset.t, !!el.dataset.sec);
      else if (k === 'until') txt = untilText(el.dataset.t, el.dataset.fmt, el.dataset.pre || '', el.dataset.post || '', el.dataset.zero || 'now');
      else if (k === 'elapsed') txt = T.fmtClock(Math.max(0, (now - Number(el.dataset.t0)) / 1000));
      if (txt != null && el.textContent !== txt) el.textContent = txt;
    });
    tickProgress();
    fns.forEach((fn) => { try { fn(now); } catch (e) { console.error(e); } });
  }
  let timer = null;
  const start = () => { if (!timer) timer = setInterval(tick, 1000); };
  const stop = () => { clearInterval(timer); timer = null; };
  document.addEventListener('visibilitychange', () => {
    if (document.visibilityState === 'hidden') { stop(); return; }
    tick(); start();
    T.emit('resume');                       // the live layer refetches status + the open view
  });
  T.tickNow = tick;
  if (document.visibilityState !== 'hidden') start();
})();
