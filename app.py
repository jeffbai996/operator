"""Operator — standalone entrypoint.

Run the live browser / computer-use agent cockpit on its own:

    pip install -r requirements.txt
    python app.py            # then open http://127.0.0.1:5005/

It mounts the `operator` blueprint on a minimal Flask app and serves the UI at
`/` (redirects to `/operator`). Config via env (all optional):

    OPERATOR_HOST   bind host   (default 127.0.0.1)
    OPERATOR_PORT   bind port   (default 5005)
    OPERATOR_DEBUG       "1" for Flask debug/reload
    OPERATOR_AUTH_TOKEN  32+ character token required for proxy/network access
"""
import os

from flask import Flask, redirect, url_for

# flat layout: operator_view defines the blueprint (and imports operator_agent itself)
from operator_view import bp  # noqa: E402
from operator_standalone_auth import (  # noqa: E402
    configured_token,
    install_standalone_boundary,
    validate_listen_host,
)


def create_app() -> Flask:
    app = Flask(__name__, template_folder="templates", static_folder="static")
    install_standalone_boundary(
        app,
        configured_token(),
        demo=os.environ.get("OPERATOR_DEMO") == "1",
    )
    app.register_blueprint(bp)

    @app.route("/")
    def _home():
        return redirect(url_for("operator.operator_page"))

    return app


if __name__ == "__main__":
    host = os.environ.get("OPERATOR_HOST", "127.0.0.1")
    port = int(os.environ.get("OPERATOR_PORT", "5005"))
    debug = os.environ.get("OPERATOR_DEBUG", "") == "1"
    validate_listen_host(
        host,
        configured_token(),
        demo=os.environ.get("OPERATOR_DEMO") == "1",
    )
    create_app().run(host=host, port=port, debug=debug, threaded=True)
