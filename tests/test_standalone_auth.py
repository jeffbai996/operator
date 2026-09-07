"""Standalone Operator keeps local UX while closing remote and CSRF ingress."""
from __future__ import annotations

import base64

import pytest

import operator_view
from app import create_app, validate_listen_host


TOKEN = "t" * 40
LOOPBACK = {"REMOTE_ADDR": "127.0.0.1"}
LOCAL_HEADERS = {"Host": "127.0.0.1:5005"}


def _basic(token: str = TOKEN) -> str:
    encoded = base64.b64encode(f"operator:{token}".encode()).decode()
    return f"Basic {encoded}"


@pytest.fixture(autouse=True)
def clean_auth_env(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.delenv("OPERATOR_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("OPERATOR_DEMO", raising=False)


def test_default_direct_loopback_read_remains_available() -> None:
    response = create_app().test_client().get(
        "/operator/models", headers=LOCAL_HEADERS, environ_base=LOOPBACK
    )

    assert response.status_code == 200
    assert response.get_json()["models"]


def test_default_loopback_rejects_cross_site_browser_mutation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []
    monkeypatch.setattr(
        operator_view._streamer,
        "run_action",
        lambda action: called.append(action) or {"ok": True},
    )
    response = create_app().test_client().post(
        "/operator/steer",
        json={"kind": "key", "value": "Enter"},
        headers=LOCAL_HEADERS | {"Origin": "https://attacker.example"},
        environ_base=LOOPBACK,
    )

    assert response.status_code == 403
    assert called == []


def test_default_loopback_headerless_machine_mutation_remains_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []
    monkeypatch.setattr(
        operator_view._streamer,
        "run_action",
        lambda action: called.append(action) or {"ok": True},
    )

    response = create_app().test_client().post(
        "/operator/steer",
        json={"kind": "key", "value": "Enter"},
        headers=LOCAL_HEADERS,
        environ_base=LOOPBACK,
    )

    assert response.status_code == 200
    assert [action["kind"] for action in called] == ["key"]


def test_default_loopback_same_origin_cockpit_mutation_remains_available(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    called = []
    monkeypatch.setattr(
        operator_view._streamer,
        "run_action",
        lambda action: called.append(action) or {"ok": True},
    )

    response = create_app().test_client().post(
        "/operator/steer",
        json={"kind": "key", "value": "Enter"},
        headers=LOCAL_HEADERS | {"Origin": "http://127.0.0.1:5005"},
        environ_base=LOOPBACK,
    )

    assert response.status_code == 200
    assert [action["kind"] for action in called] == ["key"]


@pytest.mark.parametrize(
    ("headers", "remote_addr"),
    [
        ({"Host": "operator.example"}, "127.0.0.1"),
        (LOCAL_HEADERS | {"X-Forwarded-For": "203.0.113.9"}, "127.0.0.1"),
        (LOCAL_HEADERS, "203.0.113.9"),
    ],
)
def test_unconfigured_proxy_or_remote_read_fails_closed(
    headers: dict[str, str], remote_addr: str
) -> None:
    response = create_app().test_client().get(
        "/operator/models",
        headers=headers,
        environ_base={"REMOTE_ADDR": remote_addr},
    )

    assert response.status_code == 403
    assert response.get_json()["error"] == "standalone access requires OPERATOR_AUTH_TOKEN"


def test_configured_basic_auth_allows_remote_read_and_same_origin_cockpit_action(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", TOKEN)
    called = []
    monkeypatch.setattr(
        operator_view._streamer,
        "run_action",
        lambda action: called.append(action) or {"ok": True},
    )
    client = create_app().test_client()
    auth = {
        "Authorization": _basic(),
        "Host": "operator.example",
        "X-Forwarded-Proto": "https",
    }

    read = client.get(
        "/operator/models", headers=auth, environ_base={"REMOTE_ADDR": "192.0.2.5"}
    )
    mutation = client.post(
        "/operator/steer",
        json={"kind": "key", "value": "Enter"},
        headers=auth | {"Origin": "https://operator.example"},
        environ_base={"REMOTE_ADDR": "192.0.2.5"},
    )

    assert read.status_code == 200
    assert mutation.status_code == 200
    assert [action["kind"] for action in called] == ["key"]


@pytest.mark.parametrize(
    "origin",
    [
        "https://attacker.example",
        "https://operator.example:444",
        "http://operator.example",
        "https://operator.example@attacker.example",
    ],
)
def test_configured_basic_auth_still_rejects_cross_origin_mutation(
    monkeypatch: pytest.MonkeyPatch, origin: str
) -> None:
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", TOKEN)
    called = []
    monkeypatch.setattr(
        operator_view._streamer,
        "run_action",
        lambda action: called.append(action) or {"ok": True},
    )

    response = create_app().test_client().post(
        "/operator/steer",
        json={"kind": "key", "value": "Enter"},
        headers={
            "Authorization": _basic(),
            "Host": "operator.example",
            "Origin": origin,
            "X-Forwarded-Proto": "https",
        },
        environ_base={"REMOTE_ADDR": "192.0.2.5"},
    )

    assert response.status_code == 403
    assert called == []


def test_bearer_token_preserves_nonbrowser_automation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", TOKEN)
    called = []
    monkeypatch.setattr(
        operator_view._streamer,
        "run_action",
        lambda action: called.append(action) or {"ok": True},
    )

    response = create_app().test_client().post(
        "/operator/steer",
        json={"kind": "key", "value": "Enter"},
        headers={"Authorization": f"Bearer {TOKEN}", "Host": "operator.example"},
        environ_base={"REMOTE_ADDR": "192.0.2.5"},
    )

    assert response.status_code == 200
    assert [action["kind"] for action in called] == ["key"]


def test_missing_or_wrong_configured_token_gets_basic_challenge(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", TOKEN)
    client = create_app().test_client()

    missing = client.get("/operator/models", headers={"Host": "operator.example"})
    wrong = client.get(
        "/operator/models",
        headers={"Host": "operator.example", "Authorization": _basic("x" * 40)},
    )

    assert missing.status_code == 401
    assert wrong.status_code == 401
    assert missing.headers["WWW-Authenticate"].startswith("Basic ")


def test_weak_configured_token_is_rejected(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("OPERATOR_AUTH_TOKEN", "too-short")

    with pytest.raises(RuntimeError, match="at least 32 characters"):
        create_app()


@pytest.mark.parametrize("host", ["0.0.0.0", "192.0.2.10", "example.com"])
def test_nonloopback_listener_requires_token(host: str) -> None:
    with pytest.raises(RuntimeError, match="OPERATOR_AUTH_TOKEN"):
        validate_listen_host(host, "")

    validate_listen_host(host, TOKEN)


@pytest.mark.parametrize("host", ["127.0.0.1", "::1", "localhost"])
def test_loopback_listener_keeps_zero_config_start(host: str) -> None:
    validate_listen_host(host, "")
