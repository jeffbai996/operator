"""Authentication and browser-origin boundary for the standalone Flask app."""
from __future__ import annotations

import base64
import binascii
import ipaddress
import os
import secrets
from urllib.parse import urlsplit

from flask import Flask, jsonify, request
from werkzeug.middleware.proxy_fix import ProxyFix


_TRANSIT_HEADERS = (
    "Forwarded",
    "X-Forwarded-For",
    "X-Forwarded-Host",
    "X-Forwarded-Proto",
)
_UNSAFE_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})


def configured_token() -> str:
    token = (os.environ.get("OPERATOR_AUTH_TOKEN") or "").strip()
    if token and len(token) < 32:
        raise RuntimeError("OPERATOR_AUTH_TOKEN must be at least 32 characters")
    return token


def _host_name(value: str) -> str | None:
    """Return a validated hostname from a Host header or listen address."""
    value = value.strip()
    if not value or "@" in value or "/" in value:
        return None
    try:
        # Listen addresses use bare IPv6; HTTP Host headers bracket it.
        return str(ipaddress.ip_address(value.split("%", 1)[0]))
    except ValueError:
        pass
    try:
        parsed = urlsplit("//" + value)
        # Accessing .port rejects malformed and out-of-range values.
        parsed.port
    except ValueError:
        return None
    return parsed.hostname


def _is_loopback_host(value: str) -> bool:
    host = _host_name(value)
    if not host:
        return False
    if host.lower() == "localhost":
        return True
    try:
        return ipaddress.ip_address(host.split("%", 1)[0]).is_loopback
    except ValueError:
        return False


def validate_listen_host(host: str, token: str, *, demo: bool = False) -> None:
    """Refuse network listeners that have neither auth nor demo isolation."""
    if not demo and not token and not _is_loopback_host(host):
        raise RuntimeError(
            "OPERATOR_AUTH_TOKEN is required when OPERATOR_HOST is not loopback"
        )


def _is_direct_loopback_request() -> bool:
    if not _is_loopback_host(request.host):
        return False
    if any(request.headers.get(name) is not None for name in _TRANSIT_HEADERS):
        return False
    try:
        return ipaddress.ip_address(request.remote_addr or "").is_loopback
    except ValueError:
        return False


def _presented_auth(token: str) -> str | None:
    """Return browser or bearer when Authorization proves the configured token."""
    value = request.headers.get("Authorization", "")
    scheme, separator, credential = value.partition(" ")
    if not separator:
        return None
    if scheme.lower() == "bearer":
        return "bearer" if secrets.compare_digest(credential.strip(), token) else None
    if scheme.lower() != "basic":
        return None
    try:
        decoded = base64.b64decode(credential, validate=True).decode("utf-8")
    except (binascii.Error, UnicodeDecodeError):
        return None
    username, separator, password = decoded.partition(":")
    if not separator or username != "operator":
        return None
    return "browser" if secrets.compare_digest(password, token) else None


def _same_origin() -> bool:
    source = request.headers.get("Origin") or request.headers.get("Referer")
    if not source:
        return False

    def origin_tuple(value: str) -> tuple[str, str, int] | None:
        try:
            parsed = urlsplit(value)
            if (
                parsed.scheme not in {"http", "https"}
                or not parsed.hostname
                or parsed.username is not None
                or parsed.password is not None
            ):
                return None
            port = parsed.port or {"http": 80, "https": 443}[parsed.scheme]
        except (KeyError, ValueError):
            return None
        return parsed.scheme, parsed.hostname.lower(), port

    return origin_tuple(source) == origin_tuple(request.host_url)


def _auth_error(message: str, status: int, *, challenge: bool = False):
    response = jsonify(ok=False, error=message)
    response.status_code = status
    response.headers["Cache-Control"] = "no-store"
    if challenge:
        response.headers["WWW-Authenticate"] = 'Basic realm="Operator", charset="UTF-8"'
    return response


def install_standalone_boundary(app: Flask, token: str, *, demo: bool) -> None:
    """Protect a standalone app without changing host-app blueprint mounts."""
    # A configured token authenticates proxy transit before any protected route
    # runs. ProxyFix keeps the blueprint's exact-origin check working under TLS.
    app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)

    @app.before_request
    def _standalone_boundary():
        if demo:
            # operator_view's demo boundary owns public read-only/isolated mode.
            return None
        auth_kind = _presented_auth(token) if token else None
        if token and not auth_kind:
            return _auth_error("valid Operator authorization required", 401, challenge=True)
        if not token and not _is_direct_loopback_request():
            return _auth_error("standalone access requires OPERATOR_AUTH_TOKEN", 403)
        if request.method in _UNSAFE_METHODS and auth_kind == "browser":
            if not _same_origin():
                return _auth_error("same-origin browser request required", 403)
        return None
