/* Settings: one page, all in one place. Plex, Import, Downloads (fill target, quality and defaults), Network (direct, or
   your own proxies for YouTube traffic), Notifications, Trim ads, Account (password, API key, sign out).
   Deep links (#/settings/plex) scroll to the section. */
(function () {
  'use strict';
  const T = window.T;
  const html = T.html, icon = T.icon, ui = T.ui;
  const SECTIONS = [['plex', 'Plex', 'film'], ['import', 'Import', 'clipboard'], ['downloads', 'Downloads', 'download'], ['network', 'Network', 'server'], ['notifications', 'Notifications', 'bell'], ['trim', 'Trim ads', 'scissors'], ['account', 'Account', 'user']];
  const MAXLEN = [[0, 'No limit'], [20, '20 min'], [30, '30 min'], [60, '1 hour'], [90, '90 min'], [120, '2 hours'], [180, '3 hours']];
  const RATES = [['', 'Unlimited'], [String(2 * 1048576), '2 MB/s'], [String(4 * 1048576), '4 MB/s'], [String(6 * 1048576), '6 MB/s'], [String(10 * 1048576), '10 MB/s'], [String(20 * 1048576), '20 MB/s']];
  const HOURS = Array.from({ length: 24 }, (_, h) => [String(h).padStart(2, '0') + ':00', T.fmtHHMM(String(h).padStart(2, '0') + ':00').replace(' ', ' ')]);
  const HEIGHTS = [['2160', '2160p (4K)'], ['1440', '1440p'], ['1080', '1080p'], ['720', '720p']];
  const MIN_PW = 10;
  const DEFAULT_CAP = 3 * 1099511627776;

  const V = (T.views.settings = { nav: 'settings', title: 'Settings' });
  let root, plex = null, setupInfo = null, settings = null, draft = null, spy = null, tgtCtl = null, imp = null, trimSet = null, net = null, netTests = {}, netEdit = null, netBusy = false, apikey = null, newKey = null, audit = null, auditBusy = false, capBytes = DEFAULT_CAP;

  /* ---------- Import (Google Takeout CSV, or pasted channel links and @handles) ---------- */
  function importCard() {
    return html`<div class="imp">
      <p class="form-lead">Bring in channels from a Google Takeout file, or paste channel links and @handles, one per line. You see the changes before anything happens.</p>
      <div class="drop" data-slot="drop">
        <textarea class="input paste" data-slot="paste" rows="7" placeholder="${'youtube.com/@channel\n@anotherchannel\nhttps://www.youtube.com/channel/UC…'}" aria-label="Channel links, @handles, or the contents of subscriptions.csv"></textarea>
        <p class="drop-hint">${icon('file')}<span>Or drop a Google Takeout <code>subscriptions.csv</code> here, or <label class="link-btn file-pick">choose the file<input type="file" accept=".csv,text/csv,text/plain" data-slot="imp-file" hidden></label>. Get it at takeout.google.com → YouTube → subscriptions.</span></p>
      </div>
      <div class="btn-row">
        <button class="btn btn-primary" type="button" data-act="imp-preview" disabled>${icon('search')}Preview changes</button>
        ${T.api.mode === 'mock' ? html`<button class="btn btn-ghost" type="button" data-act="imp-sample">${icon('clipboard')}Paste a sample (preview only)</button>` : ''}
        <span class="imp-count dim" data-slot="imp-count"></span>
      </div>
      <div data-slot="diff"></div>
    </div>`;
  }
  function diffView(p) {
    const row = (kind, key, title, sub) => html`<label class="diff-row"><input type="checkbox" data-${kind}="${key}" checked><span class="diff-check">${icon('check')}</span>${T.poster({ title }, 'poster-xs')}<span class="diff-main"><b>${title}</b><small>${sub}</small></span></label>`;
    return html`<div class="diff">
      <div class="diff-sum"><span class="ds ds-found"><b>${T.fmtNum(p.found)}</b> found</span><span class="ds ds-new"><b>${p.new.length}</b> new</span><span class="ds ds-gone"><b>${p.missing.length}</b> not in this list</span><span class="ds"><b>${T.fmtNum(p.unchanged)}</b> unchanged</span></div>
      ${p.unrecognized_lines ? html`<p class="hint">${icon('info')}${T.plural(p.unrecognized_lines, 'line')} didn’t look like a channel and were skipped.</p>` : ''}
      ${!p.new.length && !p.missing.length ? html`<div class="note note-good">${icon('check-circle')}<span>Everything already matches. Nothing to change.</span></div>` : ''}
      <div class="diff-cols">
        ${p.new.length ? html`<div class="diff-col"><h4>${icon('plus')}New channels <span class="dim">added with your defaults</span></h4>${p.new.map((n) => row('add', n.handle || n.channel_id, n.title || n.handle || n.channel_id, (n.handle || n.channel_id) + (n.subscribers ? ' · ' + T.fmtCompact(n.subscribers) + ' subscribers' : '')))}</div>` : ''}
        ${p.missing.length ? html`<div class="diff-col"><h4>${icon('timer')}Not in this list <span class="dim">untick to keep; ticked ones are removed after 3 days, and you can undo</span></h4>${p.missing.map((n) => row('rm', n.channel_id, n.title, n.handle))}</div>` : ''}
      </div>
      ${p.new.length || p.missing.length ? html`<div class="btn-row"><button class="btn btn-primary" type="button" data-act="imp-apply">${icon('check')}Apply changes</button><button class="btn" type="button" data-act="imp-reset">Start over</button></div>` : ''}
    </div>`;
  }
  function bindImport() {
    const ta = root.querySelector('[data-slot="paste"]'), btn = root.querySelector('[data-act="imp-preview"]'), drop = root.querySelector('[data-slot="drop"]');
    const count = () => {
      const n = ta.value.split(/\r?\n/).filter((l) => l.trim()).length;
      btn.disabled = !ta.value.trim();
      root.querySelector('[data-slot="imp-count"]').textContent = ta.value.trim() ? T.plural(n, 'line') : '';
    };
    const readFile = (f) => {
      if (!f) return;
      if (f.size > 5 * 1048576) { T.toast('That file is too big to be a subscriptions list.', { type: 'error' }); return; }
      const fr = new FileReader();
      fr.onload = () => { ta.value = String(fr.result || ''); count(); };
      fr.onerror = () => T.toast('Couldn’t read that file.', { type: 'error' });
      fr.readAsText(f);
    };
    ta.addEventListener('input', count);
    root.querySelector('[data-slot="imp-file"]').addEventListener('change', (e) => { readFile(e.target.files && e.target.files[0]); e.target.value = ''; });
    ['dragover', 'dragenter'].forEach((t) => drop.addEventListener(t, (e) => { e.preventDefault(); drop.classList.add('is-over'); }));
    ['dragleave', 'drop'].forEach((t) => drop.addEventListener(t, () => drop.classList.remove('is-over')));
    drop.addEventListener('drop', (e) => { e.preventDefault(); readFile(e.dataTransfer.files && e.dataTransfer.files[0]); });
  }

  /* ---------- Plex ---------- */
  function plexCard() {
    const su = setupInfo && setupInfo.plex;
    if (su && !su.configured) {
      return html`<div class="plex">
        <div class="yt-head"><span class="yt-ic">${icon('film')}</span><div><h3>Plex isn’t connected</h3>
          <p>Connect Plex and new videos show up in your library by themselves, with posters and episode details. Not using Plex? Tubarr still saves every video with standard NFO info files and artwork, which Jellyfin and Emby read as-is.</p></div></div>
        <div class="btn-row"><a class="btn btn-primary" href="#/setup">${icon('link')}Connect Plex</a></div>
      </div>`;
    }
    const p = plex;
    const state = p ? p.state : su ? su.state : 'unreachable';
    const ok = state === 'connected';
    const name = (p && p.server_name) || (su && su.server_name) || 'Plex';
    const url = (p && p.url) || (su && su.url) || '';
    const msg = (p && p.message) || (su && su.message);
    const lib = p && p.library;
    return html`<div class="plex">
      <div class="plex-row">
        <span class="status-dot ${ok ? 'ok' : 'crit'}" aria-hidden="true"></span>
        <div class="plex-main"><b>${ok ? 'Connected to ' + name : state === 'unauthorized' ? 'Plex refused the sign-in' : 'Can’t reach Plex'}</b>
          <span class="dim">${url}${p && p.version ? ' · Plex Media Server ' + p.version : ''}${p && p.checked_at ? html` · checked ${T.tAgo(p.checked_at)}` : ''}</span>
          ${msg ? html`<span class="t-crit">${msg}</span>` : ''}</div>
        <button class="btn btn-sm" type="button" data-act="plex-test">${icon('refresh')}Test</button>
      </div>
      ${lib ? html`<div class="plex-lib">
        <div class="plex-lib-head"><span class="lib-ic">${icon('tv')}</span><div><h3>“${lib.name}” library ${lib.state === 'scanning' ? html`<span class="tag tag-sm"><span class="spin"></span>Scanning</span>` : lib.state === 'missing' ? html`<span class="tag tag-sm tag-warn">Not found in Plex</span>` : ''}</h3>
          <span class="dim">${T.fmtNum(lib.show_count)} shows · ${T.fmtNum(lib.episode_count)} episodes · last scan ${T.tAgo(lib.last_scan_at)}</span></div></div>
        <dl class="facts facts-3">
          <div><dt>Folder in Plex</dt><dd><code>${(su && su.plex_root) || lib.path}</code></dd></div>
          <div><dt>Agent</dt><dd>${lib.agent}</dd></div>
          <div><dt>Scanner</dt><dd>${lib.scanner}</dd></div>
        </dl>
        ${lib.shared_with_friends && lib.shared_with_friends.length ? html`<div class="note note-info">${icon('user')}<span><b>Shared with:</b> ${lib.shared_with_friends.join(', ')}. Sharing is managed in Plex (Settings → Manage Library Access).</span></div>` : ''}
        <div class="note note-info">${icon('info')}<span>Tubarr writes each show’s and episode’s details into NFO files next to the videos, so Plex (and Jellyfin or Emby) show them without matching anything online.</span></div>
        <div class="btn-row"><button class="btn" type="button" data-act="plex-scan" ${lib.state === 'scanning' ? T.raw('disabled') : ''}>${icon('refresh')}Scan library now</button><button class="btn" type="button" data-act="plex-repolish">${icon('sparkles')}Re-polish every channel</button></div>
      </div>` : su && su.library ? html`<dl class="facts facts-3"><div><dt>Library</dt><dd>${su.library}</dd></div><div><dt>Folder in Plex</dt><dd><code>${su.plex_root || '—'}</code></dd></div></dl>` : ''}
      <div class="btn-row plex-setup-row"><a class="btn btn-sm" href="#/setup">${icon('wand')}Run setup again</a><button class="btn btn-sm btn-danger-ghost" type="button" data-act="plex-clear">${icon('logout')}Disconnect Plex</button></div>
    </div>`;
  }
  function renderPlex() { const b = root && root.querySelector('[data-slot="plex"]'); if (b) T.setHTML(b, plexCard()); }

  /* ---------- Network: which line YouTube traffic goes out on (direct, or one of your proxies) ---------- */
  const MODES = [
    ['direct', 'Direct', 'No proxy: straight out of this server.'],
    ['single', 'One proxy', 'Always use one proxy you pick.'],
    ['failover', 'Failover', 'Use the first line in the order; move down it after repeated failures.'],
    ['rotate', 'Rotate', 'Take turns between the lines in the order.'],
  ];
  const URL_RE = /^(https?|socks5h?):\/\/\S+$/i;
  const URL_HINT = 'The address should start with http://, https://, socks5:// or socks5h://.';
  const lineName = (id) => { if (id === 'direct') return 'Direct'; const p = net && (net.proxies || []).find((x) => x.id === id); return p ? p.name : id; };
  const lineMasked = (id) => { const p = id !== 'direct' && net && (net.proxies || []).find((x) => x.id === id); return p ? p.masked : 'No proxy'; };
  function testOut(id) {
    const t = netTests[id];
    if (!t) return '';
    if (t.busy) return html`<span class="net-test dim"><span class="spin"></span>Testing…</span>`;
    if (t.error) return html`<span class="net-test t-crit">${icon('alert-circle')}${t.error}</span>`;
    return html`<span class="net-test t-good">${icon('check-circle')}<span class="num">${t.ip || '?'}</span>${t.latency_ms != null ? html`<span class="dim"> · ${t.latency_ms} ms</span>` : ''}</span>`;
  }
  function proxyRow(p) {
    if (netEdit === p.id) {
      return html`<li class="net-proxy is-editing" data-pid="${p.id}">
        <div class="net-edit">
          <div class="field"><label class="label" for="s-pe-name">Name</label><input class="input" id="s-pe-name" type="text" value="${p.name}" autocomplete="off" spellcheck="false"></div>
          <div class="field"><label class="label" for="s-pe-url">Address</label><input class="input" id="s-pe-url" type="password" autocomplete="off" spellcheck="false" placeholder="Leave empty to keep the saved one"><p class="hint">Now: <code>${p.masked}</code></p></div>
        </div>
        <div class="btn-row"><button class="btn btn-sm btn-primary" type="button" data-act="net-edit-save" data-id="${p.id}">${icon('check')}Save</button><button class="btn btn-sm" type="button" data-act="net-edit-cancel">Cancel</button></div>
      </li>`;
    }
    return html`<li class="net-proxy" data-pid="${p.id}">
      <span class="net-proxy-main"><b>${p.name}</b><code>${p.masked}</code>${testOut(p.id)}</span>
      <span class="net-proxy-acts">
        <button class="btn btn-sm" type="button" data-act="net-test" data-id="${p.id}" ${netTests[p.id] && netTests[p.id].busy ? T.raw('disabled') : ''}>${icon('zap')}Test</button>
        ${p.id === 'direct' ? '' : html`<button class="btn-icon" type="button" data-act="net-edit" data-id="${p.id}" aria-label="Edit ${p.name}" title="Edit">${icon('wand')}</button><button class="btn-icon" type="button" data-act="net-del" data-id="${p.id}" aria-label="Delete ${p.name}" title="Delete">${icon('trash')}</button>`}
      </span>
    </li>`;
  }
  function netCard() {
    const n = net;
    if (!n) return html`<div class="net-card is-muted"><p class="dim">Couldn’t read the network settings just now.</p><div class="btn-row"><button class="btn btn-sm" type="button" data-act="net-reload">${icon('refresh')}Try again</button></div></div>`;
    const s = n.status || {}, proxies = n.proxies || [], mode = n.mode || 'direct';
    const ls = s.last_switch;
    const tone = s.paused ? 'crit' : s.stale ? 'warn' : 'ok';
    const modeRow = MODES.find((m) => m[0] === mode) || MODES[0];
    const order = (n.order || ['direct']).filter((id) => id === 'direct' || proxies.some((p) => p.id === id));
    const num = (k, label, min, max, step, unit, hint) => html`<div class="field"><label class="label" for="s-net-${k}">${label}</label><div class="inline-2"><input class="input input-num" id="s-net-${k}" type="number" min="${min}" max="${max}" step="${step}" value="${n[k]}" data-net="${k}"><span class="dim">${unit}</span></div>${hint ? html`<p class="hint">${hint}</p>` : ''}</div>`;
    return html`<div class="net-card is-${tone}">
      <div class="yt-head"><span class="yt-ic ${tone === 'ok' ? 'ok' : ''}">${icon(s.stale || s.paused ? 'alert' : 'server')}</span><div>
        <h3>${s.paused ? html`YouTube downloads are <b>paused</b>` : html`YouTube traffic uses <b>${s.active_name || lineName(s.active || 'direct')}</b>${s.ip ? html` · <span class="num">${s.ip}</span>` : ''}`}</h3>
        <p>${modeRow[1]}: ${modeRow[2]}${s.reason ? html` <span class="dim">(${s.reason})</span>` : ''}</p></div></div>
      ${s.paused ? html`<div class="note note-crit net-paused">${icon('alert')}<span><b>YouTube downloads are paused: no proxy is answering.</b> ${s.reason || ''} Tubarr won’t fall back to your own connection while “Never fall back to Direct” is on. Test the proxies below, add another, or turn that off.</span></div>` : ''}
      ${s.stale ? html`<div class="note note-warn">${icon('alert')}<span><b>The network status hasn’t updated since ${s.updated_at ? T.fmtTime(s.updated_at) : 'a while'}</b>, so this may be out of date.</span></div>` : ''}
      <dl class="facts">
        <div><dt>On this line since</dt><dd>${s.since ? T.tAgo(s.since) : '—'}</dd></div>
        <div><dt>Latency</dt><dd>${s.latency_ms != null ? s.latency_ms + ' ms' : '—'}</dd></div>
        <div><dt>Last switch</dt><dd>${ls ? html`${ls.from_name} → ${ls.to_name}, ${T.tAgo(ls.at)}${ls.reason ? html` <span class="dim">(${ls.reason})</span>` : ''}` : 'None yet'}</dd></div>
        <div><dt>${mode === 'rotate' ? 'Next turn' : 'Back to the first line'}</dt><dd>${mode === 'rotate' ? (s.next_rotate_at ? T.fmtTime(s.next_rotate_at) + ' (between downloads)' : '—') : mode === 'failover' && s.preferred_retry_at ? 'Tries again at ' + T.fmtTime(s.preferred_retry_at) : '—'}</dd></div>
      </dl>

      <div class="form-sec net-sec">
        <h3>How to connect</h3>
        <div class="net-btns" role="group" aria-label="How YouTube traffic connects">${MODES.map(([k, l, d]) => {
          const on = mode === k;
          return html`<button class="btn ${on ? 'btn-primary' : ''}" type="button" data-act="net-mode" data-mode="${k}" aria-pressed="${on ? 'true' : 'false'}" ${netBusy ? T.raw('disabled') : ''}><span class="net-lbl">${on ? icon('check') : ''}${l}</span><small class="net-ip">${d}</small></button>`;
        })}</div>
        ${mode === 'single' ? html`<div class="field net-field"><label class="label" for="s-net-sel">Which proxy</label>${ui.select('net-sel', proxies.map((p) => [p.id, p.name]), n.selected || '', { attrs: 'id="s-net-sel" data-net="selected"' })}</div>` : ''}
        ${mode === 'failover' || mode === 'rotate' ? html`<div class="field net-field"><span class="label">Order</span>
            <ol class="net-order">${order.map((id, i) => html`<li><span class="step-n">${i + 1}</span><span class="net-order-main"><b>${lineName(id)}</b><small class="dim">${lineMasked(id)}</small></span>
              <button class="btn-icon" type="button" data-act="net-up" data-i="${i}" aria-label="Move ${lineName(id)} up" ${i === 0 ? T.raw('disabled') : ''}>${icon('chev-down', 'flip')}</button><button class="btn-icon" type="button" data-act="net-down" data-i="${i}" aria-label="Move ${lineName(id)} down" ${i === order.length - 1 ? T.raw('disabled') : ''}>${icon('chev-down')}</button></li>`)}</ol>
            <p class="hint">${mode === 'failover' ? 'The first line is preferred. Direct counts as a line too.' : 'Lines take turns in this order. Direct counts as a line too.'}</p></div>
          <div class="field-grid">${mode === 'failover' ? html`
            ${num('fail_threshold', 'Switch after', 1, 20, 1, 'failures in a row', 'Connection failures before moving to the next line.')}
            ${num('cooldown_minutes', 'Try the first line again after', 5, 10080, 5, 'minutes', '')}
            <div class="field"><span class="label">Check before going back</span>${ui.switch('net-check', !!n.check_preferred, { text: n.check_preferred ? 'On' : 'Off', attrs: 'data-net="check_preferred"' })}<p class="hint">One quick test of the first line before returning to it.</p></div>`
            : num('rotate_hours', 'Take turns every', 0.5, 168, 0.5, 'hours', 'The switch waits until no download is running.')}</div>` : ''}
        ${proxies.length || n.never_direct ? html`<div class="field net-field"><span class="label">Never fall back to Direct</span>${ui.switch('net-nd', !!n.never_direct, { text: (n.never_direct ? 'On' : 'Off') + (n.never_direct_auto ? ' (automatic)' : ''), attrs: 'data-net="never_direct"' })}<p class="hint">${icon('shield')}<span>When every proxy is down, pause downloads instead of using your own connection (protects your real IP). On by default once you add a proxy.</span></p></div>` : ''}
      </div>

      <div class="form-sec net-sec">
        <h3>Lines</h3>
        <ul class="net-proxies">${[{ id: 'direct', name: 'Direct', masked: 'No proxy' }].concat(proxies).map(proxyRow)}</ul>
        ${proxies.length ? '' : html`<p class="hint">${icon('info')}No proxies yet. Everything goes direct.</p>`}
        <div class="net-add">
          <h4>Add a proxy</h4>
          <div class="net-edit">
            <div class="field"><label class="label" for="s-np-name">Name</label><input class="input" id="s-np-name" type="text" autocomplete="off" spellcheck="false" placeholder="Backup line"></div>
            <div class="field"><label class="label" for="s-np-url">Address</label><input class="input" id="s-np-url" type="password" autocomplete="off" spellcheck="false" placeholder="socks5h://user:pass@host:1080"></div>
          </div>
          <div class="btn-row"><button class="btn btn-primary" type="button" data-act="net-add">${icon('plus')}Add proxy</button></div>
        </div>
        <p class="hint">${icon('info')}<span>Only YouTube traffic uses these; Plex, the PO-token helper and local traffic always go direct.</span></p>
        <p class="hint">${icon('shield')}<span>Credentials are stored encrypted and never shown again.</span></p>
      </div>
    </div>`;
  }
  function renderNet() { const b = root && root.querySelector('[data-slot="net"]'); if (b) T.setHTML(b, netCard()); }
  function setNet(n) { net = n; renderNet(); if (T.app.refreshNetChip) T.app.refreshNetChip(n); }
  function patchNet(body, ok) {
    netBusy = true; renderNet();
    return T.api.patch('/api/network', body).then((n) => { netBusy = false; setNet(n); if (ok) T.toast(ok); return n; })
      .catch((e) => { netBusy = false; renderNet(); T.toastError(e); });
  }
  function bindNet() {
    const sec = root.querySelector('#set-network');
    sec.addEventListener('change', (e) => {
      const t = e.target, k = t.dataset.net;
      if (!k || !net) return;
      if (k === 'check_preferred') { patchNet({ check_preferred: t.checked }, t.checked ? 'Tubarr tests the first line before going back to it.' : 'Tubarr goes back to the first line without testing it first.'); return; }
      if (k === 'never_direct') { patchNet({ never_direct: t.checked }, t.checked ? 'Downloads pause instead of going direct when every proxy is down.' : 'Tubarr may use your own connection when every proxy is down.'); return; }
      if (k === 'selected') { patchNet({ selected: t.value }, 'YouTube traffic uses ' + lineName(t.value) + '.'); return; }
      const v = Number(t.value);
      if (t.value === '' || !isFinite(v) || v < Number(t.min) || v > Number(t.max)) { T.toast('Use a number from ' + t.min + ' to ' + t.max + '.', { type: 'error' }); t.value = net[k]; return; }
      patchNet({ [k]: v }, 'Saved.');
    });
  }

  /* ---------- Account: password, API key, sign out ---------- */
  function accountCard() {
    const a = T.api.auth || {};
    const mock = T.api.mode === 'mock';
    return html`<div class="acct">
      <div class="yt-head"><span class="yt-ic ok">${icon('user')}</span><div><h3>Signed in as <b>${a.user || 'admin'}</b></h3>
        <p>${a.via === 'api_key' ? 'This request came in with the API key.' : 'This browser stays signed in until you sign out.'}${mock ? ' (Preview mode: nothing here is real.)' : ''}</p></div>
        <button class="btn btn-sm" type="button" data-act="logout">${icon('logout')}Sign out</button></div>
      <div class="form-sec acct-sec">
        <h3>Change password</h3>
        <p class="form-lead">Every other signed-in browser is signed out when it changes.</p>
        <form class="acct-form" data-form="password" novalidate>
          <input type="text" name="username" autocomplete="username" value="${a.user || ''}" hidden>
          <div class="field"><label class="label" for="s-pw-cur">Current password</label><input class="input" id="s-pw-cur" name="current" type="password" autocomplete="current-password"></div>
          <div class="field"><label class="label" for="s-pw-new">New password</label><input class="input" id="s-pw-new" name="next" type="password" autocomplete="new-password" minlength="${MIN_PW}"><p class="hint">At least ${MIN_PW} characters.</p></div>
          <div class="field"><label class="label" for="s-pw-conf">Confirm new password</label><input class="input" id="s-pw-conf" name="confirm" type="password" autocomplete="new-password"></div>
          <p class="auth-err" data-slot="pw-err" role="alert" hidden></p>
          <button class="btn btn-primary" type="submit">${icon('lock')}Change password</button>
        </form>
      </div>
      <div class="form-sec acct-sec" data-slot="apikey">${apiKeyCard()}</div>
      <div class="form-sec acct-sec" data-slot="audit">${auditCard()}</div>
    </div>`;
  }
  function apiKeyCard() {
    const k = apikey;
    return html`<h3>API key</h3>
      <p class="form-lead">For scripts and other apps: send it as the <code>X-Api-Key</code> header. Never put it in a URL.</p>
      <p class="hint">${icon('lock')}<span>Read-only: status and lists; it can’t change anything.</span></p>
      ${!k ? html`<p class="dim">Couldn’t read the API key state just now.</p>`
        : html`<p class="acct-key-state">${k.exists ? html`${icon('check-circle')}A key is set <code>${k.hint || ''}</code>${k.created_at ? html` <span class="dim">· created ${T.fmtDay(k.created_at, true)}</span>` : ''}` : html`${icon('minus-circle')}No API key yet.`}</p>`}
      ${newKey ? html`<div class="key-once">
          <div class="note note-warn">${icon('alert')}<span><b>Copy it now; Tubarr only keeps a one-way hash.</b> It won’t be shown again.</span></div>
          <div class="input-group"><input class="input key-field" id="s-newkey" type="text" readonly value="${newKey}" aria-label="Your new API key" spellcheck="false"><button class="btn" type="button" data-act="key-copy">${icon('clipboard')}Copy</button></div>
        </div>` : ''}
      ${k ? html`<div class="btn-row"><button class="btn ${k.exists ? '' : 'btn-primary'}" type="button" data-act="key-gen">${icon('refresh')}${k.exists ? 'Regenerate' : 'Generate a key'}</button>${k.exists ? html`<button class="btn btn-danger-ghost" type="button" data-act="key-revoke">${icon('trash')}Revoke</button>` : ''}</div>` : ''}`;
  }
  const AUDIT_LABELS = {
    'auth.login': 'Signed in', 'auth.login_failed': 'Failed sign-in', 'auth.logout': 'Signed out', 'auth.locked': 'Sign-in locked',
    'auth.setup': 'Admin account created', 'auth.password_changed': 'Password changed',
    'auth.apikey_created': 'API key created', 'auth.apikey_revoked': 'API key revoked',
    'network.proxy_added': 'Proxy added', 'network.proxy_updated': 'Proxy changed', 'network.proxy_deleted': 'Proxy deleted',
    'network.mode': 'Network mode changed', 'network.settings': 'Network settings changed', 'network.switch': 'Switched line', 'network.paused': 'Downloads paused (no proxy)',
    'plex.connected': 'Plex connected', 'plex.cleared': 'Plex disconnected', 'setup.complete': 'Setup finished', 'settings.changed': 'Settings changed',
  };
  const auditLabel = (a) => AUDIT_LABELS[a] || String(a || '').replace(/[._]/g, ' ').replace(/^\w/, (c) => c.toUpperCase());
  function auditCard() {
    const list = audit && audit.entries;
    return html`<div class="audit-head"><div><h3>Security log</h3><p class="form-lead">Sign-ins, password and key changes, and network changes. Newest first.</p></div>
        <button class="btn btn-sm" type="button" data-act="audit-refresh" ${auditBusy ? T.raw('disabled') : ''}>${auditBusy ? html`<span class="spin"></span>` : icon('refresh')}Refresh</button></div>
      ${!audit ? html`<p class="dim">Couldn’t read the security log just now.</p>` : !list.length ? html`<p class="dim">Nothing logged yet.</p>`
        : html`<ul class="audit">${list.map((e) => html`<li class="${/failed|locked|paused/.test(e.action || '') ? 'is-warn' : ''}"><span class="audit-when" title="${e.at ? new Date(e.at).toLocaleString() : ''}">${T.fmtAgo(e.at)}</span><span class="audit-who">${e.who}</span><span class="audit-main"><b>${auditLabel(e.action)}</b>${e.detail ? html`<small>${e.detail}</small>` : ''}</span></li>`)}</ul>`}`;
  }
  function renderAudit() { const b = root && root.querySelector('[data-slot="audit"]'); if (b) T.setHTML(b, auditCard()); }
  function renderKey() { const b = root && root.querySelector('[data-slot="apikey"]'); if (b) T.setHTML(b, apiKeyCard()); }
  function bindAccount() {
    const f = root.querySelector('[data-form="password"]');
    if (!f) return;
    const err = f.querySelector('[data-slot="pw-err"]');
    const say = (m) => { err.hidden = !m; err.textContent = m || ''; };
    f.addEventListener('submit', async (e) => {
      e.preventDefault();
      say('');
      const cur = f.elements.current.value, nx = f.elements.next.value, cf = f.elements.confirm.value;
      if (!cur) { say('Enter your current password.'); return; }
      if (nx.length < MIN_PW) { say('The new password needs at least ' + MIN_PW + ' characters.'); return; }
      if (nx !== cf) { say('The two new passwords don’t match.'); return; }
      const btn = f.querySelector('button[type="submit"]');
      btn.disabled = true;
      try {
        await T.api.post('/api/auth/password', { current_password: cur, new_password: nx });
        try { await T.api.authState(); } catch (x) { /* the next request fetches a fresh token if needed */ }
        f.reset();
        T.toast('Password changed. Other browsers were signed out.');
      } catch (x) {
        say(x.status === 403 ? (x.message || 'The current password isn’t right.') : x.message);
      } finally { btn.disabled = false; }
    });
  }
  function copyText(input) {
    const v = input.value;
    const done = () => T.toast('Copied.');
    if (navigator.clipboard && window.isSecureContext) { navigator.clipboard.writeText(v).then(done, () => { input.select(); document.execCommand('copy'); done(); }); return; }
    input.focus(); input.select();
    try { document.execCommand('copy'); done(); } catch (e) { T.toast('Select the key and copy it by hand.', { type: 'info' }); }
  }

  /* ---------- Downloads + Notifications (one draft, one save bar) ---------- */
  function hourOf(hhmm) { return Number(hhmm.split(':')[0]); }
  function paceBar(p) {
    const a = hourOf(p.overnight_start), b = hourOf(p.overnight_end);
    const cells = Array.from({ length: 24 }, (_, h) => { const night = a <= b ? h >= a && h < b : h >= a || h < b; return html`<i class="${night ? 'night' : 'day'}" title="${T.fmtHHMM(String(h).padStart(2, '0') + ':00')}"></i>`; });
    return html`<div class="pace"><div class="pace-bar" aria-hidden="true">${cells}</div><div class="pace-scale" aria-hidden="true"><span>12 AM</span><span>6 AM</span><span>12 PM</span><span>6 PM</span><span>12 AM</span></div>
      <p class="hint">${icon('moon')}Backlog downloads run ${T.fmtHHMM(p.overnight_start)}–${T.fmtHHMM(p.overnight_end)}. ${p.daytime === 'new_only' ? 'During the day only new uploads download.' : p.daytime === 'all' ? 'During the day everything downloads.' : 'Nothing downloads during the day.'}</p></div>`;
  }
  /* Best quality + AV1: shared by both forms below. */
  function qualityFields(d) {
    return html`<div class="field"><label class="label" for="s-maxh">Best quality</label>${ui.select('maxh', HEIGHTS, String(d.max_height || 2160), { attrs: 'id="s-maxh" data-d="max_height"' })}<p class="hint">The best copy up to this. 4K takes about four times the space of 1080p.</p></div>
      <div class="field"><span class="label">Allow AV1</span>${ui.switch('av1', !!d.allow_av1, { text: d.allow_av1 ? 'Allowed' : 'Off', attrs: 'data-d="allow_av1"' })}<p class="hint">Off keeps files playable on older TVs/streamers and GPUs without AV1 decoding.</p></div>`;
  }
  function spaceSec(d, lead) {
    return html`<div class="form-sec">
        <h3>Space</h3>
        <p class="form-lead">${lead}</p>
        <div data-slot="target">${T.retention.renderTarget(d.fill_target_bytes, capBytes)}</div>
        <div class="field-row">
          <div class="field"><label class="label" for="s-protect">Never remove each channel’s newest</label>${ui.select('protect', [0, 1, 2, 3, 5, 10].map((n) => [String(n), n === 0 ? 'None (no protection)' : n + ' video' + (n === 1 ? '' : 's')]), String(d.protect_newest), { attrs: 'id="s-protect" data-d="protect_newest"' })}</div>
        </div>
      </div>`;
  }
  function downloadsForm() {
    const d = draft.downloads, p = d.pacing;
    if (!p) return downloadsFormAuto(d);
    return html`<div class="form" data-form="downloads">
      ${spaceSec(d, 'Tubarr fills the library up to this target, then rolls: each new video pushes out watched ones first, then the oldest backlog of the biggest channel.')}
      <div class="form-sec">
        <h3>Defaults for every channel</h3>
        <p class="form-lead">A channel follows these unless you give it its own rule on its page.</p>
        <div class="field-grid">
          ${qualityFields(d)}
          <div class="field"><label class="label" for="s-maxlen">Maximum length</label>${ui.select('maxlen', MAXLEN.map(([m, l]) => [String(m), l]), String(d.max_duration_minutes || 0), { attrs: 'id="s-maxlen" data-d="max_duration_minutes"' })}<p class="hint">Longer ones are skipped (you can still grab one by hand).</p></div>
          <div class="field"><span class="label">Livestream replays</span>${ui.switch('live', d.include_live, { text: d.include_live ? 'Included' : 'Skipped', attrs: 'data-d="include_live"' })}</div>
          <div class="field"><span class="label">Shorts</span>${ui.switch('shorts', true, { disabled: true, text: 'Always skipped' })}<p class="hint">${icon('lock')}Shorts never download.</p></div>
        </div>
        <div class="field"><span class="label">SponsorBlock: cut these</span>
          <div class="sb-chips">${ui.SB.map(([k, l]) => html`<label class="chip chip-check"><input type="checkbox" data-sbd="${k}" ${d.sponsorblock.includes(k) ? T.raw('checked') : ''}><span>${icon('check')}${l}</span></label>`)}</div>
          <p class="hint">${icon('info')}Intros are never cut: they become a chapter. Segments come from the SponsorBlock community.</p></div>
        <div class="field"><label class="label" for="s-sbwait">Wait for SponsorBlock segments</label>${ui.select('sbwait', [['0', 'Don’t wait'], ['2', 'Up to 2 hours'], ['6', 'Up to 6 hours'], ['12', 'Up to 12 hours'], ['24', 'Up to a day']], String(d.sponsorblock_wait_hours), { attrs: 'id="s-sbwait" data-d="sponsorblock_wait_hours"' })}<p class="hint">New uploads often get their segments a few hours after release. Waiting means fewer sponsor reads.</p></div>
      </div>
      <div class="form-sec">
        <h3>Pacing</h3>
        <div class="note note-info">${icon('lock')}<span>Downloads run <b>one at a time</b>, so the server and your internet stay responsive.</span></div>
        <div class="field-grid">
          <div class="field"><span class="label">Overnight window</span><div class="inline-2">${ui.select('ostart', HOURS, p.overnight_start, { label: 'Starts', attrs: 'data-p="overnight_start"' })}<span class="dim">to</span>${ui.select('oend', HOURS, p.overnight_end, { label: 'Ends', attrs: 'data-p="overnight_end"' })}</div></div>
          <div class="field"><span class="label">During the day</span>${ui.seg('daytime', [['new_only', 'New uploads only'], ['all', 'Everything'], ['none', 'Nothing']], p.daytime, { label: 'During the day', attrs: 'data-p="daytime"', cls: 'seg-wrap' })}</div>
          <div class="field"><label class="label" for="s-dayrate">Daytime speed limit</label>${ui.select('dayrate', RATES, p.day_rate_limit_bps == null ? '' : String(p.day_rate_limit_bps), { attrs: 'id="s-dayrate" data-p="day_rate_limit_bps"' })}</div>
          <div class="field"><label class="label" for="s-nightrate">Overnight speed limit</label>${ui.select('nightrate', RATES, p.night_rate_limit_bps == null ? '' : String(p.night_rate_limit_bps), { attrs: 'id="s-nightrate" data-p="night_rate_limit_bps"' })}</div>
          <div class="field"><label class="label" for="s-check">Check for new uploads every</label>${ui.select('check', [['5', '5 minutes (new uploads within minutes)'], ['10', '10 minutes'], ['15', '15 minutes'], ['30', '30 minutes'], ['60', 'hour']], String(d.check_interval_minutes), { attrs: 'id="s-check" data-d="check_interval_minutes"' })}</div>
        </div>
        <div data-slot="pace">${paceBar(p)}</div>
      </div>
    </div>`;
  }
  /* The real downloader: human pace (one video, a short random pause, a daily cap), videos kept as uploaded.
     Every value here is the live one the worker uses; saving applies within seconds, nothing restarts. */
  function downloadsFormAuto(d) {
    const c = d.concurrency || {}, hp = d.human_pace || {}, conc = c.max || 1;
    return html`<div class="form" data-form="downloads">
      <div class="form-sec">
        <h3>Pacing</h3>
        <p class="form-lead">Tubarr downloads the way a person saving videos would: ${conc <= 1 ? 'one video at a time, one after another' : 'up to ' + conc + ' at once'}, with a short random pause between downloads and a daily limit. Changes apply within seconds.</p>
        <div class="field-grid">
          <div class="field"><label class="label" for="s-gapmin">Minutes between downloads</label>
            <div class="inline-2"><input class="input input-num" id="s-gapmin" type="number" min="0" max="1440" step="any" value="${hp.gap_min_minutes}" data-hp="gap_min_minutes" aria-label="Shortest pause, minutes"><span class="dim">to</span><input class="input input-num" type="number" min="0" max="1440" step="any" value="${hp.gap_max_minutes}" data-hp="gap_max_minutes" aria-label="Longest pause, minutes"><span class="dim">min</span></div>
            <p class="hint">After each download starts, Tubarr waits a random time in this range. 0 to 0 means no pause.</p></div>
          <div class="field"><label class="label" for="s-perday">Max downloads per day</label><input class="input input-num" id="s-perday" type="number" min="1" max="1000" step="1" value="${hp.per_day_max}" data-hp="per_day_max"><p class="hint">Counted over the last 24 hours.</p></div>
          <div class="field"><span class="label">Downloads at once</span><p class="static-val"><b class="num">${conc}</b> ${conc <= 1 ? '(one after another, never in parallel)' : ''}</p><p class="hint">${icon('lock')}Set by the server.</p></div>
        </div>
      </div>
      ${spaceSec(d, 'Tubarr fills the library up to this target, then rolls: each new video pushes out the oldest one in the library.')}
      <div class="form-sec">
        <h3>What gets downloaded</h3>
        <div class="field-grid">${qualityFields(d)}</div>
        <dl class="facts facts-3">
          <div><dt>New uploads checked</dt><dd>every ${d.check_interval_minutes} min</dd></div>
          <div><dt>Shorts</dt><dd>Always skipped</dd></div>
          <div><dt>Livestream replays</dt><dd>${d.include_live ? 'Included' : 'Skipped'}</dd></div>
          <div><dt>Speed limit</dt><dd>${d.rate_limit_bps ? T.fmtSpeed(d.rate_limit_bps) + ' each' : 'None'}</dd></div>
        </dl>
        <div class="note note-info">${icon('film')}<span><b>Videos stay exactly as uploaded.</b> Tubarr doesn’t cut sponsor segments. <a href="#/settings/trim">Trim ads</a> (Trimarr) trims them afterwards and can undo it.</span></div>
        ${d.subtitles ? html`<p class="hint">${icon('info')}${d.subtitles}</p>` : ''}
      </div>
    </div>`;
  }
  function notifForm() {
    const n = draft.notifications;
    const EVENTS = [['download_failed', 'A download failed', 'After the last retry'], ['channel_changes', 'Channel changes', 'Added, flagged for removal, removed'], ['storage_warning', 'Storage', 'The library started rolling, or got close to the limit'], ['daily_summary', 'Morning summary', 'What landed overnight, at 8 AM'], ['each_download', 'Every download', 'One message per video (chatty)']].filter(([k]) => k in (n.events || {}));
    return html`<div class="form" data-form="notifications">
      <div class="field"><label class="label" for="s-hook">Discord webhook</label>
        <div class="input-group"><input class="input" id="s-hook" type="password" autocomplete="off" spellcheck="false" placeholder="${n.configured && n.discord_webhook_url !== '' ? 'Saved (' + (n.hint || 'hidden') + '). Paste a new link to replace it' : 'https://discord.com/api/webhooks/…'}" value="${n.discord_webhook_url || ''}" data-n="discord_webhook_url">
          <button class="btn-icon" type="button" data-act="hook-show" aria-label="Show the webhook URL">${icon('eye')}</button>
          <button class="btn" type="button" data-act="hook-test">${icon('send')}Send test</button>
          ${n.configured && n.discord_webhook_url !== '' ? html`<button class="btn" type="button" data-act="hook-clear">Remove</button>` : ''}</div>
        <p class="hint">Discord: channel settings → Integrations → Webhooks → New webhook → Copy URL. The saved link is kept secret: it's never shown again. Remove it to turn notifications off.</p></div>
      <ul class="toggles">${EVENTS.map(([k, l, d]) => html`<li><span><b>${l}</b><small>${d}</small></span>${ui.switch('ev-' + k, n.events[k], { label: l, attrs: 'data-ev="' + k + '"' })}</li>`)}</ul>
    </div>`;
  }
  const clone = (o) => JSON.parse(JSON.stringify(o));
  function dirtyParts() {
    const out = {};
    if (JSON.stringify(draft.downloads) !== JSON.stringify(settings.downloads)) out.downloads = draft.downloads;
    if (JSON.stringify(draft.notifications) !== JSON.stringify(settings.notifications)) out.notifications = draft.notifications;
    return out;
  }
  let barSig = null;
  function renderSaveBar() {
    const bar = root.querySelector('[data-slot="savebar"]');
    const d = dirtyParts(), keys = Object.keys(d);
    bar.classList.toggle('open', keys.length > 0);
    const sig = keys.join() + '|' + !!bar.querySelector('[data-act="save"][disabled]');
    if (sig === barSig && bar.firstChild) return;
    barSig = sig;
    T.setHTML(bar, keys.length ? html`<span>${icon('info')}Unsaved changes in ${keys.map((k) => k === 'downloads' ? 'Downloads' : 'Notifications').join(' and ')}</span><button class="btn btn-sm" type="button" data-act="discard">Discard</button><button class="btn btn-sm btn-primary" type="button" data-act="save">${icon('check')}Save changes</button>` : '');
  }
  function bindForms() {
    const f = root.querySelector('[data-form="downloads"]');
    const switchText = (t, on, off) => { const txt = t.closest('.switch').querySelector('.switch-text'); if (txt) txt.textContent = t.checked ? on : off; };
    f.addEventListener('change', (e) => {
      const t = e.target, d = draft.downloads;
      if (t.dataset.d) {
        const k = t.dataset.d;
        if (k === 'include_live') { d[k] = t.checked; switchText(t, 'Included', 'Skipped'); }
        else if (k === 'allow_av1') { d[k] = t.checked; switchText(t, 'Allowed', 'Off'); }
        else if (k === 'max_height') { d.max_height = Number(t.value); d.quality = t.value + 'p'; }
        else d[k] = Number(t.value);
      } else if (t.dataset.p && d.pacing) {
        const k = t.dataset.p;
        d.pacing[k] = /rate/.test(k) ? (t.value === '' ? null : Number(t.value)) : t.value;
        T.setHTML(root.querySelector('[data-slot="pace"]'), paceBar(d.pacing));
      } else if (t.dataset.hp) {
        d.human_pace = d.human_pace || {};
        d.human_pace[t.dataset.hp] = Number(t.value);
      } else if (t.dataset.sbd) {
        d.sponsorblock = Array.from(f.querySelectorAll('[data-sbd]')).filter((x) => x.checked).map((x) => x.dataset.sbd);
      }
      renderSaveBar();
    });
    f.addEventListener('input', (e) => { const t = e.target; if (t.dataset.hp && t.type === 'number' && t.value !== '') { draft.downloads.human_pace[t.dataset.hp] = Number(t.value); renderSaveBar(); } });
    tgtCtl = T.retention.bindTarget(f, { onChange: (b) => { draft.downloads.fill_target_bytes = b; renderSaveBar(); } });
    bindNotif();
  }
  function bindNotif() {
    const n = root.querySelector('[data-form="notifications"]');
    n.addEventListener('input', (e) => { if (e.target.dataset.n) { const v = e.target.value.trim(); if (v) draft.notifications.discord_webhook_url = v; else delete draft.notifications.discord_webhook_url; renderSaveBar(); } });
    n.addEventListener('change', (e) => { if (e.target.dataset.ev) { draft.notifications.events[e.target.dataset.ev] = e.target.checked; renderSaveBar(); } });
  }

  /* ---------- Trim ads (optional Trimarr add-on) ---------- */
  function trimCard() {
    const s = T.trimarr.status || {};
    if (!s.available) {
      return html`<div class="trim-card is-missing"><div class="trim-head"><span class="trim-ic">${icon('scissors')}</span><div>
        <h3>Trimarr isn’t installed</h3><p>${s.reason === 'unreachable' ? 'Tubarr can’t reach it right now. ' : ''}Trimarr is an optional add-on that trims sponsor segments out of videos that are already in Plex. Tubarr itself keeps every video exactly as uploaded.</p></div></div></div>`;
    }
    return html`<div class="trim-card ${s.enabled ? 'is-on' : 'is-off'}">
      <div class="trim-head"><span class="trim-ic">${icon('scissors')}</span>
        <div><h3>Trimarr is <b>${s.enabled ? 'on' : 'off'}</b></h3>
          <p>${T.plural(s.trimmed_videos || 0, 'video')} trimmed · ${T.fmtSpan(s.removed_seconds || 0)} of ads removed${s.queue ? html` · ${s.queue} waiting` : ''}${s.last_run_at ? html` · last ran ${T.tAgo(s.last_run_at)}` : ''}</p></div>
        ${ui.switch('trim-global', !!s.enabled, { label: 'Trimarr on or off', attrs: 'data-trim-global=""' })}</div>
      <p class="hint">${icon('info')}Tubarr keeps videos exactly as uploaded; Trimarr trims sponsor segments out afterwards, using SponsorBlock. Choose channels on each channel’s page. Every trim can be undone from the video’s menu.</p>
      ${trimSettings()}
      <div class="btn-row"><a class="btn btn-sm btn-primary" href="#/trim">${icon('scissors')}Open the Trim ads page</a><button class="btn btn-sm" type="button" data-act="trim-report">${icon('search')}Dry run: what would it trim?</button></div>
      <div data-slot="trim-report"></div>
    </div>`;
  }
  const TRIM_CATS = { sponsor: 'Sponsor reads', selfpromo: 'Self-promotion', interaction: 'Like/subscribe reminders', intro: 'Intros', outro: 'End cards', preview: 'Previews', filler: 'Filler' };
  function trimSettings() {
    const t = trimSet;
    if (!t) return '';
    return html`<div class="trim-set">
      <div class="field"><span class="label">SponsorBlock: cut these</span>
        <div class="sb-chips">${Object.keys(TRIM_CATS).map((k) => html`<label class="chip chip-check"><input type="checkbox" data-trimcat="${k}" ${(t.categories || []).includes(k) ? T.raw('checked') : ''}><span>${icon('check')}${TRIM_CATS[k]}</span></label>`)}</div>
        <p class="hint">Saved as soon as you tick a box. These are Trimarr’s live settings.</p></div>
      <dl class="facts facts-3">
        <div><dt>Only videos at least</dt><dd>${t.min_upload_age_hours} h old</dd></div>
        <div><dt>Never cuts more than</dt><dd>${Math.round((t.max_removed_fraction || 0) * 100)}% of a video</dd></div>
        <div><dt>Originals kept for undo</dt><dd>${t.keep_originals_days} day${t.keep_originals_days === 1 ? '' : 's'}, up to ${Math.round(t.max_originals_gb)} GB</dd></div>
        <div><dt>New channels</dt><dd>${t.channel_default ? 'Trimmed' : 'Not trimmed'}</dd></div>
        <div><dt>Runs</dt><dd>every ${t.pass_interval_hours} h</dd></div>
      </dl>
    </div>`;
  }
  function renderTrim() { const b = root.querySelector('[data-slot="trim"]'); if (b) T.setHTML(b, trimCard()); }

  /* ---------- Page ---------- */
  function sectionHtml(id, title, ic, lead, body) {
    return html`<section class="set-sec" id="set-${id}" data-sec="${id}" aria-labelledby="set-${id}-h">
      <header class="set-head"><span class="set-ic">${icon(ic)}</span><div><h2 id="set-${id}-h">${title}</h2>${lead ? html`<p>${lead}</p>` : ''}</div></header>
      <div class="card set-card">${body}</div>
    </section>`;
  }
  V.section = function (name, smooth) {
    const el = root && root.querySelector('#set-' + name);
    if (!el) return;
    if (name === SECTIONS[0][0] && !smooth) { window.scrollTo(0, 0); markSub(name); return; }
    const y = el.getBoundingClientRect().top + window.scrollY - (window.innerWidth < 760 ? 118 : 84);
    window.scrollTo({ top: Math.max(0, y), behavior: smooth && !T.reducedMotion() ? 'smooth' : 'auto' });
    markSub(name);
  };
  function markSub(name) {
    root.querySelectorAll('[data-jump]').forEach((a) => a.classList.toggle('active', a.dataset.jump === name));
    T.$$('[data-navsub="settings"] a').forEach((a) => a.classList.toggle('active', a.dataset.sub === name));
  }
  const HEAD = html`<header class="page-head"><div class="page-title"><h1>Settings</h1><p class="page-sub">Everything in one place: Plex, how space is used, quality, alerts and your account.</p></div></header>`;
  V.enter = async function (el, route) {
    root = el; imp = null; newKey = null; netTests = {}; netEdit = null; netBusy = false;
    T.setHTML(root, html`<div class="page page-settings">${HEAD}<div class="set-loading"><span class="spin spin-lg"></span></div></div>`);
    let st;
    try {
      [setupInfo, plex, settings, , trimSet, net, apikey, st, audit] = await Promise.all([
        T.api.get('/api/setup').catch(() => null), T.api.get('/api/plex').catch(() => null), T.api.get('/api/settings'), T.trimarr.load(),
        T.api.get('/api/trimarr/raw/settings').catch(() => null), T.api.get('/api/network').catch(() => null),
        T.api.get('/api/auth/apikey').catch(() => null), T.live.status ? Promise.resolve(T.live.status) : T.api.get('/api/status').catch(() => null),
        T.api.get('/api/audit?limit=100').catch(() => null),
      ]);
    } catch (e) { T.setHTML(root, html`<div class="page">${ui.errorState(e)}</div>`); return; }
    capBytes = (st && st.storage && st.storage.cap_bytes) || DEFAULT_CAP;
    draft = clone(settings);
    T.setHTML(root, html`<div class="page page-settings">
      ${HEAD}
      <div class="set-layout">
        <nav class="set-nav" aria-label="Settings sections">${SECTIONS.map(([k, l, ic]) => html`<a href="#/settings/${k}" data-jump="${k}">${icon(ic)}${l}</a>`)}</nav>
        <div class="set-main">
          ${sectionHtml('plex', 'Plex', 'film', 'Where your YouTube library lives in Plex.', html`<div data-slot="plex">${plex || setupInfo ? plexCard() : ui.errorState({ message: 'Plex status is unavailable right now.' }, 'plex-test')}</div>`)}
          ${sectionHtml('import', 'Import subscriptions', 'clipboard', 'Add many channels at once: a Google Takeout file, or a list of links and @handles.', importCard())}
          ${sectionHtml('downloads', 'Downloads', 'download', 'How fast, how much space, and what to download. These are the live values.', downloadsForm())}
          ${sectionHtml('network', 'Network', 'server', 'How Tubarr reaches YouTube: directly, or through your own proxies.', html`<div data-slot="net">${netCard()}</div>`)}
          ${sectionHtml('notifications', 'Notifications', 'bell', 'Get a Discord message when something needs you.', notifForm())}
          ${sectionHtml('trim', 'Trim ads', 'scissors', 'Optional: the Trimarr add-on cleans sponsor reads out of videos already in Plex.', html`<div data-slot="trim">${trimCard()}</div>`)}
          ${sectionHtml('account', 'Account', 'user', 'Your sign-in, and a key for scripts.', accountCard())}
        </div>
      </div>
      <div class="savebar" data-slot="savebar" role="region" aria-label="Unsaved changes"></div>
    </div>`);
    bindImport(); bindForms(); bindAccount(); bindNet();
    root.querySelector('#set-trim').addEventListener('change', (e) => {
      if (e.target.dataset.trimcat != null) { V.act('trim-cat', e.target); return; }
      if (e.target.dataset.trimGlobal == null) return;
      const on = e.target.checked;
      T.trimarr.setEnabled(on).then(() => { renderTrim(); T.toast(on ? 'Trimarr is on. It trims the channels you pick, a few videos at a time.' : 'Trimarr is off. Nothing more gets trimmed.'); })
        .catch((err) => { e.target.checked = !on; T.toastError(err); });
    });
    // highlight the section in view
    spy = new IntersectionObserver((ents) => { const vis = ents.filter((x) => x.isIntersecting).sort((a, b) => a.boundingClientRect.top - b.boundingClientRect.top)[0]; if (vis) markSub(vis.target.dataset.sec); }, { rootMargin: '-30% 0px -60% 0px' });
    root.querySelectorAll('.set-sec').forEach((s) => spy.observe(s));
    requestAnimationFrame(() => V.section(route.section || 'plex', false));
  };
  V.leave = function () {
    newKey = null;
    if (spy) spy.disconnect(); spy = null;
  };
  V.refresh = () => {
    Promise.all([T.api.get('/api/setup').catch(() => setupInfo), T.api.get('/api/plex').catch(() => plex)]).then(([s, p]) => { setupInfo = s; plex = p; renderPlex(); });
  };
  V.act = function (name, el) {
    const run = (p, ok) => p.then((r) => { if (ok) T.toast(typeof ok === 'function' ? ok(r) : ok); return r; }).catch((e) => { T.toastError(e); throw e; });
    switch (name) {
      case 'trim-cat': {
        const cats = Array.from(root.querySelectorAll('[data-trimcat]')).filter((x) => x.checked).map((x) => x.dataset.trimcat);
        run(T.api.patch('/api/trimarr/raw/settings', { categories: cats }), 'Trimarr now cuts: ' + (cats.map((c) => TRIM_CATS[c] || c).join(', ') || 'nothing')).then((s) => { if (s && s.categories) trimSet = s; }).catch(() => {});
        return;
      }
      case 'imp-sample': { const ta = root.querySelector('[data-slot="paste"]'); ta.value = T.mock.sampleImportText(); ta.dispatchEvent(new Event('input')); return; }
      case 'imp-preview': {
        const ta = root.querySelector('[data-slot="paste"]');
        el.disabled = true;
        run(T.api.post('/api/import/preview', { text: ta.value })).then((p) => { imp = p; T.setHTML(root.querySelector('[data-slot="diff"]'), diffView(p)); root.querySelector('[data-slot="diff"]').scrollIntoView({ behavior: T.reducedMotion() ? 'auto' : 'smooth', block: 'nearest' }); }).catch(() => {}).finally(() => { el.disabled = false; });
        return;
      }
      case 'imp-apply': {
        const add = Array.from(root.querySelectorAll('[data-add]')).filter((x) => x.checked).map((x) => x.dataset.add);
        const rm = Array.from(root.querySelectorAll('[data-rm]')).filter((x) => x.checked).map((x) => x.dataset.rm);
        run(T.api.post('/api/import/apply', { import_id: imp.import_id, add, remove: rm }), (r) => 'Added ' + T.plural(r.added, 'channel') + (r.flagged_for_removal ? '; ' + r.flagged_for_removal + ' will be removed in 3 days' : '') + '. They’re looked up in the background, a few seconds apart.')
          .then(() => { T.app.invalidateChannels(); V.act('imp-reset'); }).catch(() => {});
        return;
      }
      case 'imp-reset': imp = null; root.querySelector('[data-slot="paste"]').value = ''; T.setHTML(root.querySelector('[data-slot="diff"]'), ''); root.querySelector('[data-slot="paste"]').dispatchEvent(new Event('input')); return;
      case 'plex-test': run(T.api.post('/api/plex/test'), (p) => p.state === 'connected' ? 'Plex answered.' : 'Plex didn’t answer.').then((p) => { plex = p; renderPlex(); }).catch(() => {}); return;
      case 'plex-scan': run(T.api.post('/api/plex/scan'), (r) => r.message || 'Scanning.').then(() => T.api.get('/api/plex')).then((p) => { plex = p; renderPlex(); }).catch(() => {}); return;
      case 'plex-repolish': run(T.api.post('/api/plex/repolish'), (r) => r.message || 'Re-polishing.').catch(() => {}); return;
      case 'plex-clear':
        T.confirm({ title: 'Disconnect Plex?', danger: true, confirm: 'Disconnect', body: html`<p>Tubarr forgets the Plex server and its sign-in. Downloads carry on and every video keeps its NFO files and artwork; Plex just stops being told about new ones. You can connect again any time.</p>` })
          .then((ok) => {
            if (!ok) return;
            run(T.api.post('/api/setup/plex/clear'), 'Plex disconnected.').then((p) => { setupInfo = Object.assign({}, setupInfo || {}, { plex: p }); plex = null; renderPlex(); }).catch(() => {});
          });
        return;
      case 'net-reload': T.api.get('/api/network').then(setNet).catch(T.toastError); return;
      case 'net-mode': {
        const mode = el.dataset.mode;
        if (!net || mode === net.mode) return;
        const proxies = net.proxies || [];
        if (mode !== 'direct' && !proxies.length) { T.toast('Add a proxy under Lines first.', { type: 'info' }); const i = root.querySelector('#s-np-name'); if (i) i.focus(); return; }
        const body = { mode };
        if (mode === 'single') body.selected = net.selected && proxies.some((p) => p.id === net.selected) ? net.selected : proxies[0].id;
        patchNet(body, mode === 'direct' ? 'YouTube traffic goes direct.' : mode === 'single' ? 'YouTube traffic uses ' + lineName(body.selected) + '.' : mode === 'failover' ? 'Failover is on.' : 'Lines take turns now.');
        return;
      }
      case 'net-up': case 'net-down': {
        const proxies = net.proxies || [];
        const order = (net.order || ['direct']).filter((id) => id === 'direct' || proxies.some((p) => p.id === id));
        const i = Number(el.dataset.i), j = name === 'net-up' ? i - 1 : i + 1;
        if (j < 0 || j >= order.length) return;
        [order[i], order[j]] = [order[j], order[i]];
        patchNet({ order });
        return;
      }
      case 'net-test': {
        const id = el.dataset.id;
        netTests[id] = { busy: true }; renderNet();
        T.api.post('/api/network/proxies/' + encodeURIComponent(id) + '/test').then((r) => { netTests[id] = { ip: r.ip, latency_ms: r.latency_ms }; })
          .catch((e) => { netTests[id] = { error: e.message || 'The test failed.' }; }).finally(renderNet);
        return;
      }
      case 'net-edit': netEdit = el.dataset.id; renderNet(); { const i = root.querySelector('#s-pe-name'); if (i) i.focus(); } return;
      case 'net-edit-cancel': netEdit = null; renderNet(); return;
      case 'net-edit-save': {
        const id = el.dataset.id, nm = root.querySelector('#s-pe-name').value.trim(), url = root.querySelector('#s-pe-url').value.trim();
        if (!nm) { T.toast('Give the proxy a name.', { type: 'error' }); return; }
        if (url && !URL_RE.test(url)) { T.toast(URL_HINT, { type: 'error' }); return; }
        const body = { name: nm }; if (url) body.url = url;
        el.disabled = true;
        run(T.api.patch('/api/network/proxies/' + encodeURIComponent(id), body), 'Saved ' + nm + '.').then((n) => { netEdit = null; delete netTests[id]; setNet(n); }).catch(() => { el.disabled = false; });
        return;
      }
      case 'net-del': {
        const id = el.dataset.id, nm = lineName(id);
        T.confirm({ title: 'Delete ' + nm + '?', danger: true, confirm: 'Delete', body: html`<p>Tubarr forgets this proxy and its saved address.${net && net.status && net.status.active === id ? ' It’s in use right now, so traffic moves to the next line.' : ''}</p>` })
          .then((ok) => { if (ok) run(T.api.del('/api/network/proxies/' + encodeURIComponent(id)), 'Deleted ' + nm + '.').then((n) => { delete netTests[id]; setNet(n); }).catch(() => {}); });
        return;
      }
      case 'net-add': {
        const ni = root.querySelector('#s-np-name'), ui2 = root.querySelector('#s-np-url');
        const nm = ni.value.trim(), url = ui2.value.trim();
        if (!nm) { T.toast('Give the proxy a name, like “Backup line”.', { type: 'error' }); ni.focus(); return; }
        if (!URL_RE.test(url)) { T.toast(URL_HINT, { type: 'error' }); ui2.focus(); return; }
        el.disabled = true;
        run(T.api.post('/api/network/proxies', { name: nm, url }), 'Added ' + nm + '. Press Test to check it.').then(setNet).catch(() => { el.disabled = false; });
        return;
      }
      case 'key-gen': {
        const go = () => { el.disabled = true; run(T.api.post('/api/auth/apikey')).then((r) => { apikey = { exists: true, created_at: r.created_at, hint: r.hint }; newKey = r.api_key; renderKey(); const f = root.querySelector('#s-newkey'); if (f) { f.focus(); f.select(); } }).catch(() => { el.disabled = false; }); };
        if (!apikey || !apikey.exists) { go(); return; }
        T.confirm({ title: 'Make a new API key?', danger: true, confirm: 'Regenerate', body: html`<p>The current key stops working at once. Anything that uses it needs the new one.</p>` }).then((ok) => { if (ok) go(); });
        return;
      }
      case 'key-revoke':
        T.confirm({ title: 'Revoke the API key?', danger: true, confirm: 'Revoke', body: html`<p>Scripts using it stop working at once. You can make a new one any time.</p>` })
          .then((ok) => { if (ok) run(T.api.del('/api/auth/apikey'), 'API key revoked.').then(() => { apikey = { exists: false, created_at: null, hint: null }; newKey = null; renderKey(); }).catch(() => {}); });
        return;
      case 'audit-refresh':
        auditBusy = true; renderAudit();
        T.api.get('/api/audit?limit=100').then((a) => { audit = a; }).catch(T.toastError).finally(() => { auditBusy = false; renderAudit(); });
        return;
      case 'key-copy': { const i = root.querySelector('#s-newkey'); if (i) copyText(i); return; }
      case 'hook-show': { const i = root.querySelector('#s-hook'); const show = i.type === 'password'; i.type = show ? 'text' : 'password'; T.setHTML(el, icon(show ? 'eye-off' : 'eye')); el.setAttribute('aria-label', show ? 'Hide the webhook URL' : 'Show the webhook URL'); return; }
      case 'trim-report': {
        el.disabled = true;
        T.trimarr.report().then((r) => {
          T.setHTML(root.querySelector('[data-slot="trim-report"]'), html`<div class="note note-info">${icon('scissors')}<span>Trimarr would trim <b>${T.plural(r.videos, 'video')}</b> and remove about <b>${T.fmtSpan(r.seconds)}</b> of sponsor reads${r.channels ? html` across ${T.plural(r.channels, 'channel')}` : ''}${r.bytes_saved ? html`, saving ${T.fmtBytes(r.bytes_saved)}` : ''}. This was a dry run: nothing changed.</span></div>`);
        }).catch(T.toastError).finally(() => { el.disabled = false; });
        return;
      }
      case 'hook-clear': draft.notifications.discord_webhook_url = ''; T.setHTML(root.querySelector('#set-notifications .set-card'), notifForm()); bindNotif(); renderSaveBar(); return;
      case 'hook-test': run(T.api.post('/api/notifications/test', { discord_webhook_url: draft.notifications.discord_webhook_url }), 'Test message sent. Check Discord.').catch(() => {}); return;
      case 'discard': {
        draft = clone(settings);
        const dl = root.querySelector('#set-downloads .set-card'), nt = root.querySelector('#set-notifications .set-card');
        T.setHTML(dl, downloadsForm()); T.setHTML(nt, notifForm());
        bindForms(); renderSaveBar();
        return;
      }
      case 'save': {
        const d = dirtyParts();
        el.disabled = true;
        run(T.api.patch('/api/settings', d), 'Saved.').then((s) => { settings = s; draft = clone(s); if (d.notifications) { T.setHTML(root.querySelector('#set-notifications .set-card'), notifForm()); bindNotif(); } renderSaveBar(); if (tgtCtl) tgtCtl.set(s.downloads.fill_target_bytes); }).catch(() => { el.disabled = false; });
        return;
      }
      default: return false;
    }
  };
})();
