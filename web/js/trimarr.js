/* Tubarr web UI: the optional Trimarr add-on (trims sponsor segments out of videos already in Plex).
   Every call goes through Tubarr's same-origin proxy, /api/trimarr/…, described in API.md ("Trimarr").
   This file is the only place that knows Trimarr's routes: when trimarr/API.md is final, adjust them here.
   If Trimarr isn't installed or reachable, `available` is false and the UI hides or disables its controls. */
(function () {
  'use strict';
  const T = window.T;
  const tr = (T.trimarr = { status: { available: false, enabled: false }, loaded: false });

  tr.load = async function () {
    try {
      const s = await T.api.get('/api/trimarr/status');
      tr.status = Object.assign({ available: true }, s);
    } catch (e) {
      tr.status = { available: false, enabled: false, reason: e.status === 404 ? 'not_installed' : 'unreachable', message: e.message };
    }
    tr.loaded = true;
    T.emit('trimarr', tr.status);
    return tr.status;
  };
  tr.available = () => !!(tr.status && tr.status.available);
  tr.setEnabled = async function (on) {
    const s = await T.api.patch('/api/trimarr/settings', { enabled: !!on });
    tr.status = Object.assign({ available: true }, s);
    T.emit('trimarr', tr.status);
    return tr.status;
  };
  tr.channel = (id) => T.api.get('/api/trimarr/channels/' + encodeURIComponent(id));
  tr.setChannel = (id, on) => T.api.patch('/api/trimarr/channels/' + encodeURIComponent(id), { enabled: !!on });
  tr.trim = (videoId) => T.api.post('/api/trimarr/videos/' + encodeURIComponent(videoId) + '/trim');
  tr.undo = (videoId) => T.api.post('/api/trimarr/videos/' + encodeURIComponent(videoId) + '/undo');
  tr.report = (channelId) => T.api.get('/api/trimarr/report' + (channelId ? '?channel=' + encodeURIComponent(channelId) : ''));
})();
