/* Tubarr web UI: API client, live-vs-mock decision, sign-in state, CSRF and status polling. See docs/API.md. */
(function () {
  'use strict';
  const T = window.T;
  const Q = new URLSearchParams(location.search);

  class ApiError extends Error {
    constructor(status, code, message, data) { super(message); this.status = status; this.code = code; this.data = data || null; }
  }
  T.ApiError = ApiError;

  /* Routes that answer 401 as part of their normal job (a wrong password), so a 401 there is not "signed out". */
  const AUTH_OPEN = /^\/api\/auth\/(login|setup|state)$/;

  function cookie(name) {
    const m = document.cookie.match(new RegExp('(?:^|;\\s*)' + name + '=([^;]*)'));
    return m ? decodeURIComponent(m[1]) : null;
  }

  const api = (T.api = {
    mode: 'unknown',   // 'live' | 'mock'
    reason: '',
    online: true,
    /* The last GET /api/auth/state answer. In mock mode: always signed in, setup done. */
    auth: null,

    /* Decide once at start-up. A server that answers (even 401 "Please sign in") is always live: the sign-in screen
       shows, never mock data, even with ?mock=1 in the address. Mock data only when the page was opened from disk,
       or when nothing answers at all (a plain static file server during development), and only if the mock scripts
       are there (they are not shipped in the Docker image): otherwise the normal offline banner shows.
       ?mock=0 turns the fallback off. Never switch to mock later: a dropped connection shows a banner instead. */
    async detect() {
      if (location.protocol === 'file:' && Q.get('mock') !== '0') return this.useMock('opened from disk');
      const probe = async (path) => {
        const ctl = new AbortController();
        const timer = setTimeout(() => ctl.abort(), 2500);
        try { return await fetch(path, { signal: ctl.signal, headers: { Accept: 'application/json' }, cache: 'no-store', credentials: 'same-origin' }); }
        finally { clearTimeout(timer); }
      };
      try {
        const r = await probe('/health');
        let ok = false;
        if (r.ok) { try { const j = await r.json(); ok = !!(j && j.ok); } catch (e) { ok = false; } }
        if (!ok) {
          // no /health (an older or proxied server): the auth route answers JSON either way
          const a = await probe('/api/auth/state');
          let j = null; try { j = await a.json(); } catch (e) { /* not Tubarr */ }
          if (!j || (!('authenticated' in j) && !(j.error && j.error.code))) throw new Error('HTTP ' + r.status);
        }
        this.mode = 'live';
        return this.mode;
      } catch (e) {
        if (Q.get('mock') === '0') return this.offline();
        return this.useMock("the API didn't answer (" + (e.name === 'AbortError' ? 'timed out' : e.message) + ')');
      }
    },
    async useMock(reason) {
      if (!T.mock) { try { await loadMockScripts(); } catch (e) { /* not shipped here */ } }
      if (!T.mock || !T.mock.handle) return this.offline();
      this.mode = 'mock';
      this.reason = reason;
      return this.mode;
    },
    /* Live, but nothing answers yet: the offline banner shows until the server is back. */
    offline() { this.mode = 'live'; this.online = false; return this.mode; },

    /* GET /api/auth/state: who is signed in, and the CSRF token for this session. */
    async authState() {
      const s = await this.req('GET', '/api/auth/state');
      this.auth = s;
      return s;
    },
    csrf() { return cookie('__Host-tubarr_csrf') || cookie('tubarr_csrf') || (this.auth && this.auth.csrf) || ''; },

    async req(method, path, body, retried) {
      if (this.mode === 'mock') {
        await T.sleep(60 + Math.random() * 140);
        const res = T.mock.handle(method, path, body);
        if (res.status >= 400) throw new ApiError(res.status, res.body.error.code, res.body.error.message, res.body.error);
        return res.body;
      }
      const headers = { Accept: 'application/json' };
      if (body !== undefined) headers['Content-Type'] = 'application/json';
      if (method !== 'GET') { const c = this.csrf(); if (c) headers['X-CSRF-Token'] = c; }
      let r;
      try {
        r = await fetch(path, { method, headers, body: body !== undefined ? JSON.stringify(body) : undefined, cache: 'no-store', credentials: 'same-origin' });
      } catch (e) {
        setOnline(false);
        throw new ApiError(0, 'offline', "Can't reach Tubarr. Is the server running?");
      }
      setOnline(true);
      let j = null;
      try { j = await r.json(); } catch (e) { /* empty or non-JSON body */ }
      if (!r.ok) {
        const err = (j && j.error) || {};
        // a stale CSRF token (e.g. after a password change in another tab): fetch a fresh one and try once more
        if (r.status === 403 && err.code === 'csrf' && !retried) {
          try { await this.authState(); } catch (e) { /* fall through to the error below */ }
          return this.req(method, path, body, true);
        }
        if (r.status === 401 && !AUTH_OPEN.test(path)) signedOut();
        throw new ApiError(r.status, err.code || 'internal', err.message || 'Tubarr answered with an error (HTTP ' + r.status + ').', err);
      }
      return j;
    },
    get(p) { return this.req('GET', p); },
    post(p, b) { return this.req('POST', p, b === undefined ? {} : b); },
    patch(p, b) { return this.req('PATCH', p, b); },
    del(p, b) { return this.req('DELETE', p, b); },
  });

  /* Any 401 from the API means the session ended (expired, signed out elsewhere, password changed): live updates
     stop and the shell (app.js) shows the sign-in screen. Sent once until the next sign-in. */
  let signedOutSent = false;
  function signedOut() {
    if (signedOutSent) return;
    signedOutSent = true;
    if (api.auth) api.auth = Object.assign({}, api.auth, { authenticated: false, csrf: null });
    live.stop();
    T.emit('unauthorized');
  }
  api.signedIn = function (state) { signedOutSent = false; if (state) api.auth = state; };
  api.signedOut = signedOut;

  function setOnline(v) {
    if (api.online === v) return;
    api.online = v;
    T.emit('connection', v);
  }

  /* Mock files are only fetched when needed (classic <script> tags work from file:// too). */
  function loadScript(src) {
    return new Promise((resolve, reject) => {
      const s = document.createElement('script');
      s.src = src; s.onload = resolve; s.onerror = () => reject(new Error('could not load ' + src));
      document.head.appendChild(s);
    });
  }
  async function loadMockScripts() {
    await loadScript('mock/art.js').catch(() => {});
    await loadScript('mock/seed.js');
    await loadScript('mock/mock-data.js');
    await loadScript('mock/mock-server.js');
  }

  /* File uploads (posters, thumbnails): multipart in live mode; the mock gets a data URL instead. */
  api.upload = async function (path, file, retried) {
    if (file.size > 10 * 1048576) throw new ApiError(413, 'too_large', 'That image is over 10 MB.');
    if (this.mode === 'mock') {
      const url = await new Promise((resolve, reject) => { const fr = new FileReader(); fr.onload = () => resolve(fr.result); fr.onerror = () => reject(fr.error); fr.readAsDataURL(file); });
      return this.req('POST', path, { file_data_url: url });
    }
    const fd = new FormData();
    fd.append('file', file, file.name);
    const headers = {}; const c = this.csrf(); if (c) headers['X-CSRF-Token'] = c;
    let r;
    try { r = await fetch(path, { method: 'POST', body: fd, headers, credentials: 'same-origin' }); } catch (e) { setOnline(false); throw new ApiError(0, 'offline', "Can't reach Tubarr. Is the server running?"); }
    let j = null; try { j = await r.json(); } catch (e) { /* ignore */ }
    if (!r.ok) {
      const err = (j && j.error) || {};
      if (r.status === 403 && err.code === 'csrf' && !retried) { try { await this.authState(); } catch (e) { /* ignore */ } return this.upload(path, file, true); }
      if (r.status === 401) signedOut();
      throw new ApiError(r.status, err.code || 'internal', err.message || 'Upload failed (HTTP ' + r.status + ').', err);
    }
    return j;
  };

  /* ---------- Live updates ----------
     Primary path: the server-sent event stream GET /api/events (the mock has an in-page equivalent).
     Every message is re-emitted on the bus as 'ev' and 'ev:<type>'; 'status' messages also update T.live.status.
     While the stream is down, GET /api/status is polled instead so the top bar never goes stale.
     The stream is reconnected by hand (1 s, 2 s, 5 s, then every 5 s), because a browser's own EventSource retry
     gives up for good on some failures (a web-app restart answering 502 once is enough). An EventSource can't see
     the HTTP status, so before every reconnect GET /api/auth/state is asked whether the session is still signed in:
     if not, everything stops and the sign-in screen shows (no reconnect loop against a 401). After every reconnect
     the status and the open view are fetched again. A stream that goes quiet for 30 s (the server pings every 15 s)
     is treated as dead: status is polled every 15 s and the stream is reopened. When the server's build stamp
     changes (new code deployed), the page reloads itself once. */
  const live = (T.live = { status: null, timer: null, fails: 0, streaming: false, es: null, lastEventAt: 0, build: null, stopped: false, unsub: null, safety: null, retryT: null });
  function dispatch(msg) {
    if (msg.data && (msg.event === 'status' || msg.event === 'hello')) checkBuild(msg.data.build);
    if (msg.event === 'status') { live.status = msg.data; T.emit('status', msg.data); }
    if (msg.event === 'ping' || msg.event === 'hello') return;
    T.emit('ev', msg);
    T.emit('ev:' + msg.event, msg.data);
  }
  /* ---- reload once when new code is deployed ---- */
  let reloadT = null;
  function checkBuild(b) {
    if (!b || api.mode !== 'live') return;
    if (!live.build) { live.build = b; return; }
    clearTimeout(reloadT);
    if (b === live.build) return;
    reloadT = setTimeout(tryReload, 4000);     // a deploy pushes files one by one: wait until it settles
  }
  function tryReload() {
    // never throw away something the user is in the middle of (an open dialog, unsaved settings, the setup wizard)
    if (document.querySelector('.modal-wrap') || document.querySelector('.savebar.open') || document.querySelector('.page-setup')) { reloadT = setTimeout(tryReload, 5000); return; }
    let last = 0;
    try { last = Number(sessionStorage.getItem('tubarr.reloadedAt')) || 0; } catch (e) { /* storage blocked */ }
    const wait = 20000 - (Date.now() - last);
    if (wait > 0) { reloadT = setTimeout(tryReload, wait); return; }
    try { sessionStorage.setItem('tubarr.reloadedAt', String(Date.now())); } catch (e) { /* storage blocked */ }
    location.reload();
  }
  /* ---- the "Reconnecting…" pill in the top bar ---- */
  function setReconnecting(v) {
    if (live.reconnecting === v) return;
    live.reconnecting = v;
    T.emit('reconnecting', v);
  }
  async function fetchStatus() {
    const s = await api.get('/api/status');
    live.fails = 0;
    setOnline(true);
    dispatch({ event: 'status', data: s });
  }
  live.poll = async function () {
    clearTimeout(live.timer);
    if (live.streaming || live.stopped) return;
    try { await fetchStatus(); } catch (e) {
      live.fails++;
      if (e.code !== 'offline' && e.status !== 401) console.warn('status poll failed', e);
    }
    if (live.streaming || live.stopped) return;
    const base = document.visibilityState === 'hidden' ? 15000 : 2000;
    live.timer = setTimeout(live.poll, live.fails ? Math.min(15000, base * (1 + live.fails)) : base);
  };
  /* Fetch status and the open view again (after a reconnect, or when the tab comes back). */
  live.resync = function () {
    if (live.stopped) return;
    fetchStatus().catch(() => {});
    T.emit('stream-reconnected');
  };
  /* Still signed in? Only asked when the stream dropped (live mode). A network failure counts as "yes, retry". */
  async function stillSignedIn() {
    try { const s = await api.authState(); if (!s.authenticated) { signedOut(); return false; } return true; }
    catch (e) { return e.status !== 401; }
  }
  /* Stop everything: signed out. live.start() starts it again after the next sign-in. */
  live.stop = function () {
    live.stopped = true;
    clearTimeout(live.timer); clearTimeout(live.retryT); clearInterval(live.safety);
    if (live.es) { try { live.es.close(); } catch (e) { /* already closed */ } live.es = null; }
    if (live.unsub) { live.unsub(); live.unsub = null; }
    live.streaming = false;
    setReconnecting(false);
  };
  const BACKOFF = [1000, 2000, 5000];
  live.start = function () {
    live.stopped = false;
    if (api.mode === 'mock') {
      live.streaming = true;
      live.unsub = T.mock.subscribe(dispatch);
      return;
    }
    live.poll();
    if (!window.EventSource) return;
    let opened = false, tries = 0, lastSafety = 0;
    const types = ['hello', 'status', 'video.added', 'video.state', 'video.progress', 'video.removed', 'channel.added', 'channel.updated', 'channel.removed', 'storage', 'history', 'ping', 'resync'];
    const connect = () => {
      clearTimeout(live.retryT);
      if (live.stopped) return;
      if (live.es) { try { live.es.close(); } catch (e) { /* already closed */ } }
      const es = (live.es = new EventSource('/api/events', { withCredentials: true }));
      types.forEach((t) => es.addEventListener(t, (e) => {
        if (es !== live.es) return;
        live.lastEventAt = Date.now();
        // resync: too much changed at once (e.g. the worker re-queued thousands of rows): refetch, like after a reconnect
        if (t === 'resync') { T.emit('stream-reconnected'); return; }
        let data = null;
        try { data = JSON.parse(e.data); } catch (err) { return; }
        dispatch({ id: e.lastEventId, event: t, data });
      }));
      es.onopen = () => {
        if (es !== live.es) return;
        const again = opened;
        opened = true; tries = 0; live.lastEventAt = Date.now();
        live.streaming = true; clearTimeout(live.timer); setOnline(true); setReconnecting(false);
        if (again) live.resync();
      };
      es.onerror = async () => {
        if (es !== live.es) return;
        try { es.close(); } catch (e) { /* ignore */ }
        live.es = null;
        if (live.streaming) { live.streaming = false; live.poll(); }
        if (live.stopped) return;
        setReconnecting(true);
        // an EventSource can't tell a 401 from a network blip: ask before looping
        if (!(await stillSignedIn()) || live.stopped) return;
        live.retryT = setTimeout(connect, BACKOFF[Math.min(tries, BACKOFF.length - 1)]);
        tries++;
      };
    };
    live.reconnect = connect;
    connect();
    // safety net: a stream can die silently (no error event). Quiet for 30 s -> poll status every 15 s and reopen it.
    clearInterval(live.safety);
    live.safety = setInterval(() => {
      if (live.stopped || document.visibilityState === 'hidden' || !live.streaming) return;
      if (Date.now() - live.lastEventAt < 30000) return;
      if (Date.now() - lastSafety < 15000) return;
      lastSafety = Date.now();
      fetchStatus().catch(() => {});
      setReconnecting(true);
      connect();
    }, 1000);
  };
  // coming back to the tab: the ticker was paused, so fetch fresh numbers (tick.js emits 'resume')
  T.on('resume', () => { if (api.mode !== 'live' || live.stopped) return; if (live.streaming) live.resync(); else live.poll(); });

  /* A small helper for views that poll their own endpoint while they are on screen. */
  T.poller = function (fn, ms) {
    let t = null, stopped = false;
    const loop = async () => {
      if (stopped) return;
      try { await fn(); } catch (e) { /* the view shows its own error state */ }
      if (!stopped) t = setTimeout(loop, document.visibilityState === 'hidden' ? Math.max(ms, 15000) : ms);
    };
    t = setTimeout(loop, ms);
    return { stop() { stopped = true; clearTimeout(t); }, now() { clearTimeout(t); loop(); } };
  };
})();
