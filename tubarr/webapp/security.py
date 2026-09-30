"""HTTP security for the web app: the sign-in gate, CSRF checks, the read-only API key, cookies, security headers.

Rules (enforced in guard(), the first middleware):
  * Open without sign-in: GET /health, the static UI files, GET /api/auth/state, POST /api/auth/login and
    POST /api/auth/setup (the latter two only with a JSON body, so a plain HTML form on another site can't post).
  * Everything else under /api/ needs either the session cookie or the X-Api-Key header (never a URL parameter).
  * Session requests that change state (anything but GET/HEAD/OPTIONS) must carry X-CSRF-Token matching the session.
  * The API key is read-only: GET/HEAD only, and never for SESSION_ONLY paths (account, audit log, API key itself,
    the YouTube lookup).
  * Request bodies are capped (BodyLimit, the outermost middleware): 64 KB for /api/auth/*, 5 MB for the import,
    1 MB for everything else; 413 otherwise. Chunked bodies are counted as they arrive.
  * Over HTTPS the cookies use the `__Host-` prefix (Secure, path=/, no Domain), so a sibling subdomain can't plant one.
  * X-Forwarded-For / -Proto are only honoured from TUBARR_TRUSTED_PROXIES (uvicorn's proxy_headers; __main__.py).
"""
import os

from fastapi.responses import JSONResponse

from .. import auth

SAFE = ("GET", "HEAD", "OPTIONS")
OPEN_API = {("GET", "/api/auth/state"), ("POST", "/api/auth/login"), ("POST", "/api/auth/setup")}
SESSION_ONLY_PREFIXES = ("/api/auth/", "/api/audit", "/api/lookup")
# Images: the app's own, plus YouTube's thumbnail / avatar / banner hosts (the lookup preview and not-yet-downloaded
# videos show YouTube's own images).
IMG_SRC = "'self' data: blob: https://i.ytimg.com https://*.ggpht.com https://*.googleusercontent.com"
CSP = ("default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src " + IMG_SRC + "; "
       "font-src 'self' data:; connect-src 'self'; frame-src 'none'; frame-ancestors 'none'; object-src 'none'; "
       "base-uri 'self'; form-action 'self'")
HEADERS = {"X-Content-Type-Options": "nosniff", "X-Frame-Options": "DENY", "Referrer-Policy": "same-origin",
           "Content-Security-Policy": CSP, "Cross-Origin-Opener-Policy": "same-origin-allow-popups",
           "Permissions-Policy": "camera=(), microphone=(), geolocation=()"}


def _err(status, code, message, **extra):
    body = {"error": dict({"code": code, "message": message}, **extra)}
    return JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})


def is_https(request):
    force = os.environ.get("TUBARR_COOKIE_SECURE", "auto").strip().lower()
    if force in ("1", "true", "yes"):
        return True
    if force in ("0", "false", "no"):
        return False
    return request.url.scheme == "https" or request.headers.get("x-forwarded-proto", "").lower() == "https"


def cookie_name(request, base):
    """`__Host-<name>` over HTTPS (Secure, path=/, no Domain: a sibling subdomain can't set or overwrite it)."""
    return "__Host-" + base if is_https(request) else base


def set_session_cookies(request, response, cookie, csrf):
    secure = is_https(request)
    age = int(auth.SESSION_DAYS * 86400)
    response.set_cookie(cookie_name(request, auth.SESSION_COOKIE), cookie, max_age=age, httponly=True, samesite="lax",
                        secure=secure, path="/")
    response.set_cookie(cookie_name(request, auth.CSRF_COOKIE), csrf, max_age=age, httponly=False, samesite="lax",
                        secure=secure, path="/")


def clear_session_cookies(response):
    for base in (auth.SESSION_COOKIE, auth.CSRF_COOKIE):
        response.delete_cookie(base, path="/")
        response.delete_cookie("__Host-" + base, path="/", secure=True)


def identify(request):
    """-> ("session", session dict) | ("api_key", None) | (None, None)."""
    sess = auth.read_session(request.cookies.get(cookie_name(request, auth.SESSION_COOKIE)))
    if sess:
        return "session", sess
    key = request.headers.get("x-api-key")
    if key and auth.check_api_key(key.strip()):
        return "api_key", None
    return None, None


async def guard(request, call_next):
    path, method = request.url.path, request.method.upper()
    request.state.via, request.state.session = None, None
    if path.startswith("/api/"):
        via, sess = identify(request)
        request.state.via, request.state.session = via, sess
        if (method, path) in OPEN_API:
            if method == "POST" and not request.headers.get("content-type", "").lower().startswith("application/json"):
                return _err(415, "bad_request", "Send JSON.")
        elif via is None:
            return _err(401, "unauthorized", "Please sign in.")
        elif via == "api_key":
            if method not in SAFE:
                return _err(403, "forbidden", "The API key is read-only.")
            if path.startswith(SESSION_ONLY_PREFIXES):
                return _err(403, "forbidden", "Sign in to the web UI for this.")
        elif method not in SAFE and not auth.csrf_ok(sess, request.headers.get("x-csrf-token")):
            return _err(403, "csrf", "The page's security token is missing or old. Reload the page.")
    resp = await call_next(request)
    for k, v in HEADERS.items():
        resp.headers.setdefault(k, v)
    if is_https(request):
        resp.headers.setdefault("Strict-Transport-Security", "max-age=15552000")
    return resp


def who(request):
    s = getattr(request.state, "session", None)
    return (s or {}).get("user") or ("api-key" if getattr(request.state, "via", None) == "api_key" else "anonymous")


def client_addr(request):
    # With TUBARR_TRUSTED_PROXIES set, uvicorn has already replaced this with the X-Forwarded-For client (only for
    # requests that came from one of those proxies); otherwise it's the TCP peer. Never read the header here.
    return request.client.host if request.client else "?"


def trusted_proxies():
    """TUBARR_TRUSTED_PROXIES: comma-separated reverse-proxy addresses/CIDRs, or '' (default: trust none)."""
    v = (os.environ.get("TUBARR_TRUSTED_PROXIES") or "").strip()
    return [p.strip() for p in v.split(",") if p.strip()]


# ------------------------------------------------------------------ request body cap (pure ASGI, outermost)
BODY_MAX_DEFAULT = 1024 * 1024
BODY_LIMITS = (("/api/auth/", 64 * 1024), ("/api/import/", 5 * 1024 * 1024))


def body_limit(path):
    return next((n for pre, n in BODY_LIMITS if path.startswith(pre)), BODY_MAX_DEFAULT)


class BodyLimit:
    """413 for bodies over body_limit(path). Content-Length is checked up front; bodies without one (chunked) are
    read and counted as they arrive, then replayed to the app, so nothing downstream ever sees an oversized body."""

    def __init__(self, app):
        self.app = app

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http" or scope.get("method", "GET").upper() in ("GET", "HEAD", "OPTIONS"):
            return await self.app(scope, receive, send)
        limit = body_limit(scope.get("path") or "")
        headers = dict(scope.get("headers") or [])
        cl = headers.get(b"content-length")
        if cl is not None:
            if not cl.strip().isdigit():
                return await _err(400, "bad_request", "Bad Content-Length.")(scope, receive, send)
            if int(cl) > limit:
                return await _err(413, "too_large", "That request is too big.")(scope, receive, send)
        chunks, total = [], 0
        while True:
            msg = await receive()
            if msg["type"] == "http.disconnect":
                return
            part = msg.get("body", b"")
            total += len(part)
            if total > limit:
                return await _err(413, "too_large", "That request is too big.")(scope, receive, send)
            chunks.append(part)
            if not msg.get("more_body"):
                break
        replay = [{"type": "http.request", "body": b"".join(chunks), "more_body": False}]

        async def again():
            return replay.pop() if replay else await receive()
        return await self.app(scope, again, send)
