"""python -m tubarr.webapp  ->  the web UI + API on TUBARR_WEB_PORT (default 9194), all interfaces of the container.

Put it behind a reverse proxy with HTTPS if it's reachable from anywhere but your own network (see README)."""
import logging
import os

import uvicorn

from .. import redact


def main():
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(name)s %(levelname)s %(message)s")
    for noisy in ("urllib3", "websockets"):
        logging.getLogger(noisy).setLevel(logging.WARNING)
    redact.install()
    from .app import app
    from .security import trusted_proxies
    # X-Forwarded-For / X-Forwarded-Proto are only believed from the reverse proxies in TUBARR_TRUSTED_PROXIES
    # (e.g. "172.18.0.5" or "10.0.0.0/24"). Unset = trust nobody: the client address is the TCP peer.
    trusted = trusted_proxies()
    uvicorn.run(app, host=os.environ.get("TUBARR_WEB_HOST", "0.0.0.0"), port=int(os.environ.get("TUBARR_WEB_PORT", "9194")),
                log_level="warning", log_config=None, access_log=False, proxy_headers=bool(trusted),
                forwarded_allow_ips=",".join(trusted) if trusted else None, server_header=False,
                timeout_graceful_shutdown=3)


if __name__ == "__main__":
    main()
