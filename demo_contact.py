#!/usr/bin/env python3
"""Minimal public holding page for a disabled Operator demo.

This process intentionally imports no Operator modules and exposes no browser,
agent, state, filesystem, or control route.  It is the production-safe public
endpoint while the interactive demo is disabled.
"""
from __future__ import annotations

import os

from flask import Flask


PAGE = """<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1, viewport-fit=cover">
<title>Operator</title>
<style>
html,body{margin:0;height:100%}*{box-sizing:border-box}body{display:grid;place-items:center;
min-height:100dvh;background:#000;color:#e7e9ee;padding:2rem;font:400 16px/1.55
system-ui,-apple-system,"Segoe UI",sans-serif}.stack{display:grid;justify-items:center;text-align:center}
.mark{width:clamp(96px,22vw,150px);height:auto;transform-origin:center}.mark:hover{animation:greet
.78s cubic-bezier(.34,1.06,.44,1)}.wordmark{font-weight:700;font-size:clamp(2.2rem,8vw,3.4rem);
line-height:1;letter-spacing:-.03em;color:#fff;margin-top:1.6rem}.msg{color:#8b93a3;font-size:.95rem;
max-width:30rem;margin-top:1.5rem}.msg span{display:block}@keyframes greet{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.mark:hover{animation:none}}
</style>
</head>
<body><main class="stack">
<svg class="mark" viewBox="6.4 6.4 11.2 11.2" fill="none" aria-hidden="true">
<g stroke="#fff" stroke-width="1.25" stroke-linecap="round"><circle cx="12" cy="12" r="4.8"/>
<path d="M12.743 7.29A3.1 3.1 0 0 1 12 13.4a1.4 1.4 0 0 0 0-2.8"/>
<path d="M11.257 16.71A3.1 3.1 0 0 1 12 10.6a1.4 1.4 0 0 0 0 2.8"/></g></svg>
<div class="wordmark">Operator</div>
<p class="msg"><span>Operator demo is unavailable at this time.</span>
<span>Please contact the developer.</span></p>
</main></body></html>"""


def create_app() -> Flask:
    app = Flask(__name__)

    @app.after_request
    def _security_headers(response):
        response.headers["Cache-Control"] = "no-store, max-age=0"
        response.headers["Content-Security-Policy"] = (
            "default-src 'none'; style-src 'unsafe-inline'; "
            "img-src data:; base-uri 'none'; form-action 'none'; "
            "frame-ancestors 'none'"
        )
        response.headers["Cross-Origin-Resource-Policy"] = "same-origin"
        response.headers["Permissions-Policy"] = (
            "camera=(), microphone=(), geolocation=(), payment=(), usb=()"
        )
        response.headers["Referrer-Policy"] = "no-referrer"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["X-Frame-Options"] = "DENY"
        return response

    @app.route("/", defaults={"_path": ""})
    @app.route("/<path:_path>")
    def contact(_path: str):
        return PAGE, 200, {"Content-Type": "text/html; charset=utf-8"}

    return app


app = create_app()


if __name__ == "__main__":
    port = int(os.environ.get("OP_DEMO_PORT", "5050"))
    app.run(host="127.0.0.1", port=port, debug=False, threaded=True)
