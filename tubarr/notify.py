"""Discord notifications (Settings -> Notifications): a plain webhook post, nothing else.

Only real Discord webhook links are ever posted to (WEBHOOK_RE: https, discord.com / discordapp.com, the
/api/webhooks/<id>/<token> path), without redirects and ignoring environment proxy variables, so the setting (or the
test button) can't be used to make Tubarr fetch other addresses. The link is a secret (anyone holding it can post):
it's stored encrypted (settings.WEBHOOK_KEY in the vault) and scrubbed from logs.
"""
import logging
import re

import requests

from . import redact, settings

log = logging.getLogger("tubarr.notify")
WEBHOOK_RE = re.compile(r"^https://(?:ptb\.|canary\.)?(?:discord|discordapp)\.com/api/webhooks/\d{5,25}/[A-Za-z0-9_-]{20,200}/?$")


def valid(url):
    return isinstance(url, str) and bool(WEBHOOK_RE.match(url.strip()))


def post(url, text):
    url = (url or "").strip()
    if not valid(url):
        raise ValueError("not a Discord webhook link")
    redact.remember(url)
    with requests.Session() as s:
        s.trust_env = False
        r = s.post(url, json={"content": text[:1900], "username": "Tubarr"}, timeout=15, allow_redirects=False)
    if r.status_code >= 300:
        raise RuntimeError("Discord answered %d" % r.status_code)


def send(event, text):
    """Send if a webhook is set and this event is switched on. Returns True when sent."""
    n = settings.get("notifications")
    url = (n.get("discord_webhook_url") or "").strip()
    if not url or not (n.get("events") or {}).get(event, False):
        log.debug("notification %s not sent (off): %s", event, text[:120])
        return False
    post(url, text)
    log.info("notification %s sent", event)
    return True
