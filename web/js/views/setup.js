/* First-run setup wizard (#/setup): 1 Connect Plex (recommended) -> 2 Pick the library -> 3 Add your subscriptions
   -> 4 Done. Shown automatically after the first sign-in while GET /api/setup says done: false; "Run setup again"
   in Settings -> Plex opens it any time. Every step can be skipped; the last step calls POST /api/setup/complete. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon, ui = T.ui;
  const V = (T.views.setup = { nav: 'settings', title: 'Set up Tubarr' });
  const STEPS = [[1, 'Connect Plex'], [2, 'Pick the library'], [3, 'Add your subscriptions'], [4, 'Done']];
  const NFO_LINE = 'Tubarr still saves every video with standard NFO info files and artwork, which Jellyfin and Emby read as-is.';

  let root = null, S = null, pollT = null, plexWin = null;
  const fresh = () => ({
    step: 1, su: null, busy: false,
    pin: null, pinState: 'idle', servers: [], uri: '', manualErr: '',
    libs: null, lib: '', root: '', rootEdited: false, libErr: '',
    tab: 'csv', text: '', fileName: '', preview: null, applied: null, skippedPlex: false, completed: false,
  });

  /* ---------- helpers ---------- */
  const plexOk = () => !!(S.su && S.su.plex && S.su.plex.configured && S.su.plex.state === 'connected');
  function stopPoll() { clearTimeout(pollT); pollT = null; }
  function render() {
    if (!root) return;
    const box = root.querySelector('[data-slot="step"]');
    const bar = root.querySelector('[data-slot="stepper"]');
    if (bar) T.setHTML(bar, stepper());
    if (box) T.setHTML(box, [null, stepPlex, stepLibrary, stepSubs, stepDone][S.step]());
    bindStep();
  }
  function go(step) { if (step === 2 && !plexOk()) step = 1; S.step = step; if (step !== 1) stopPoll(); render(); window.scrollTo({ top: 0, behavior: T.reducedMotion() ? 'auto' : 'smooth' }); if (step === 2 && !S.libs) loadLibraries(); if (step === 4) complete(); }
  function stepper() {
    return STEPS.map(([n, l]) => {
      const cls = n === S.step ? 'is-on' : n < S.step ? 'is-done' : '';
      const inner = html`<span class="step-n">${n < S.step ? icon('check') : n}</span><span class="setup-step-l">${l}</span>`;
      return html`<li class="${cls}" ${n === S.step ? T.raw('aria-current="step"') : ''}>${n < S.step && S.step < 4 ? html`<button type="button" class="setup-step-btn" data-act="goto" data-step="${n}">${inner}</button>` : html`<span class="setup-step-btn">${inner}</span>`}</li>`;
    });
  }
  const skipPlex = () => html`<div class="setup-skip"><button class="link-btn" type="button" data-act="skip-plex">Not using Plex? Skip this step</button><p class="hint">${NFO_LINE}</p></div>`;

  /* ---------- Step 1: Connect Plex ---------- */
  function rankConn(c) { return (c.local && !c.relay ? 0 : !c.relay ? 1 : 2); }
  function bestUri(servers) {
    const all = [];
    servers.forEach((s, si) => (s.connections || []).forEach((c) => all.push({ c, rank: rankConn(c) + (s.owned ? 0 : 3), si })));
    all.sort((a, b) => a.rank - b.rank || a.si - b.si);
    return all.length ? all[0].c.uri : '';
  }
  function stepPlex() {
    const p = S.su && S.su.plex;
    if (plexOk() && S.pinState === 'idle') {
      return html`<div class="setup-pane">
        <div class="yt-head"><span class="yt-ic ok">${icon('check')}</span><div><h3>Connected to <b>${p.server_name || 'Plex'}</b></h3><p class="dim">${p.url || ''}</p></div></div>
        <div class="btn-row"><button class="btn btn-primary btn-lg" type="button" data-act="goto" data-step="2">Continue${icon('chev-right')}</button><button class="btn" type="button" data-act="plex-signin">${icon('login')}Connect a different server</button></div>
      </div>`;
    }
    if (S.pinState === 'authorized') {
      const servers = S.servers || [];
      return html`<div class="setup-pane">
        <div class="yt-head"><span class="yt-ic ok">${icon('check')}</span><div><h3>Signed in to Plex</h3><p>Pick the server your YouTube library should live on. The first address is usually the best one: it stays inside your home network.</p></div></div>
        ${servers.length ? html`<div class="setup-servers">${servers.map((s) => html`<fieldset class="setup-server"><legend><b>${s.name}</b>${s.owned ? html`<span class="tag tag-sm tag-brand">Yours</span>` : html`<span class="tag tag-sm">Shared with you</span>`}</legend>
            ${(s.connections || []).slice().sort((a, b) => rankConn(a) - rankConn(b)).map((c) => html`<label class="setup-choice"><input type="radio" name="plex-uri" value="${c.uri}" ${c.uri === S.uri ? T.raw('checked') : ''}><span class="setup-radio" aria-hidden="true"></span><span class="setup-choice-main"><b>${c.uri}</b><small>${c.local ? 'On your home network' : 'Over the internet'}${c.relay ? ' · through Plex’s relay (slow)' : ''}</small></span></label>`)}
          </fieldset>`)}</div>
          <div class="btn-row"><button class="btn btn-primary btn-lg" type="button" data-act="plex-use" ${S.uri && !S.busy ? '' : T.raw('disabled')}>${S.busy ? html`<span class="spin"></span>` : icon('check')}Use this server</button><button class="btn btn-ghost" type="button" data-act="plex-signin">${icon('refresh')}Sign in again</button></div>`
          : html`<div class="note note-warn">${icon('alert')}<span>This Plex account doesn’t have any servers Tubarr can see. Sign in with the account that owns the server, or enter its address below.</span></div>`}
        ${S.manualErr ? html`<div class="note note-crit">${icon('alert-circle')}<span>${S.manualErr}</span></div>` : ''}
        ${manualBox(!servers.length)}
        ${skipPlex()}
      </div>`;
    }
    const waiting = S.pinState === 'waiting' && S.pin;
    return html`<div class="setup-pane">
      <div class="yt-hero setup-hero">
        <span class="yt-hero-mark">${T.logoMark()}</span>
        <div>
          <span class="tag tag-sm tag-brand">Recommended first step</span>
          <h3>Connect Plex</h3>
          <p>Sign in with your Plex account and pick your server. Tubarr then puts every new video straight into your library, with posters, episode details and chapters.</p>
          ${waiting ? html`<div class="pin-wait">
              <p class="signin-wait"><span class="spin"></span>Waiting for you to allow Tubarr in the Plex window…</p>
              <p class="pin-alt">Or open <a href="${S.pin.link_url || 'https://plex.tv/link'}" target="_blank" rel="noopener noreferrer">plex.tv/link ${icon('ext')}</a> on any device and enter code <b class="pin-code">${S.pin.code}</b></p>
              <div class="btn-row"><button class="btn" type="button" data-act="plex-reopen">${icon('ext')}Open the Plex window again</button><button class="btn btn-ghost" type="button" data-act="plex-cancel">Cancel</button></div>
            </div>`
            : html`${S.pinState === 'expired' ? html`<div class="note note-warn">${icon('timer')}<span>That sign-in code expired. Start again; it only takes a moment.</span></div>` : ''}
              <div class="btn-row"><button class="btn btn-primary btn-lg" type="button" data-act="plex-signin" ${S.busy ? T.raw('disabled') : ''}>${S.busy ? html`<span class="spin"></span>` : icon('login')}Sign in with Plex</button></div>`}
        </div>
      </div>
      ${S.manualErr ? html`<div class="note note-crit">${icon('alert-circle')}<span>${S.manualErr}</span></div>` : ''}
      ${manualBox(false)}
      ${skipPlex()}
    </div>`;
  }
  function manualBox(open) {
    return html`<details class="setup-manual" ${open ? T.raw('open') : ''}>
      <summary>${icon('chev-right')}Enter the address and a token by hand</summary>
      <div class="setup-manual-body">
        <div class="field"><label class="label" for="su-url">Plex address</label><input class="input" id="su-url" type="url" inputmode="url" autocomplete="off" spellcheck="false" placeholder="http://plex.local:32400"></div>
        <div class="field"><label class="label" for="su-token">Plex token</label><input class="input" id="su-token" type="password" autocomplete="off" spellcheck="false" placeholder="Leave empty to keep the saved one"><p class="hint">${icon('info')}<span>In Plex Web, open any movie or episode → ⋯ → Get Info → View XML; the token is the <code>X-Plex-Token</code> at the end of the address. Tubarr stores it encrypted and never shows it again.</span></p></div>
        <div class="btn-row"><button class="btn" type="button" data-act="plex-manual">${icon('link')}Connect</button></div>
      </div>
    </details>`;
  }

  /* ---------- Step 2: Pick the library ---------- */
  function stepLibrary() {
    const media = (S.su && S.su.media_path) || '/youtube';
    if (!S.libs) return html`<div class="setup-pane setup-loading"><span class="spin spin-lg"></span><p class="dim">Asking Plex for its TV libraries…</p></div>`;
    if (!S.libs.length) {
      return html`<div class="setup-pane">
        <div class="yt-head"><span class="yt-ic">${icon('tv')}</span><div><h3>No TV library for Tubarr yet</h3><p>Tubarr files each channel as a TV show, so it needs a <b>TV Shows</b> library in Plex. Create one, then check again:</p></div></div>
        <ol class="steps setup-howto">
          <li><span class="step-n">1</span><span>In Plex, open Settings → Libraries → <b>Add Library</b> and choose <b>TV Shows</b>. Name it anything, for example “YouTube”.</span></li>
          <li><span class="step-n">2</span><span>Add the folder Tubarr saves to: the one mounted at <code>${media}</code> in Tubarr’s container, as Plex sees it.</span></li>
          <li><span class="step-n">3</span><span>Under Advanced, pick the agent <b>Plex Series</b> or the local NFO option, and turn on <b>Use local assets</b>.</span></li>
        </ol>
        ${S.libErr ? html`<div class="note note-crit">${icon('alert-circle')}<span>${S.libErr}</span></div>` : ''}
        <div class="btn-row"><button class="btn btn-primary" type="button" data-act="lib-refresh">${icon('refresh')}Check again</button><button class="btn btn-ghost" type="button" data-act="goto" data-step="3">Do this later</button></div>
      </div>`;
    }
    const cur = S.libs.find((l) => l.title === S.lib);
    return html`<div class="setup-pane">
      <div class="yt-head"><span class="yt-ic">${icon('tv')}</span><div><h3>Which library should hold your YouTube channels?</h3><p>Each channel becomes a show, each video an episode. A library of its own keeps them apart from your other TV.</p></div></div>
      <div class="setup-libs" role="radiogroup" aria-label="Plex TV libraries">${S.libs.map((l) => html`<label class="setup-choice"><input type="radio" name="plex-lib" value="${l.title}" ${l.title === S.lib ? T.raw('checked') : ''}><span class="setup-radio" aria-hidden="true"></span><span class="setup-choice-main"><b>${l.title}</b><small>${l.agent_label || l.agent || ''}${l.paths && l.paths.length ? html` · <code>${l.paths.join(', ')}</code>` : ''}</small></span></label>`)}</div>
      <div class="field setup-root"><label class="label" for="su-root">Folder as Plex sees it</label><input class="input" id="su-root" type="text" autocomplete="off" spellcheck="false" value="${S.root}" placeholder="${(cur && cur.paths && cur.paths[0]) || '/data/youtube'}">
        <p class="hint">${icon('info')}<span>Tubarr saves to <code>${media}</code> inside its container; this is that folder’s path as Plex sees it. It’s usually the library’s folder shown above.</span></p></div>
      ${S.libErr ? html`<div class="note note-crit">${icon('alert-circle')}<span>${S.libErr}</span></div>` : ''}
      <div class="btn-row"><button class="btn btn-primary btn-lg" type="button" data-act="lib-use" ${S.lib && !S.busy ? '' : T.raw('disabled')}>${S.busy ? html`<span class="spin"></span>` : icon('check')}Use this library</button><button class="btn btn-ghost" type="button" data-act="lib-refresh">${icon('refresh')}Check again</button><button class="btn btn-ghost" type="button" data-act="goto" data-step="1">${icon('chev-left')}Back</button></div>
    </div>`;
  }

  /* ---------- Step 3: Add your subscriptions ---------- */
  function stepSubs() {
    const p = S.preview;
    if (p) {
      const n = p.new || [];
      return html`<div class="setup-pane">
        <div class="yt-head"><span class="yt-ic">${icon('subs')}</span><div><h3>${n.length ? T.plural(n.length, 'new channel') + ' found' : 'Nothing new to add'}</h3>
          <p>${T.fmtNum(p.found)} found${p.unchanged ? ' · ' + T.fmtNum(p.unchanged) + ' already in Tubarr' : ''}${p.unrecognized_lines ? ' · ' + T.plural(p.unrecognized_lines, 'line') + ' skipped (not a channel)' : ''}.</p></div></div>
        ${n.length ? html`<div class="setup-pick-all"><button class="link-btn" type="button" data-act="subs-all">Select all</button><span class="sep">·</span><button class="link-btn" type="button" data-act="subs-none">Select none</button><span class="dim" data-slot="subs-count"></span></div>
          <div class="setup-chans">${n.map((c) => html`<label class="diff-row"><input type="checkbox" data-add="${c.handle || c.channel_id}" checked><span class="diff-check">${icon('check')}</span>${T.poster({ title: c.title || c.handle || '?' }, 'poster-xs')}<span class="diff-main"><b>${c.title || c.handle || c.channel_id}</b><small>${c.handle || c.channel_id}</small></span></label>`)}</div>
          <div class="note note-info">${icon('timer')}<span>Channels are looked up in the background at a gentle pace, a few seconds apart, so they appear one by one. Their videos follow.</span></div>` : ''}
        <div class="btn-row">${n.length ? html`<button class="btn btn-primary btn-lg" type="button" data-act="subs-apply" ${S.busy ? T.raw('disabled') : ''}>${S.busy ? html`<span class="spin"></span>` : icon('plus')}<span data-slot="subs-btn">Add ${T.plural(n.length, 'channel')}</span></button>` : html`<button class="btn btn-primary btn-lg" type="button" data-act="goto" data-step="4">Continue${icon('chev-right')}</button>`}
          <button class="btn btn-ghost" type="button" data-act="subs-back">${icon('chev-left')}Use a different list</button></div>
      </div>`;
    }
    return html`<div class="setup-pane">
      <div class="yt-head"><span class="yt-ic">${icon('subs')}</span><div><h3>Add your subscriptions</h3><p>Bring in the channels you follow in one go. You’ll see the list before anything is added.</p></div></div>
      ${ui.seg('su-tab', [['csv', 'Upload Google Takeout subscriptions.csv', 'file'], ['paste', 'Paste channel links or @handles', 'clipboard']], S.tab, { label: 'How to add channels', cls: 'seg-wrap setup-tabs', attrs: 'data-su="tab"' })}
      ${S.tab === 'csv' ? html`<div class="setup-tab">
          <p class="hint">${icon('info')}<span>Get it at <a href="https://takeout.google.com/" target="_blank" rel="noopener noreferrer">takeout.google.com</a> → YouTube → subscriptions. The download holds a file called <code>subscriptions.csv</code>.</span></p>
          <div class="drop setup-drop" data-slot="drop">
            <label class="setup-file">${icon('file')}<span>${S.fileName ? html`<b>${S.fileName}</b> <span class="dim">· choose another</span>` : html`<b>Choose subscriptions.csv</b> <span class="dim">or drop it here</span>`}</span><input type="file" accept=".csv,text/csv,text/plain" data-slot="su-file"></label>
          </div>
        </div>`
        : html`<div class="setup-tab"><textarea class="input paste" data-slot="su-text" rows="8" placeholder="${'youtube.com/@channel\n@anotherchannel\nhttps://www.youtube.com/channel/UC…'}" aria-label="Channel links or @handles, one per line">${S.text}</textarea><p class="hint">One per line: channel links, @handles or channel IDs.</p></div>`}
      <div class="btn-row"><button class="btn btn-primary btn-lg" type="button" data-act="subs-preview" ${S.text.trim() && !S.busy ? '' : T.raw('disabled')}>${S.busy ? html`<span class="spin"></span>` : icon('search')}Find channels</button>
        ${T.api.mode === 'mock' && S.tab === 'paste' ? html`<button class="btn btn-ghost" type="button" data-act="subs-sample">${icon('clipboard')}Paste a sample</button>` : ''}
        <button class="btn btn-ghost" type="button" data-act="goto" data-step="4">Skip for now</button></div>
    </div>`;
  }

  /* ---------- Step 4: Done ---------- */
  function stepDone() {
    const p = S.su && S.su.plex;
    const added = S.applied ? S.applied.added : 0;
    return html`<div class="setup-pane setup-done">
      <span class="empty-mark">${T.logoMark()}</span>
      <h3>You’re all set</h3>
      <p>${added ? html`Tubarr is looking up ${T.plural(added, 'channel')} in the background and will start downloading right after.` : 'Tubarr keeps checking your channels and downloads new videos as they come out.'}</p>
      <dl class="facts facts-3">
        <div><dt>Plex</dt><dd>${plexOk() ? (p.server_name || 'Connected') : 'Not connected (NFO files only)'}</dd></div>
        <div><dt>Library</dt><dd>${plexOk() && p.library ? p.library : '—'}</dd></div>
        <div><dt>Channels</dt><dd>${added ? '+' + T.fmtNum(added) + ' being added' : T.fmtNum((S.su && S.su.channels) || 0)}</dd></div>
      </dl>
      ${S.completed === 'error' ? html`<div class="note note-warn">${icon('alert')}<span>Tubarr couldn’t save that setup is finished. It may ask again next time.</span></div>` : ''}
      <div class="btn-row"><a class="btn btn-primary btn-lg" href="#/timeline">${icon('history')}Go to the Timeline</a><a class="btn" href="#/activity">${icon('activity')}Watch it work</a><a class="btn btn-ghost" href="#/settings">${icon('sliders')}Settings</a></div>
    </div>`;
  }

  /* ---------- actions ---------- */
  function bindStep() {
    if (!root) return;
    root.querySelectorAll('input[name="plex-uri"]').forEach((r) => r.addEventListener('change', () => { S.uri = r.value; const b = root.querySelector('[data-act="plex-use"]'); if (b) b.disabled = false; }));
    root.querySelectorAll('input[name="plex-lib"]').forEach((r) => r.addEventListener('change', () => {
      S.lib = r.value;
      const l = S.libs.find((x) => x.title === r.value);
      const inp = root.querySelector('#su-root');
      if (!S.rootEdited && l && l.paths && l.paths[0]) { S.root = l.paths[0]; if (inp) inp.value = S.root; }
      const b = root.querySelector('[data-act="lib-use"]'); if (b) b.disabled = false;
    }));
    const ri = root.querySelector('#su-root');
    if (ri) ri.addEventListener('input', () => { S.root = ri.value; S.rootEdited = true; });
    root.querySelectorAll('[data-su="tab"]').forEach((r) => r.addEventListener('change', () => { S.tab = r.value; render(); }));
    const ta = root.querySelector('[data-slot="su-text"]');
    if (ta) ta.addEventListener('input', () => { S.text = ta.value; S.fileName = ''; const b = root.querySelector('[data-act="subs-preview"]'); if (b) b.disabled = !S.text.trim(); });
    const fi = root.querySelector('[data-slot="su-file"]');
    if (fi) fi.addEventListener('change', () => readFile(fi.files && fi.files[0]));
    const drop = root.querySelector('[data-slot="drop"]');
    if (drop) {
      ['dragover', 'dragenter'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('is-over'); }));
      ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, () => drop.classList.remove('is-over')));
      drop.addEventListener('drop', (e) => { e.preventDefault(); readFile(e.dataTransfer.files && e.dataTransfer.files[0]); });
    }
    const chans = root.querySelector('.setup-chans');
    if (chans) { chans.addEventListener('change', countPicked); countPicked(); }
  }
  function countPicked() {
    const n = root.querySelectorAll('[data-add]:checked').length, all = root.querySelectorAll('[data-add]').length;
    const c = root.querySelector('[data-slot="subs-count"]'); if (c) c.textContent = n + ' of ' + all + ' selected';
    const b = root.querySelector('[data-act="subs-apply"]'), t = root.querySelector('[data-slot="subs-btn"]');
    if (b) b.disabled = !n || S.busy;
    if (t) t.textContent = 'Add ' + T.plural(n, 'channel');
  }
  function readFile(f) {
    if (!f) return;
    if (f.size > 5 * 1048576) { T.toast('That file is too big to be a subscriptions list.', { type: 'error' }); return; }
    const fr = new FileReader();
    fr.onload = () => { S.text = String(fr.result || ''); S.fileName = f.name; render(); };
    fr.onerror = () => T.toast('Couldn’t read that file.', { type: 'error' });
    fr.readAsText(f);
  }
  function pollPin() {
    stopPoll();
    if (!S.pin) return;
    pollT = setTimeout(async () => {
      if (!S || !S.pin || S.pinState !== 'waiting') return;
      try {
        const r = await T.api.get('/api/setup/plex/pin/' + encodeURIComponent(S.pin.pin_id));
        if (r.state === 'authorized') {
          S.pinState = 'authorized'; S.servers = r.servers || []; S.uri = bestUri(S.servers);
          try { if (plexWin && !plexWin.closed) plexWin.close(); } catch (e) { /* another origin: leave it */ }
          plexWin = null; render(); return;
        }
        if (r.state === 'expired') { S.pinState = 'expired'; S.pin = null; render(); return; }
      } catch (e) { if (e.status === 401) return; /* a blip: keep polling */ }
      pollPin();
    }, 2000);
  }
  function openPlexWindow(url) {
    // opened first (inside the click) and pointed at Plex afterwards, so popup blockers let it through
    try {
      if (plexWin && !plexWin.closed) { plexWin.location.href = url; plexWin.focus(); return true; }
    } catch (e) { /* fall through and open a new one */ }
    plexWin = window.open(url, 'tubarr-plex', 'width=720,height=780');
    return !!plexWin;
  }
  async function plexSignin() {
    stopPoll();
    // must happen synchronously in the click handler: a blank window now, Plex's address once the PIN exists
    let w = null;
    try { w = window.open('', 'tubarr-plex', 'width=720,height=780'); } catch (e) { w = null; }
    if (w) { try { w.document.title = 'Plex'; w.document.body.innerHTML = '<p style="font:15px system-ui;padding:24px;color:#555">Opening Plex…</p>'; } catch (e) { /* not ours to write */ } }
    plexWin = w;
    S.busy = true; S.manualErr = ''; render();
    try {
      const pin = await T.api.post('/api/setup/plex/pin');
      S.pin = pin; S.pinState = 'waiting';
      if (w && !w.closed) { try { w.opener = null; } catch (e) { /* ignore */ } w.location.href = pin.auth_url; }
      else T.toast('Your browser blocked the Plex window. Use “Open the Plex window again”, or plex.tv/link with the code.', { type: 'info', timeout: 8000 });
      pollPin();
    } catch (e) {
      try { if (w) w.close(); } catch (x) { /* ignore */ }
      plexWin = null;
      T.toastError(e);
    } finally { S.busy = false; render(); }
  }
  async function usePlex(body) {
    S.busy = true; S.manualErr = ''; render();
    try {
      const r = await T.api.post('/api/setup/plex', body);
      S.su = Object.assign({}, S.su || {}, { plex: Object.assign({}, r, { libraries: undefined }) });
      if (Array.isArray(r.libraries)) setLibs(r.libraries);
      S.busy = false;
      if (r.state && r.state !== 'connected') { S.manualErr = r.message || (r.state === 'unauthorized' ? 'Plex refused the token.' : 'Tubarr couldn’t reach Plex at that address.'); render(); return; }
      S.pinState = 'idle'; S.pin = null;
      T.toast('Connected to ' + (r.server_name || 'Plex') + '.');
      go(2);
    } catch (e) { S.busy = false; S.manualErr = e.message; render(); }
  }
  function setLibs(libs) {
    S.libs = libs || [];
    const want = (S.su && S.su.plex && S.su.plex.library) || S.lib;
    const pick = S.libs.find((l) => l.title === want) || S.libs.find((l) => /youtube/i.test(l.title)) || S.libs[0];
    S.lib = pick ? pick.title : '';
    if (!S.rootEdited) S.root = (S.su && S.su.plex && S.su.plex.library === S.lib && S.su.plex.plex_root) || (pick && pick.paths && pick.paths[0]) || '';
  }
  async function loadLibraries() {
    S.libErr = '';
    try { const r = await T.api.get('/api/setup/plex/libraries'); setLibs(r.libraries); }
    catch (e) { S.libs = S.libs || []; S.libErr = e.message; }
    render();
  }
  async function complete() {
    if (S.completed === true) return;
    try { await T.api.post('/api/setup/complete'); S.completed = true; if (T.app) T.app.setupDone = true; }
    catch (e) { S.completed = 'error'; render(); }
  }

  V.enter = async function (el) {
    root = el; stopPoll(); S = fresh();
    T.setHTML(root, html`<div class="page page-setup">
      <header class="page-head"><div class="page-title"><h1>Set up Tubarr</h1><p class="page-sub">A few minutes, once: Plex first, then your channels. Every step can be skipped and changed later in Settings.</p></div>
        <div class="page-actions"><button class="btn btn-ghost" type="button" data-act="skip-all">Skip setup</button></div></header>
      <ol class="setup-steps" data-slot="stepper" aria-label="Setup steps"></ol>
      <div class="card set-card setup-card" data-slot="step"><div class="set-loading"><span class="spin spin-lg"></span></div></div>
    </div>`);
    try { S.su = await T.api.get('/api/setup'); } catch (e) { T.setHTML(root.querySelector('[data-slot="step"]'), ui.errorState(e)); return; }
    render();
  };
  V.leave = function () { stopPoll(); root = null; };
  V.act = function (name, el) {
    switch (name) {
      case 'goto': go(Number(el.dataset.step)); return;
      case 'plex-signin': plexSignin(); return;
      case 'plex-reopen': if (S.pin && !openPlexWindow(S.pin.auth_url)) T.toast('Your browser blocked the window. Use plex.tv/link with the code instead.', { type: 'info' }); return;
      case 'plex-cancel': stopPoll(); S.pin = null; S.pinState = 'idle'; try { if (plexWin && !plexWin.closed) plexWin.close(); } catch (e) { /* ignore */ } plexWin = null; render(); return;
      case 'plex-use': if (S.uri) usePlex({ url: S.uri }); return;
      case 'plex-manual': {
        const url = root.querySelector('#su-url').value.trim(), token = root.querySelector('#su-token').value.trim();
        if (!/^https?:\/\/\S+$/i.test(url)) { S.manualErr = 'Enter the full address, like http://plex.local:32400.'; render(); const d = root.querySelector('.setup-manual'); if (d) d.open = true; return; }
        const body = { url }; if (token) body.token = token;
        usePlex(body);
        return;
      }
      case 'skip-plex': S.skippedPlex = true; stopPoll(); go(3); return;
      case 'lib-refresh': S.libs = null; render(); loadLibraries(); return;
      case 'lib-use': {
        if (!S.lib) return;
        const pr = (root.querySelector('#su-root') || {}).value;
        S.root = (pr || S.root || '').trim();
        if (!S.root) { S.libErr = 'Enter the folder as Plex sees it.'; render(); return; }
        S.busy = true; S.libErr = ''; render();
        T.api.post('/api/setup/plex/library', { title: S.lib, plex_root: S.root }).then((p) => {
          S.busy = false; S.su = Object.assign({}, S.su || {}, { plex: p }); T.toast('Tubarr uses the “' + S.lib + '” library.'); go(3);
        }).catch((e) => { S.busy = false; S.libErr = e.message; render(); });
        return;
      }
      case 'subs-sample': S.text = T.mock.sampleImportText(); render(); return;
      case 'subs-preview':
        if (!S.text.trim()) return;
        S.busy = true; render();
        T.api.post('/api/import/preview', { text: S.text }).then((p) => { S.busy = false; S.preview = p; render(); }).catch((e) => { S.busy = false; render(); T.toastError(e); });
        return;
      case 'subs-back': S.preview = null; render(); return;
      case 'subs-all': case 'subs-none': root.querySelectorAll('[data-add]').forEach((c) => { c.checked = name === 'subs-all'; }); countPicked(); return;
      case 'subs-apply': {
        const add = Array.from(root.querySelectorAll('[data-add]')).filter((x) => x.checked).map((x) => x.dataset.add);
        if (!add.length) return;
        S.busy = true; render();
        T.api.post('/api/import/apply', { import_id: S.preview.import_id, add, remove: [] }).then((r) => {
          S.busy = false; S.applied = r; if (T.app.invalidateChannels) T.app.invalidateChannels(); go(4);
        }).catch((e) => { S.busy = false; render(); T.toastError(e); });
        return;
      }
      case 'skip-all':
        T.api.post('/api/setup/complete').then(() => { if (T.app) T.app.setupDone = true; T.app.navigate('#/timeline'); }).catch(T.toastError);
        return;
      default: return false;
    }
  };
})();
