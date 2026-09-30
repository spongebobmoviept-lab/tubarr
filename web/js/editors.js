/* Tubarr web UI: editors and dialogs. Video sheet (details + edit), channel editor (name, genre, summary,
   poster), Add channel, and the retention control with its live space estimate. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon, ui = T.ui;

  const GENRES = ['Autos & Vehicles', 'Comedy', 'Education', 'Entertainment', 'Film & Animation', 'Gaming', 'Howto & Style', 'Music', 'News & Politics', 'People & Blogs', 'Science & Technology', 'Sports', 'Travel & Events'];

  /* ---------- Picker grid shared by posters and thumbnails ---------- */
  function pickerGrid(name, variants, current, kind) {
    return html`<div class="picker picker-${kind}" role="radiogroup" aria-label="${kind === 'poster' ? 'Poster' : 'Thumbnail'}">
      ${variants.map((v) => html`<label class="pick"><input type="radio" name="${name}" value="${v.id}" ${v.id === current ? T.raw('checked') : ''}>
        <span class="pick-img">${v.url ? html`<img src="${v.url}" alt="" loading="lazy">` : ''}</span><span class="pick-l">${v.label}</span>${icon('check', 'pick-check')}</label>`)}
      <label class="pick pick-upload"><input type="file" accept="image/jpeg,image/png,image/webp" data-upload>
        <span class="pick-img"><span class="pick-up">${icon('image')}<span>Upload your own</span><small>JPEG, PNG or WebP, up to 10 MB</small></span></span><span class="pick-l">Upload</span></label>
    </div>`;
  }
  function bindUpload(root, onFile) {
    const inp = root.querySelector('[data-upload]');
    if (!inp) return;
    inp.addEventListener('change', () => {
      const f = inp.files && inp.files[0];
      if (!f) return;
      if (!/^image\/(jpeg|png|webp)$/.test(f.type)) { T.toast('Choose a JPEG, PNG or WebP image.', { type: 'error' }); return; }
      if (f.size > 10 * 1048576) { T.toast('That image is over 10 MB.', { type: 'error' }); return; }
      const url = URL.createObjectURL(f);
      const box = inp.closest('.pick');
      T.setHTML(box.querySelector('.pick-img'), html`<img src="${url}" alt="">`);
      box.querySelector('.pick-l').textContent = f.name.length > 22 ? f.name.slice(0, 20) + '…' : f.name;
      root.querySelectorAll('.picker input[type=radio]').forEach((r) => { r.checked = false; });
      box.classList.add('is-chosen');
      onFile(f);
    });
    root.querySelectorAll('.picker input[type=radio]').forEach((r) => r.addEventListener('change', () => { const b = inp.closest('.pick'); b.classList.remove('is-chosen'); onFile(null); }));
  }

  /* ---------- Video sheet: details, quick actions, and "Edit info" ---------- */
  T.openVideo = async function (id, opts) {
    const o = opts || {};
    const m = T.modal({ title: 'Video', icon: 'film', size: 'modal-lg', body: html`<div class="modal-loading"><span class="spin spin-lg"></span></div>` });
    let v, thumbs;
    try { [v, thumbs] = await Promise.all([T.api.get('/api/videos/' + id), T.api.get('/api/videos/' + id + '/thumbnails')]); }
    catch (e) { m.body(ui.errorState(e)); return; }
    T.cacheVideos([v]);
    let file = null;
    const titleEl = m.$('.modal-head h2');
    if (titleEl) titleEl.textContent = v.title;
    const facts = [
      html`<span>${icon('calendar')}Uploaded ${T.fmtDay(v.upload_date, true)}</span>`,
      html`<span>${icon('clock')}${T.fmtClock(v.duration_seconds)}</span>`,
      v.size_bytes ? html`<span>${icon('storage')}${v.state === 'downloaded' ? '' : '~'}${T.fmtBytes(v.size_bytes)}</span>` : '',
      v.resolution ? html`<span>${icon('tv')}${T.fmtRes(v.resolution)}</span>` : '',
      v.sponsorblock_cut_seconds > 0 ? html`<span class="sb-chip">${icon('scissors')}SponsorBlock cut ${T.fmtClock(v.sponsorblock_cut_seconds)}</span>` : '',
      v.watched ? html`<span>${icon('eye')}Watched</span>` : '',
    ];
    m.body(html`<div class="vd">
        <div class="vd-media">${T.thumb(v, 'thumb-xl', html`<span class="dur">${T.fmtClock(v.duration_seconds)}</span>`)}</div>
        <div class="vd-info">
          <a class="vd-ch" href="#/channel/${encodeURIComponent(v.channel_id)}" data-close>${v.channel_title}</a>
          <div class="vd-state">${ui.videoState(v)}${v.episode ? html`<span class="code">${v.episode}</span>` : ''}</div>
          <div class="vd-facts">${facts}</div>
          ${v.state === 'failed' && v.error ? html`<div class="note note-crit">${icon('alert-circle')}<span>${v.error.message}</span></div>` : ''}
          <div class="vd-actions">
            ${v.state === 'downloaded' ? ui.switch('keep', v.keep, { text: 'Keep forever', attrs: 'data-vd="keep"' }) : ''}
            ${v.plex_url ? html`<a class="btn btn-sm" href="${v.plex_url}" target="_blank" rel="noopener noreferrer">${icon('film')}Open in Plex</a>` : ''}
            <a class="btn btn-sm" href="${v.youtube_url}" target="_blank" rel="noopener noreferrer">${icon('ext')}YouTube</a>
            ${v.state === 'failed' && (!v.error || v.error.retryable) ? html`<button class="btn btn-sm btn-crit-soft" type="button" data-vd="retry">${icon('refresh')}Retry</button>` : ''}
            ${ui.canDownloadNow && ui.canDownloadNow(v) ? html`<button class="btn btn-sm btn-primary" type="button" data-vd="now" title="Start this as the very next download: skips the pause and the daily limit (still one at a time)">${icon('download')}Download now</button>` : ''}
            ${['skipped_live', 'skipped_too_long', 'removed'].includes(v.state) ? html`<button class="btn btn-sm" type="button" data-vd="download">${icon('download')}${v.state === 'removed' ? 'Download again' : 'Download anyway'}</button>` : ''}
            ${['downloaded', 'queued', 'waiting_sponsorblock', 'failed'].includes(v.state) ? html`<button class="btn btn-sm btn-danger-ghost" type="button" data-vd="delete">${icon('trash')}${v.state === 'downloaded' ? 'Delete now' : "Don't download"}</button>` : ''}
          </div>
        </div>
      </div>
      <section class="vd-edit" id="vd-edit">
        <div class="sec-head"><h3>Edit info</h3><span class="sec-note">${icon('lock')}Saved to Plex and locked there, so Plex never overwrites it.</span></div>
        <div class="field">
          <div class="field-top"><label for="vd-title">Title</label>${v.locked_fields.includes('title') ? html`<button class="link-btn" type="button" data-vd="revert-title">${icon('undo')}Use YouTube's title</button>` : ''}</div>
          <input class="input" id="vd-title" name="title" value="${v.title}" maxlength="200">
          ${v.locked_fields.includes('title') ? html`<p class="hint">YouTube's title: ${v.youtube_title}</p>` : ''}
        </div>
        <div class="field">
          <div class="field-top"><label for="vd-summary">Summary</label>${v.locked_fields.includes('summary') ? html`<button class="link-btn" type="button" data-vd="revert-summary">${icon('undo')}Use YouTube's description</button>` : ''}</div>
          <textarea class="input" id="vd-summary" name="summary" rows="6">${v.summary}</textarea>
        </div>
        <div class="field"><div class="field-top"><span class="label">Thumbnail</span></div>${pickerGrid('vd-thumb', thumbs.variants, thumbs.current, 'thumb')}</div>
      </section>`);
    m.footer(html`<span class="foot-note" data-vd="dirty"></span><button class="btn" type="button" data-close>Close</button><button class="btn btn-primary" type="button" data-vd="save" disabled>${icon('check')}Save changes</button>`);
    const save = m.$('[data-vd="save"]'), dirtyNote = m.$('[data-vd="dirty"]');
    const initial = { title: v.title, summary: v.summary, thumb: thumbs.current };
    const cur = () => ({ title: m.$('#vd-title').value.trim(), summary: m.$('#vd-summary').value, thumb: (m.$('input[name="vd-thumb"]:checked') || {}).value || null });
    const check = () => { const c = cur(); const dirty = c.title !== initial.title || c.summary !== initial.summary || (c.thumb && c.thumb !== initial.thumb) || !!file; save.disabled = !dirty || !c.title; dirtyNote.textContent = dirty ? 'Unsaved changes' : ''; };
    m.el.addEventListener('input', check);
    m.el.addEventListener('change', check);
    bindUpload(m.el, (f) => { file = f; check(); });
    if (o.edit) setTimeout(() => { const e = m.$('#vd-edit'); if (e) e.scrollIntoView({ behavior: T.reducedMotion() ? 'auto' : 'smooth', block: 'start' }); m.$('#vd-title').focus({ preventScroll: true }); }, 60);
    const done = (nv, msg) => { T.cacheVideos([nv]); ui.patchRows(nv); if (msg) T.toast(msg); };
    m.el.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-vd]'); if (!b) return;
      const what = b.dataset.vd;
      try {
        if (what === 'save') {
          b.disabled = true;
          const c = cur(), body = {};
          if (c.title !== initial.title) body.title = c.title;
          if (c.summary !== initial.summary) body.summary = c.summary;
          let nv = null;
          if (Object.keys(body).length) nv = await T.api.patch('/api/videos/' + v.id, body);
          if (file) nv = await T.api.upload('/api/videos/' + v.id + '/thumbnail', file);
          else if (c.thumb && c.thumb !== initial.thumb) nv = await T.api.post('/api/videos/' + v.id + '/thumbnail', { variant: c.thumb });
          if (nv) done(nv, 'Saved. Plex gets the new info in a moment.');
          m.close();
        } else if (what === 'revert-title') { const nv = await T.api.patch('/api/videos/' + v.id, { title: null }); done(nv, "Back to YouTube's title"); m.close(); }
        else if (what === 'revert-summary') { const nv = await T.api.patch('/api/videos/' + v.id, { summary: null }); done(nv, "Back to YouTube's description"); m.close(); }
        else if (what === 'retry' || what === 'download') { const nv = await T.api.post('/api/videos/' + v.id + '/download', {}); done(nv, 'Queued: ' + v.title); m.close(); }
        else if (what === 'delete') { m.close(); T.videoActs.run('v-delete', v); }
        else if (what === 'now') { m.close(); T.videoActs.run('v-now', v); }
      } catch (err) { T.toastError(err); b.disabled = false; }
    });
    const keep = m.$('[data-vd="keep"]');
    if (keep) keep.addEventListener('change', async () => {
      try { const nv = await T.api.patch('/api/videos/' + v.id, { keep: keep.checked }); done(nv, keep.checked ? 'Kept forever: never cleaned up' : 'No longer kept forever'); }
      catch (err) { keep.checked = !keep.checked; T.toastError(err); }
    });
  };

  /* ---------- Channel editor: display name, genre, summary, poster ---------- */
  T.openChannelEdit = async function (ch, onSaved) {
    const m = T.modal({ title: 'Edit ' + ch.title, icon: 'wand', size: 'modal-lg', body: html`<div class="modal-loading"><span class="spin spin-lg"></span></div>` });
    let posters;
    try { posters = await T.api.get('/api/channels/' + ch.id + '/posters'); } catch (e) { m.body(ui.errorState(e)); return; }
    let file = null;
    const locked = ch.locked_fields || [];
    m.body(html`<div class="ce">
      <div class="ce-fields">
        <div class="field">
          <div class="field-top"><label for="ce-title">Display name</label>${locked.includes('title') ? html`<button class="link-btn" type="button" data-ce="revert-title">${icon('undo')}Use YouTube's</button>` : ''}</div>
          <input class="input" id="ce-title" value="${ch.title}" maxlength="120">
          <p class="hint">On YouTube: ${ch.youtube_title}. This is the show name in Plex; folders on disk keep YouTube's name.</p>
        </div>
        <div class="field">
          <div class="field-top"><label for="ce-genre">Genre</label>${locked.includes('genre') ? html`<button class="link-btn" type="button" data-ce="revert-genre">${icon('undo')}Reset</button>` : ''}</div>
          <input class="input" id="ce-genre" value="${ch.genre || ''}" list="ce-genres" maxlength="60">
          <datalist id="ce-genres">${GENRES.map((g) => html`<option value="${g}"></option>`)}</datalist>
        </div>
        <div class="field">
          <div class="field-top"><label for="ce-summary">Summary</label>${locked.includes('summary') ? html`<button class="link-btn" type="button" data-ce="revert-summary">${icon('undo')}Use the About text</button>` : ''}</div>
          <textarea class="input" id="ce-summary" rows="7">${ch.summary || ''}</textarea>
        </div>
        <p class="sec-note">${icon('lock')}Saved to Plex and locked there, so a re-scan never overwrites it.</p>
      </div>
      <div class="ce-posters"><div class="field-top"><span class="label">Poster</span></div>${pickerGrid('ce-poster', posters.variants, posters.current, 'poster')}</div>
    </div>`);
    m.footer(html`<span class="foot-note" data-ce="dirty"></span><button class="btn" type="button" data-close>Cancel</button><button class="btn btn-primary" type="button" data-ce="save" disabled>${icon('check')}Save</button>`);
    const initial = { title: ch.title, genre: ch.genre || '', summary: ch.summary || '', poster: posters.current };
    const cur = () => ({ title: m.$('#ce-title').value.trim(), genre: m.$('#ce-genre').value.trim(), summary: m.$('#ce-summary').value, poster: (m.$('input[name="ce-poster"]:checked') || {}).value || null });
    const save = m.$('[data-ce="save"]');
    const check = () => { const c = cur(); const dirty = c.title !== initial.title || c.genre !== initial.genre || c.summary !== initial.summary || (c.poster && c.poster !== initial.poster) || !!file; save.disabled = !dirty || !c.title; m.$('[data-ce="dirty"]').textContent = dirty ? 'Unsaved changes' : ''; };
    m.el.addEventListener('input', check); m.el.addEventListener('change', check);
    bindUpload(m.el, (f) => { file = f; check(); });
    m.el.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-ce]'); if (!b) return;
      const what = b.dataset.ce;
      try {
        let res = null;
        if (what === 'save') {
          b.disabled = true;
          const c = cur(), meta = {};
          if (c.title !== initial.title) meta.title = c.title;
          if (c.genre !== initial.genre) meta.genre = c.genre || null;
          if (c.summary !== initial.summary) meta.summary = c.summary;
          if (Object.keys(meta).length) res = await T.api.patch('/api/channels/' + ch.id, { meta });
          if (file) res = await T.api.upload('/api/channels/' + ch.id + '/poster', file);
          else if (c.poster && c.poster !== initial.poster) res = await T.api.post('/api/channels/' + ch.id + '/poster', { variant: c.poster });
        } else if (what.indexOf('revert-') === 0) {
          const f = what.slice(7);
          res = await T.api.patch('/api/channels/' + ch.id, { meta: { [f]: null } });
        } else return;
        if (res) { T.toast('Saved. Plex gets the change in a moment.'); T.app.invalidateChannels(); if (onSaved) onSaved(res); }
        m.close();
      } catch (err) { T.toastError(err); b.disabled = false; }
    });
  };

  /* ---------- Add channel ---------- */
  T.openAddChannel = function (prefill) {
    let preview = null, seq = 0;
    const m = T.modal({
      title: 'Add a channel', icon: 'plus', size: 'modal-md',
      body: html`<label class="label" for="add-q">YouTube channel link or @handle</label>
        <div class="input-xl">${icon('link')}<input id="add-q" placeholder="youtube.com/@channel  or  @channel" autocomplete="off" spellcheck="false" value="${prefill || ''}" autofocus></div>
        <p class="hint">Paste from the address bar of a channel page. A video link works too: Tubarr adds its channel.</p>
        <div class="add-result" data-add="result" aria-live="polite"></div>
        <details class="add-more"><summary>${icon('sliders')}<span>Download settings</span><span class="add-more-sum">Uses your defaults (you can change them any time on the channel's page)</span></summary>
          <div class="add-grid">
            <div class="field"><span class="label">Quality</span>${ui.seg('add-quality', [['', 'Default'], ['1080p', '1080p'], ['720p', '720p']], '', { label: 'Quality' })}</div>
            <div class="field"><label class="label" for="add-keep">How much to keep</label>${ui.select('add-keep', [['', 'Default: fill and roll'], ['count:10', 'Only the newest 10'], ['count:25', 'Only the newest 25'], ['days:90', 'Only the last 3 months'], ['days:365', 'Only the last year'], ['forever', 'Keep forever (whole back catalog)'], ['new_only', 'New uploads only']], '', { attrs: 'id="add-keep"' })}</div>
            <div class="field field-inline">${ui.switch('add-live', false, { text: 'Include livestream replays' })}</div>
          </div>
        </details>`,
      footer: html`<button class="btn" type="button" data-close>Cancel</button><button class="btn btn-primary" type="button" data-add="go" disabled>${icon('plus')}Add channel</button>`,
    });
    const input = m.$('#add-q'), res = m.$('[data-add="result"]'), go = m.$('[data-add="go"]');
    const lookup = async () => {
      const q = input.value.trim();
      const my = ++seq;
      preview = null; go.disabled = true;
      if (q.length < 2) { T.setHTML(res, ''); return; }
      T.setHTML(res, html`<div class="add-card is-loading"><span class="poster poster-sm skel"></span><span class="add-card-main"><span class="skel skel-line"></span><span class="skel skel-line short"></span></span></div>`);
      try {
        const r = await T.api.post('/api/lookup', { q });
        if (my !== seq) return;
        preview = r;
        const c = r.channel;
        T.setHTML(res, html`<div class="add-card">${T.poster(c, 'poster-sm')}
          <div class="add-card-main"><div class="add-card-title">${c.title}</div>
            <div class="add-card-sub">${c.handle || ''}${c.subscribers ? html` · ${T.fmtCompact(c.subscribers)} subscribers` : ''}${c.youtube_video_count ? html` · ${T.fmtNum(c.youtube_video_count)} videos` : ''}</div>
            ${c.about ? html`<p class="add-card-about">${c.about}</p>` : ''}
            ${r.already_added ? html`<div class="note note-info">${icon('info')}<span>Already in Tubarr. <a href="#/channel/${encodeURIComponent(c.id)}" data-close>Open it</a></span></div>` : ''}
          </div></div>`);
        go.disabled = !!r.already_added;
      } catch (e) {
        if (my !== seq) return;
        T.setHTML(res, html`<div class="note note-crit">${icon('alert-circle')}<span>${e.message}</span></div>`);
      }
    };
    const deb = T.debounce(lookup, 450);
    input.addEventListener('input', deb);
    input.addEventListener('paste', () => setTimeout(lookup, 0));
    input.addEventListener('keydown', (e) => { if (e.key === 'Enter' && !go.disabled) go.click(); });
    if (prefill) lookup();
    go.addEventListener('click', async () => {
      if (!preview) return;
      go.disabled = true;
      const settings = {};
      const q = (m.$('input[name="add-quality"]:checked') || {}).value;
      if (q) settings.quality = q;
      const k = m.$('#add-keep').value;
      if (k) settings.retention = k === 'forever' || k === 'new_only' ? { mode: k } : k.startsWith('count:') ? { mode: 'count', count: Number(k.slice(6)) } : { mode: 'days', days: Number(k.slice(5)) };
      if (m.$('input[name="add-live"]').checked) settings.include_live = true;
      try {
        const ch = await T.api.post('/api/channels', { q: preview.channel.handle || preview.channel.id, settings });
        m.close();
        T.app.invalidateChannels();
        T.toast('Added ' + ch.title + '. Tubarr is reading its uploads now.');
        T.app.navigate('#/channel/' + encodeURIComponent(ch.id));
      } catch (e) { T.toastError(e); go.disabled = false; }
    });
  };

  /* ---------- Retention overrides (on top of fill and roll) with a live space estimate ---------- */
  const COUNT_STEPS = [3, 5, 10, 15, 20, 25, 30, 40, 50, 75, 100, 150, 200, 300, 500, 1000];
  const DAY_STEPS = [7, 14, 30, 60, 90, 180, 365, 730, 1095, 1825];
  const dayWords = (d) => d % 365 === 0 ? (d / 365 === 1 ? '1 year' : d / 365 + ' years') : d % 30 === 0 && d < 365 ? (d / 30 === 1 ? '1 month' : d / 30 + ' months') : d % 7 === 0 && d < 30 ? (d / 7 === 1 ? '1 week' : d / 7 + ' weeks') : d + ' days';
  const nearest = (steps, x) => steps.reduce((best, s, i) => (Math.abs(s - x) < Math.abs(steps[best] - x) ? i : best), 0);
  const MODES = [
    ['fill', 'Default: fill and roll', 'Shares the space with every channel. New uploads first, then the backlog reaches back as far as the fill target allows.'],
    ['count', 'Only the last N videos', 'Keeps just the newest few; older ones leave Plex.'],
    ['days', 'Only the last N days', 'Keeps a rolling window of recent uploads.'],
    ['since', 'Only since a date', 'Everything uploaded on or after a date.'],
    ['forever', 'Keep forever', 'The whole back catalog, and nothing from this channel is ever rolled out.'],
    ['new_only', 'New uploads only', 'No backlog. New uploads still roll out like the default when space is needed.'],
  ];
  const retention = (T.retention = {});
  retention.describe = function (r) {
    if (!r || r.mode === 'fill') return 'Fill and roll';
    if (r.mode === 'count') return 'Last ' + T.fmtNum(r.count) + ' videos';
    if (r.mode === 'days') return 'Last ' + dayWords(r.days);
    if (r.mode === 'since') return 'Since ' + T.fmtDay(r.since, true);
    if (r.mode === 'forever') return 'Keep forever';
    if (r.mode === 'new_only') return 'New uploads only';
    return r.mode;
  };
  retention.render = function (r) {
    r = r || { mode: 'fill' };
    const count = r.mode === 'count' ? r.count : 25, days = r.mode === 'days' ? r.days : 90;
    const since = r.mode === 'since' ? r.since : (new Date().getFullYear() - 1) + '-01-01';
    return html`<div class="ret" data-ret data-mode="${r.mode}">
      <div class="ret-modes" role="radiogroup" aria-label="How much of this channel to keep">
        ${MODES.map(([k, label, desc]) => html`<label class="ret-opt"><input type="radio" name="ret-mode" value="${k}" ${k === r.mode ? T.raw('checked') : ''}><span class="ret-dot" aria-hidden="true"></span><span class="ret-text"><b>${label}</b><small>${desc}</small></span></label>`)}
      </div>
      <div class="ret-row" data-for="count">
        <input class="range" type="range" min="0" max="${COUNT_STEPS.length - 1}" step="1" value="${nearest(COUNT_STEPS, count)}" data-ret="count-range" aria-label="Number of videos">
        <span class="ret-val"><input class="input input-num" type="number" min="1" max="5000" value="${count}" data-ret="count" aria-label="Videos"> newest videos</span>
      </div>
      <div class="ret-row" data-for="days">
        <input class="range" type="range" min="0" max="${DAY_STEPS.length - 1}" step="1" value="${nearest(DAY_STEPS, days)}" data-ret="days-range" aria-label="Days">
        <span class="ret-val"><input class="input input-num" type="number" min="1" max="7300" value="${days}" data-ret="days" aria-label="Days"> days <span class="ret-words" data-ret="days-words">(${dayWords(days)})</span></span>
      </div>
      <div class="ret-row" data-for="since"><label class="ret-val">Uploaded on or after <input class="input input-date" type="date" value="${since}" max="${new Date().toISOString().slice(0, 10)}" data-ret="since"></label></div>
      <div class="est" data-ret="est" aria-live="polite"></div>
      <div class="ret-foot"><span class="ret-foot-note" data-ret="note"></span><button class="btn btn-sm" type="button" data-ret="reset" hidden>${icon('undo')}Undo changes</button><button class="btn btn-sm btn-primary" type="button" data-ret="apply" disabled>${icon('check')}Apply</button></div>
    </div>`;
  };
  /* o: { channelId, initial (retention or null), onApply(retentionOrNull) -> Promise } */
  retention.bind = function (root, o) {
    const el = root.querySelector('[data-ret]');
    if (!el) return null;
    let cur = o.initial ? JSON.parse(JSON.stringify(o.initial)) : null, seq = 0;
    const same = (a, b) => JSON.stringify(a || null) === JSON.stringify(b || null);
    const $ = (k) => el.querySelector(`[data-ret="${k}"]`);
    const read = () => {
      const mode = (el.querySelector('input[name="ret-mode"]:checked') || {}).value || 'fill';
      if (mode === 'fill') return null;
      if (mode === 'count') return { mode, count: Math.max(1, Number($('count').value) || 1) };
      if (mode === 'days') return { mode, days: Math.max(1, Number($('days').value) || 1) };
      if (mode === 'since') return { mode, since: $('since').value || '2025-01-01' };
      return { mode };
    };
    const est = $('est');
    async function estimate() {
      const r = read(), my = ++seq;
      el.dataset.mode = r ? r.mode : 'fill';
      est.classList.add('is-loading');
      try {
        const e = await T.api.post('/api/estimate', { channel_id: o.channelId, retention: r });
        if (my !== seq) return;
        el._last = e;
        T.setHTML(est, estimatePanel(e, r));
      } catch (err) { if (my === seq) T.setHTML(est, html`<div class="note note-crit">${icon('alert-circle')}<span>${err.message}</span></div>`); }
      est.classList.remove('is-loading');
      const changed = !same(r, cur);
      $('apply').disabled = !changed;
      $('reset').hidden = !changed;
    }
    const later = T.debounce(estimate, 150);
    el.addEventListener('input', (e) => {
      const k = e.target.dataset.ret;
      if (k === 'count-range') $('count').value = COUNT_STEPS[e.target.value];
      if (k === 'days-range') $('days').value = DAY_STEPS[e.target.value];
      if (k === 'count') $('count-range').value = nearest(COUNT_STEPS, Number(e.target.value));
      if (k === 'days') $('days-range').value = nearest(DAY_STEPS, Number(e.target.value));
      if (k === 'days-range' || k === 'days') $('days-words').textContent = '(' + dayWords(Number($('days').value)) + ')';
      if (k) later();
    });
    el.addEventListener('change', (e) => { if (e.target.name === 'ret-mode' || e.target.dataset.ret === 'since') estimate(); });
    el.addEventListener('click', async (e) => {
      const b = e.target.closest('[data-ret="apply"],[data-ret="reset"]'); if (!b) return;
      if (b.dataset.ret === 'reset') { setTo(cur); estimate(); return; }
      const r = read(), last = el._last;
      if (last && last.remove.count > 0) {
        const ok = await T.confirm({ title: 'Apply “' + retention.describe(r) + '”?', danger: true, confirm: 'Apply',
          body: html`<p>${T.plural(last.remove.count, 'video')} (${T.fmtBytes(last.remove.bytes)}) will leave Plex because they fall outside the new setting. Videos you marked Keep forever, and the channel's newest 3, stay.</p>${last.add.count ? html`<p>${T.plural(last.add.count, 'video')} (~${T.fmtBytes(last.add.bytes)}) will download.</p>` : ''}` });
        if (!ok) return;
      }
      b.disabled = true;
      try {
        await o.onApply(r); cur = r ? JSON.parse(JSON.stringify(r)) : null;
        const n = $('note'); n.textContent = 'Applied'; n.classList.add('flash'); setTimeout(() => n.classList.remove('flash'), 1800);
      } catch (err) { T.toastError(err); }
      estimate();
    });
    function setTo(r) {
      const mode = r ? r.mode : 'fill';
      el.querySelectorAll('input[name="ret-mode"]').forEach((i) => { i.checked = i.value === mode; });
      if (mode === 'count') { $('count').value = r.count; $('count-range').value = nearest(COUNT_STEPS, r.count); }
      if (mode === 'days') { $('days').value = r.days; $('days-range').value = nearest(DAY_STEPS, r.days); $('days-words').textContent = '(' + dayWords(r.days) + ')'; }
      if (mode === 'since') $('since').value = r.since;
      el.dataset.mode = mode;
    }
    estimate();
    return { refresh: estimate };
  };

  /* The shared "what would it cost" meter: used now, the change (added or removed), the fill target, the cap. */
  function estMeter(t) {
    const usedR = Math.min(1, t.used_bytes / t.cap_bytes), projR = Math.min(1, t.projected_bytes / t.cap_bytes);
    const grow = projR >= usedR, tgt = t.target_bytes / t.cap_bytes;
    return html`<div class="est-meter" role="img" aria-label="${'After applying: ' + T.fmtBytes(t.projected_bytes) + ' of ' + T.fmtBytes(t.cap_bytes)}">
      <i class="em-used" style="width:${((grow ? usedR : projR) * 100).toFixed(2)}%"></i>
      <i class="${grow ? 'em-add' : 'em-rem'}" style="left:${((grow ? usedR : projR) * 100).toFixed(2)}%;width:${(Math.abs(projR - usedR) * 100).toFixed(2)}%"></i>
      <em class="em-target" style="left:${(tgt * 100).toFixed(2)}%" title="Fill target"></em>
    </div>`;
  }
  function estimatePanel(e, r) {
    const t = e.total, mode = r ? r.mode : 'fill';
    const overTarget = t.projected_bytes > t.target_bytes && t.projected_bytes > t.used_bytes;
    return html`<div class="est-top">
        <div class="est-fig"><span class="est-label">This channel</span><b class="est-big">${e.estimated ? '≈ ' : ''}${T.fmtBytes(e.projected_bytes)}</b><span class="est-was">now ${T.fmtBytes(e.current_bytes)}</span></div>
        <div class="est-deltas">
          <span class="est-d plus">${icon('download')}<b>+${T.fmtNum(e.add.count)}</b> to download${e.add.count ? html` · ${T.fmtBytes(e.add.bytes)}` : ''}</span>
          <span class="est-d minus">${icon('trash')}<b>−${T.fmtNum(e.remove.count)}</b> leave Plex${e.remove.count ? html` · ${T.fmtBytes(e.remove.bytes)}` : ''}</span>
        </div>
      </div>
      ${estMeter(t)}
      <div class="est-total"><span>Library <b>${T.fmtBytes(t.projected_bytes)}</b> of ${T.fmtBytes(t.cap_bytes, { digits: 0 })} <span class="dim">· target ${T.fmtBytes(t.target_bytes)}</span></span>
        <span class="est-kept">${mode === 'fill' && e.reach_date ? html`Reaches back to about ${T.fmtDay(e.reach_date, true)}` : html`${T.plural(e.kept_count, 'video')}${e.oldest_date ? html` back to ${T.fmtDay(e.oldest_date, true)}` : ''}`}</span></div>
      ${overTarget ? html`<div class="note note-warn">${icon('info')}<span>That goes past the fill target, so the roll makes room: watched videos first, then the oldest backlog of the biggest channels.</span></div>` : ''}
      ${mode === 'fill' ? html`<p class="est-foot">In fill and roll, a channel's share follows the whole library; this is where it stands at the current fill.</p>` : e.estimated ? html`<p class="est-foot">Sizes of videos not downloaded yet are estimates (length × typical bitrate).</p>` : ''}`;
  }

  /* ---------- Fill target (Settings → Downloads): how full before rolling, and how far back that reaches ---------- */
  const TB = 1099511627776;
  /* The slider runs from a tenth of the space (never under 10 GB, never above the current target) up to just
     under the whole space, whatever size the library's disk or quota is. */
  retention.renderTarget = function (bytes, capBytes) {
    const cap = capBytes / TB, v = bytes / TB, max = Math.max(v, cap - 0.01);
    const min = Math.min(v, Math.max(0.01, Math.round(cap * 10) / 100));
    const step = Math.max(0.01, Math.round(cap / 3) / 100);
    const lbl = (x) => x.toFixed(x < 10 ? 2 : 1) + ' TB';
    return html`<div class="tgt" data-tgt>
      <div class="tgt-head"><span>Fill to</span><b class="tgt-val" data-tgt="val">${v.toFixed(2)} TB</b><span class="dim">of the ${cap.toFixed(cap < 10 ? 2 : 1)} TB available</span></div>
      <input class="range" type="range" min="${min.toFixed(2)}" max="${max.toFixed(2)}" step="${step.toFixed(2)}" value="${v.toFixed(2)}" data-tgt="range" aria-label="Fill target in TB">
      <div class="tgt-scale" aria-hidden="true"><span>${lbl(min)}</span><span>${lbl((min + cap) / 2)}</span><span>${lbl(cap)}</span></div>
      <div class="est" data-tgt="est" aria-live="polite"></div>
    </div>`;
  };
  retention.bindTarget = function (root, o) {
    const el = root.querySelector('[data-tgt]');
    if (!el) return null;
    const range = el.querySelector('[data-tgt="range"]'), val = el.querySelector('[data-tgt="val"]'), est = el.querySelector('[data-tgt="est"]');
    let seq = 0;
    const bytes = () => Math.round(Number(range.value) * TB);
    async function run() {
      const my = ++seq;
      est.classList.add('is-loading');
      try {
        const e = await T.api.post('/api/estimate', { channel_id: null, fill_target_bytes: bytes() });
        if (my !== seq) return;
        const t = e.total;
        T.setHTML(est, html`<div class="est-top">
            <div class="est-fig"><span class="est-label">The backlog would reach back to about</span><b class="est-big">${e.reach_date ? T.fmtDay(e.reach_date, true) : '—'}</b><span class="est-was">across ${T.plural(e.channels_affected, 'channel')} on fill and roll</span></div>
            <div class="est-deltas">
              <span class="est-d plus">${icon('download')}<b>+${T.fmtNum(e.add.count)}</b> still to fill${e.add.count ? html` · ${T.fmtBytes(e.add.bytes)}` : ''}</span>
              <span class="est-d minus">${icon('trash')}<b>−${T.fmtNum(e.remove.count)}</b> would roll out${e.remove.count ? html` · ${T.fmtBytes(e.remove.bytes)}` : ''}</span>
            </div>
          </div>
          ${estMeter(t)}
          <div class="est-total"><span>Library <b>${T.fmtBytes(t.projected_bytes)}</b> of ${T.fmtBytes(t.cap_bytes, { digits: 0 })} when full</span><span class="est-kept">Now ${T.fmtBytes(t.used_bytes)}</span></div>`);
      } catch (err) { if (my === seq) T.setHTML(est, html`<div class="note note-crit">${icon('alert-circle')}<span>${err.message}</span></div>`); }
      est.classList.remove('is-loading');
    }
    const later = T.debounce(run, 150);
    range.addEventListener('input', () => { val.textContent = Number(range.value).toFixed(2) + ' TB'; if (o && o.onChange) o.onChange(bytes()); later(); });
    run();
    return { value: bytes, set(b) { range.value = (b / TB).toFixed(2); val.textContent = Number(range.value).toFixed(2) + ' TB'; run(); } };
  };
})();
