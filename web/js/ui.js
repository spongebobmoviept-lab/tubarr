/* Tubarr web UI: shared components (toasts, modals, menus, badges, form controls). */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon;
  const ui = (T.ui = {});

  /* ---------- Toasts ---------- */
  T.toast = function (message, opts) {
    const o = opts || {};
    const box = document.getElementById('toasts');
    if (!box) return;
    const el = document.createElement('div');
    const type = o.type || 'success';
    el.className = 'toast toast-' + type;
    el.setAttribute('role', type === 'error' ? 'alert' : 'status');
    const ic = { success: 'check-circle', error: 'alert-circle', info: 'info', warn: 'alert' }[type];
    T.setHTML(el, html`${icon(ic, 'toast-ic')}<div class="toast-msg">${message}</div>${o.action ? html`<button class="toast-act" type="button">${o.action.label}</button>` : ''}<button class="toast-x" type="button" aria-label="Dismiss">${icon('x')}</button>`);
    box.appendChild(el);
    requestAnimationFrame(() => el.classList.add('in'));
    let timer;
    const close = () => { clearTimeout(timer); el.classList.remove('in'); el.classList.add('out'); setTimeout(() => el.remove(), 260); };
    el.querySelector('.toast-x').onclick = close;
    if (o.action) el.querySelector('.toast-act').onclick = () => { close(); o.action.fn(); };
    const arm = () => { timer = setTimeout(close, o.timeout || (type === 'error' ? 7000 : 4200)); };
    el.addEventListener('mouseenter', () => clearTimeout(timer));
    el.addEventListener('mouseleave', arm);
    arm();
    return { close };
  };
  T.toastError = (e, prefix) => {
    // 501 = a feature the server doesn't do yet: a calm note, not an error
    if (e && e.code === 'not_implemented') return T.toast(e.message || 'Coming soon.', { type: 'info', timeout: 6500 });
    return T.toast((prefix ? prefix + ' ' : '') + (e && e.message ? e.message : 'Something went wrong.'), { type: 'error' });
  };
  T.soon = (e) => !!(e && e.code === 'not_implemented');

  /* ---------- Modals (focus-trapped, Esc closes, restores focus) ---------- */
  let modalSeq = 0;
  T.modal = function (o) {
    const root = document.getElementById('modal-root');
    const id = 'm' + ++modalSeq;
    const wrap = document.createElement('div');
    wrap.className = 'modal-wrap';
    T.setHTML(wrap, html`<div class="modal-backdrop" data-close></div>
      <div class="modal ${o.size || ''}" role="dialog" aria-modal="true" aria-labelledby="${id}-t">
        <header class="modal-head">${o.icon ? html`<span class="modal-ic ${o.tone ? 'tone-' + o.tone : ''}">${icon(o.icon)}</span>` : ''}<h2 id="${id}-t">${o.title}</h2>
          <button class="btn-icon modal-x" type="button" data-close aria-label="Close">${icon('x')}</button></header>
        <div class="modal-body">${o.body || ''}</div>
        ${o.footer ? html`<footer class="modal-foot">${o.footer}</footer>` : ''}
      </div>`);
    root.appendChild(wrap);
    document.body.classList.add('has-modal');
    const prev = document.activeElement;
    const dialog = wrap.querySelector('.modal');
    let closed = false;
    const ctl = {
      el: wrap, dialog,
      $: (s) => wrap.querySelector(s),
      body: (raw) => T.setHTML(wrap.querySelector('.modal-body'), raw),
      footer: (raw) => {
        let f = wrap.querySelector('.modal-foot');
        if (!f) { f = document.createElement('footer'); f.className = 'modal-foot'; dialog.appendChild(f); }
        T.setHTML(f, raw);
      },
      close(result) {
        if (closed) return;
        closed = true;
        wrap.classList.remove('open');
        document.removeEventListener('keydown', onKey, true);
        setTimeout(() => { wrap.remove(); if (!document.querySelector('.modal-wrap')) document.body.classList.remove('has-modal'); }, 220);
        if (prev && prev.focus) try { prev.focus({ preventScroll: true }); } catch (e) { /* gone */ }
        if (o.onClose) o.onClose(result);
      },
    };
    function focusables() { return Array.from(dialog.querySelectorAll('a[href],button:not([disabled]),input:not([disabled]):not([type=hidden]),select:not([disabled]),textarea:not([disabled]),[tabindex]:not([tabindex="-1"])')).filter((x) => x.offsetParent !== null); }
    function onKey(e) {
      if (e.key === 'Escape') { e.preventDefault(); ctl.close(); return; }
      if (e.key === 'Tab') {
        const f = focusables(); if (!f.length) return;
        const first = f[0], last = f[f.length - 1];
        if (e.shiftKey && document.activeElement === first) { e.preventDefault(); last.focus(); }
        else if (!e.shiftKey && document.activeElement === last) { e.preventDefault(); first.focus(); }
      }
    }
    document.addEventListener('keydown', onKey, true);
    wrap.addEventListener('click', (e) => { if (e.target.closest('[data-close]')) ctl.close(); });
    dialog.setAttribute('tabindex', '-1');
    requestAnimationFrame(() => {
      wrap.classList.add('open');
      const target = dialog.querySelector('[autofocus]') || dialog.querySelector('.modal-body input:not([type=radio]):not([type=checkbox]):not([type=file]),.modal-body textarea') || dialog.querySelector('.modal-foot .btn-primary:not([disabled])') || dialog;
      target.focus({ preventScroll: true });
    });
    if (o.onMount) o.onMount(ctl);
    return ctl;
  };

  /* Confirm dialog. Resolves false, or an object with the values of any inputs in the body. */
  T.confirm = function (o) {
    return new Promise((resolve) => {
      let answered = false;
      const m = T.modal({
        title: o.title, icon: o.icon || (o.danger ? 'alert' : 'info'), tone: o.danger ? 'crit' : o.tone || 'brand', size: 'modal-sm',
        body: o.body,
        footer: html`<button class="btn" type="button" data-answer="no">${o.cancel || 'Cancel'}</button>
                     <button class="btn ${o.danger ? 'btn-danger' : 'btn-primary'}" type="button" data-answer="yes">${o.confirm || 'OK'}</button>`,
        onClose: () => { if (!answered) resolve(false); },
      });
      m.el.addEventListener('click', (e) => {
        const b = e.target.closest('[data-answer]');
        if (!b) return;
        answered = true;
        if (b.dataset.answer === 'yes') {
          const vals = {};
          m.el.querySelectorAll('.modal-body input, .modal-body select').forEach((i) => { vals[i.name] = i.type === 'checkbox' ? i.checked : i.value; });
          resolve(vals);
        } else resolve(false);
        m.close();
      });
    });
  };

  /* ---------- Popover menu (the "⋯" buttons) ---------- */
  let openMenu = null;
  T.menu = function (anchor, items) {
    if (openMenu) { const was = openMenu.anchor; openMenu.close(); if (was === anchor) return; }
    const el = document.createElement('div');
    el.className = 'menu';
    el.setAttribute('role', 'menu');
    T.setHTML(el, items.filter(Boolean).map((it, i) => it === '-' ? html`<div class="menu-sep" role="separator"></div>`
      : it.href ? html`<a class="menu-item ${it.danger ? 'danger' : ''}" role="menuitem" href="${it.href}" ${it.external ? T.raw('target="_blank" rel="noopener noreferrer"') : ''} data-i="${i}">${icon(it.icon || 'chev-right')}<span>${it.label}</span>${it.external ? icon('ext', 'menu-ext') : ''}</a>`
      : html`<button class="menu-item ${it.danger ? 'danger' : ''}" role="menuitem" type="button" data-i="${i}" ${it.disabled ? T.raw('disabled') : ''}>${icon(it.icon || 'chev-right')}<span>${it.label}</span></button>`));
    document.body.appendChild(el);
    const r = anchor.getBoundingClientRect();
    const w = el.offsetWidth, h = el.offsetHeight;
    let left = Math.min(window.innerWidth - w - 8, Math.max(8, r.right - w));
    let top = r.bottom + 6;
    if (top + h > window.innerHeight - 8) top = Math.max(8, r.top - h - 6);
    el.style.left = left + 'px'; el.style.top = top + 'px';
    anchor.setAttribute('aria-expanded', 'true');
    requestAnimationFrame(() => el.classList.add('open'));
    const list = items.filter(Boolean);
    const close = () => {
      if (!el.isConnected) return;
      el.remove(); anchor.setAttribute('aria-expanded', 'false');
      document.removeEventListener('pointerdown', outside, true); document.removeEventListener('keydown', key, true);
      window.removeEventListener('scroll', close, true); window.removeEventListener('resize', close);
      openMenu = null;
    };
    const outside = (e) => { if (!el.contains(e.target) && !anchor.contains(e.target)) close(); };
    const key = (e) => {
      const btns = Array.from(el.querySelectorAll('.menu-item:not([disabled])'));
      const i = btns.indexOf(document.activeElement);
      if (e.key === 'Escape') { e.preventDefault(); close(); anchor.focus(); }
      else if (e.key === 'ArrowDown') { e.preventDefault(); (btns[i + 1] || btns[0]).focus(); }
      else if (e.key === 'ArrowUp') { e.preventDefault(); (btns[i - 1] || btns[btns.length - 1]).focus(); }
      else if (e.key === 'Tab') close();
    };
    el.addEventListener('click', (e) => {
      const b = e.target.closest('.menu-item'); if (!b) return;
      const it = list[Number(b.dataset.i)];
      close();
      if (it && it.fn) { e.preventDefault(); it.fn(); }
    });
    document.addEventListener('pointerdown', outside, true);
    document.addEventListener('keydown', key, true);
    window.addEventListener('scroll', close, true); window.addEventListener('resize', close);
    const first = el.querySelector('.menu-item'); if (first) first.focus({ preventScroll: true });
    openMenu = { close, anchor };
    return openMenu;
  };

  /* ---------- Labels ---------- */
  ui.TOPICS = [['cars', 'Cars & Builds'], ['tech', 'Tech & PCs'], ['science', 'Science & Engineering'], ['space', 'Space'], ['aviation', 'Aviation'], ['gaming', 'Gaming'], ['history', 'History & Stories'], ['makers', 'Makers & DIY'], ['music', 'Music'], ['other', 'Other']];
  ui.topicLabel = (id) => (ui.TOPICS.find((t) => t[0] === id) || [id, 'Other'])[1];
  ui.PLAYLIST_WHY = { not_numbered: 'Titles don’t look like numbered parts', mixed_channels: 'Collects videos from other channels', compilation: 'A compilation, not one story', too_small: 'Fewer than 3 videos', shorts: 'Mostly Shorts', live: 'A stream archive' };
  ui.STAGES = { download: 'Downloading', sponsorblock: 'Cutting sponsors', artwork: 'Building artwork', plex: 'Adding to Plex' };
  ui.STAGE_ICONS = { download: 'download', sponsorblock: 'scissors', artwork: 'image', plex: 'film' };
  ui.REASONS = { new_upload: 'New upload', backfill: 'Backfill', retry: 'Retry', manual: 'Manual', upgrade: 'Better copy (4K)' };
  /* "2160p" -> "4K" for people; long=true: "Best (up to 4K)" for the default quality setting */
  T.fmtRes = (r, long) => (r === '2160p' ? (long ? 'best, up to 4K' : '4K') : r || '');
  ui.SB = [
    ['sponsor', 'Sponsor'], ['selfpromo', 'Self-promotion'], ['interaction', 'Like/subscribe reminders'],
    ['outro', 'End cards'], ['preview', 'Previews/recaps'], ['filler', 'Tangents'], ['music_offtopic', 'Non-music in music videos'],
  ];

  /* ---------- Channel status badge ---------- */
  ui.channelBadge = function (ch, opts) {
    const o = opts || {};
    switch (ch.status) {
      case 'syncing': {
        const sp = ch.sync_progress;
        const pct = sp && sp.total ? ' ' + Math.round((100 * (sp.done || 0)) / sp.total) + '%' : '';
        return html`<span class="badge b-info"><span class="spin" aria-hidden="true"></span>Syncing${o.compact ? '' : pct}</span>`;
      }
      case 'pending_removal': return html`<span class="badge b-warn">${icon('timer')}Pending removal</span>`;
      case 'removed': return html`<span class="badge b-gone" title="${ch.status_detail || ''}">${icon('archive')}Removed · kept videos only</span>`;
      case 'error': return html`<span class="badge b-crit" title="${ch.status_detail || ''}">${icon('alert')}Error</span>`;
      case 'gone': return html`<span class="badge b-gone" title="${ch.gone ? 'Gone from YouTube since ' + T.fmtDay(ch.gone.at, true) + '. Everything already downloaded is kept.' : ''}">${icon('archive')}Gone from YouTube</span>`;
      default: return html`<span class="badge b-good" title="Monitored"><i class="dot" aria-hidden="true"></i><span class="badge-t">Monitored</span></span>`;
    }
  };

  /* (The video status chip lives in videos.js.) */
  ui.minutesLabel = (m) => (!m ? 'the limit' : m % 60 === 0 ? m / 60 + ' h' : m > 60 ? Math.floor(m / 60) + ' h ' + (m % 60) + ' min' : m + ' min');

  /* Small progress ring used inline. */
  ui.ring = function (p) {
    const c = 2 * Math.PI * 7;
    return T.raw(`<svg class="ring" viewBox="0 0 18 18" aria-hidden="true"><circle cx="9" cy="9" r="7" class="ring-track"/><circle cx="9" cy="9" r="7" class="ring-fill" data-live="ring" stroke-dasharray="${c.toFixed(2)}" stroke-dashoffset="${(c * (1 - T.clamp(p, 0, 1))).toFixed(2)}"/></svg>`);
  };
  ui.setRing = function (svgCircle, p) {
    if (!svgCircle) return;
    const c = 2 * Math.PI * 7;
    svgCircle.setAttribute('stroke-dashoffset', (c * (1 - T.clamp(p, 0, 1))).toFixed(2));
  };

  /* Linear progress bar; p = null -> indeterminate. */
  ui.bar = function (p, cls) {
    const ind = p == null;
    return html`<div class="bar ${cls || ''} ${ind ? 'is-ind' : ''}" role="progressbar" aria-valuemin="0" aria-valuemax="100" ${ind ? '' : T.raw('aria-valuenow="' + Math.round(p * 100) + '"')}><i style="${ind ? '' : 'width:' + (T.clamp(p, 0, 1) * 100).toFixed(2) + '%'}"></i></div>`;
  };

  /* ---------- Form controls ---------- */
  ui.switch = function (name, checked, o) {
    o = o || {};
    return html`<label class="switch ${o.disabled ? 'is-disabled' : ''}"><input type="checkbox" role="switch" name="${name}" ${checked ? T.raw('checked') : ''} ${o.disabled ? T.raw('disabled') : ''} ${o.attrs ? T.raw(o.attrs) : ''} ${o.label ? T.raw('aria-label="' + T.esc(o.label) + '"') : ''}><span class="switch-track" aria-hidden="true"><span class="switch-thumb"></span></span>${o.text ? html`<span class="switch-text">${o.text}</span>` : ''}</label>`;
  };
  ui.seg = function (name, options, value, o) {
    o = o || {};
    return html`<div class="seg ${o.cls || ''}" role="radiogroup" ${o.label ? T.raw('aria-label="' + T.esc(o.label) + '"') : ''}>${options.map(([v, label, ic]) => html`<label class="seg-opt"><input type="radio" name="${name}" value="${v}" ${String(v) === String(value) ? T.raw('checked') : ''} ${o.attrs ? T.raw(o.attrs) : ''}><span>${ic ? icon(ic) : ''}${label ? html`<span class="seg-l">${label}</span>` : ''}</span></label>`)}</div>`;
  };
  ui.select = function (name, options, value, o) {
    o = o || {};
    return html`<span class="select ${o.cls || ''}"><select name="${name}" ${o.attrs ? T.raw(o.attrs) : ''} ${o.label ? T.raw('aria-label="' + T.esc(o.label) + '"') : ''}>${options.map(([v, label]) => html`<option value="${v}" ${String(v) === String(value) ? T.raw('selected') : ''}>${label}</option>`)}</select>${icon('chev-down', 'select-chev')}</span>`;
  };

  /* ---------- Empty / error states ---------- */
  ui.empty = function (o) {
    return html`<div class="empty ${o.cls || ''}">
      <div class="empty-art">${o.art || html`<span class="empty-ic">${icon(o.icon || 'inbox')}</span>`}</div>
      <h3>${o.title}</h3>
      ${o.body ? html`<p>${o.body}</p>` : ''}
      ${o.actions ? html`<div class="empty-actions">${o.actions}</div>` : ''}
    </div>`;
  };
  ui.errorState = function (err, act) {
    return ui.empty({
      icon: err && err.code === 'offline' ? 'offline' : 'alert-circle', cls: 'is-error',
      title: err && err.code === 'offline' ? "Can't reach Tubarr" : "This page didn't load",
      body: (err && err.message) || 'Something went wrong.',
      actions: html`<button class="btn" type="button" data-act="${act || 'reload'}">${icon('refresh')}Try again</button>`,
    });
  };

  /* A little "saved" tick that fades out next to a heading. */
  ui.flashSaved = function (el) {
    if (!el) return;
    el.classList.remove('show'); void el.offsetWidth; el.classList.add('show');
  };
})();
