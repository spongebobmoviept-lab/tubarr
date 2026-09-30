"""Tubarr web app: serves the UI in ../web and implements docs/API.md on top of the worker's REAL data.

Read-only by design:
  * the worker's SQLite DB is opened with `mode=ro` (SQLite refuses writes; the web app never calls db.connect(),
    which migrates);
  * /data/status.json is only read;
  * anything that would change state goes through tubarr/actions.py (and auth.py, net.py, setupflow.py);
  * sign-in, CSRF and the read-only API key are enforced in webapp/security.py.

Run:  python -m tubarr.webapp            (port TUBARR_WEB_PORT, default 9194)
"""
VERSION = "0.1.0"
