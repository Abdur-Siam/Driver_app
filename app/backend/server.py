"""App factory + entrypoint for the standalone Driver App.

Serves the JSON API (/api/driver/v1/*), the PWA frontend (/), and POD
media (/media/*). Run locally:

    cd Driver/app && python -m backend.server        # http://127.0.0.1:5179
"""
from __future__ import annotations

import os

from flask import Flask, jsonify, request, send_from_directory

from . import auth, config, storage
from .api import api
from .db import init_db
from .ops_api import ops_api
from .seed import seed_if_empty

# Don't disclose the WSGI server software/version in the Server header (the dev
# server adds it below Flask's after_request layer). Prod runs behind gunicorn /
# a reverse proxy, which set their own — this only sanitises local runs.
try:
    from werkzeug.serving import WSGIRequestHandler
    WSGIRequestHandler.server_version = "TOM-Driver"
    WSGIRequestHandler.sys_version = ""
except Exception:
    pass


def create_app() -> Flask:
    # Production guardrails — fail the boot, not the first driver.
    if config.IS_PRODUCTION:
        if not os.environ.get("DRIVER_APP_SECRET", "").strip():
            raise RuntimeError(
                "DRIVER_APP_ENV=production requires DRIVER_APP_SECRET to be set "
                "(signed-media key must be explicit and shared across restarts/instances)")
        if os.environ.get("DRIVER_APP_SEED_DEMO", "") == "1":
            raise RuntimeError(
                "DRIVER_APP_SEED_DEMO=1 is refused in production — demo credentials "
                "(DRV001/test1234) must never exist on a commercial deploy. "
                "Provision real drivers with tools/provision_driver.py")

    app = Flask(__name__, static_folder=None)
    # Behind a reverse proxy (Azure App Service = 1 hop) trust X-Forwarded-*
    # for that many hops so remote_addr/scheme are the real client's. Never
    # on by default: a directly-exposed app must ignore forgeable headers.
    if config.TRUST_PROXY > 0:
        from werkzeug.middleware.proxy_fix import ProxyFix
        app.wsgi_app = ProxyFix(app.wsgi_app, x_for=config.TRUST_PROXY,
                                x_proto=config.TRUST_PROXY, x_host=config.TRUST_PROXY)
    # Cap request bodies (POD photos are base64 → inflate ~33%): DoS guard.
    app.config["MAX_CONTENT_LENGTH"] = config.MAX_UPLOAD_MB * 1024 * 1024
    app.register_blueprint(api)
    app.register_blueprint(ops_api)   # /api/ops/v1 — dispatch console

    init_db()
    if config.SEED_DEMO:
        if seed_if_empty():
            print("[driver-app] seeded demo drivers + jobs", flush=True)

    # Deliver any pushes queued before FCM credentials were configured.
    from . import push
    if push.configured():
        flushed = push.flush_pending()
        if flushed["sent"] or flushed["failed"]:
            print(f"[driver-app] push outbox flushed: {flushed}", flush=True)

    # ── frontend (PWA) ───────────────────────────────────────────────
    @app.route("/")
    def index():
        return send_from_directory(config.FRONTEND_DIR, "index.html")

    # ── ops / dispatch console (desktop web app) ─────────────────────
    @app.route("/ops")
    @app.route("/ops/")
    def ops_console():
        return send_from_directory(config.FRONTEND_DIR, "ops.html")

    @app.route("/<path:path>")
    def static_files(path):
        full = os.path.join(config.FRONTEND_DIR, path)
        if os.path.isfile(full):
            return send_from_directory(config.FRONTEND_DIR, path)
        # SPA fallback
        return send_from_directory(config.FRONTEND_DIR, "index.html")

    @app.route("/media/<path:name>")
    def media(name):
        # POD/receipt media is personal data — require a valid driver-scoped
        # signed URL (?exp=&did=&sig=) or an authenticated bearer token.
        exp, did, sig = request.args.get("exp"), request.args.get("did", ""), request.args.get("sig")
        authd = storage.verify_media_token(name, exp, did, sig)
        if not authd:
            a = request.headers.get("Authorization", "") or ""
            tok = a[7:].strip() if a.startswith("Bearer ") else ""
            authd = bool(tok and auth.resolve_token(tok))
        if not authd:
            return jsonify({"error": {"code": "forbidden", "message": "Media access denied"}}), 403
        resp = send_from_directory(config.MEDIA_DIR, name)
        resp.headers["Cache-Control"] = "no-store, private"
        return resp

    # Content Security Policy. 'unsafe-inline' is required for now because the
    # UI uses inline event handlers/styles; tightening to nonces (and removing
    # inline handlers) is the documented follow-up. Maps domains are added only
    # when a browser key is configured.
    def _csp():
        script = ["'self'", "'unsafe-inline'"]
        img = ["'self'", "data:", "blob:"]
        connect = ["'self'"]
        if config.GOOGLE_MAPS_BROWSER_KEY:
            script.append("https://maps.googleapis.com")
            img += ["https://maps.googleapis.com", "https://maps.gstatic.com", "https://*.googleusercontent.com"]
            connect.append("https://maps.googleapis.com")
        return "; ".join([
            "default-src 'self'",
            "base-uri 'self'",
            "object-src 'none'",
            "frame-ancestors 'none'",
            "form-action 'self'",
            "img-src " + " ".join(img),
            "script-src " + " ".join(script),
            "style-src 'self' 'unsafe-inline'",
            "connect-src " + " ".join(connect),
            "worker-src 'self'",
            "manifest-src 'self'",
        ])

    # CORS for the native app's WebView (capacitor://localhost etc.) — the API
    # is token-authenticated (no cookies), so allowlisted-origin CORS without
    # credentials is safe. Same-origin PWA requests carry no Origin mismatch
    # and are untouched.
    _CORS_HEADERS = "Content-Type, Authorization, X-Idempotency-Key"

    @app.before_request
    def cors_preflight():
        if request.method == "OPTIONS" and request.path.startswith("/api/"):
            origin = request.headers.get("Origin", "")
            if origin in config.CORS_ORIGINS:
                resp = app.make_response(("", 204))
                resp.headers["Access-Control-Allow-Origin"] = origin
                resp.headers["Access-Control-Allow-Methods"] = "GET, POST, PATCH, OPTIONS"
                resp.headers["Access-Control-Allow-Headers"] = _CORS_HEADERS
                resp.headers["Access-Control-Max-Age"] = "600"
                resp.headers["Vary"] = "Origin"
                return resp
        return None

    # Security + cache headers (PWA-friendly, no secret leakage).
    @app.after_request
    def headers(resp):
        origin = request.headers.get("Origin", "")
        if origin in config.CORS_ORIGINS and (request.path.startswith("/api/") or request.path.startswith("/media/")):
            resp.headers["Access-Control-Allow-Origin"] = origin
            resp.headers.add("Vary", "Origin")
        resp.headers.setdefault("X-Content-Type-Options", "nosniff")
        resp.headers.setdefault("Referrer-Policy", "no-referrer")
        resp.headers.setdefault("X-Frame-Options", "DENY")
        resp.headers.setdefault("Content-Security-Policy", _csp())
        resp.headers.setdefault("Permissions-Policy",
                                "geolocation=(self), camera=(self), microphone=(), payment=(), usb=(), interest-cohort=()")
        resp.headers.setdefault("Cross-Origin-Opener-Policy", "same-origin")
        if config.SECURITY_HSTS:
            resp.headers.setdefault("Strict-Transport-Security", "max-age=31536000; includeSubDomains")
        resp.headers.pop("Server", None)   # don't disclose the server/version banner
        return resp

    @app.errorhandler(413)
    def too_large(e):
        return jsonify({"error": {"code": "payload_too_large",
                                  "message": f"Upload exceeds {config.MAX_UPLOAD_MB}MB"}}), 413

    @app.errorhandler(404)
    def not_found(e):
        if request.path.startswith("/api/"):
            return jsonify({"error": {"code": "not_found", "message": "Not found"}}), 404
        return send_from_directory(config.FRONTEND_DIR, "index.html")

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("DRIVER_APP_PORT", "5179"))
    app.run(host="127.0.0.1", port=port, debug=False)
