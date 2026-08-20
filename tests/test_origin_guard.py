"""Cross-origin browser requests cannot mutate the private Operator."""

from flask import Flask

import operator_view as OV


def _client(monkeypatch):
    monkeypatch.setattr(
        OV.operator_agent.runner,
        "reset_session",
        lambda bot, **_kwargs: {"ok": True, "bot": bot},
    )
    app = Flask(__name__)
    app.config["TESTING"] = True
    app.register_blueprint(OV.bp)
    return app.test_client()


def test_foreign_origin_cannot_mutate_operator(monkeypatch) -> None:
    response = _client(monkeypatch).post(
        "/operator/agent/reset",
        json={"bot": "test"},
        headers={"Origin": "https://evil.example"},
    )

    assert response.status_code == 403
    assert response.get_json() == {
        "ok": False,
        "error": "cross-origin Operator mutation refused",
    }


def test_fetch_metadata_rejects_cross_site_post_without_origin(monkeypatch) -> None:
    response = _client(monkeypatch).post(
        "/operator/agent/reset",
        json={},
        headers={"Sec-Fetch-Site": "cross-site"},
    )

    assert response.status_code == 403


def test_foreign_referer_is_rejected_when_origin_is_absent(monkeypatch) -> None:
    response = _client(monkeypatch).post(
        "/operator/agent/reset",
        json={},
        headers={"Referer": "https://evil.example/trap"},
    )

    assert response.status_code == 403


def test_same_origin_browser_mutation_still_works(monkeypatch) -> None:
    response = _client(monkeypatch).post(
        "/operator/agent/reset",
        json={"bot": "test"},
        headers={
            "Origin": "http://localhost",
            "Sec-Fetch-Site": "same-origin",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_same_site_metadata_with_matching_origin_still_works(monkeypatch) -> None:
    """Safari/PWA requests may report same-site despite an exact origin match."""
    response = _client(monkeypatch).post(
        "/operator/agent/reset",
        json={"bot": "test"},
        headers={
            "Origin": "http://localhost",
            "Sec-Fetch-Site": "same-site",
        },
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True


def test_same_site_without_origin_or_referer_is_rejected(monkeypatch) -> None:
    response = _client(monkeypatch).post(
        "/operator/agent/reset",
        json={},
        headers={"Sec-Fetch-Site": "same-site"},
    )

    assert response.status_code == 403


def test_headerless_internal_client_still_works(monkeypatch) -> None:
    response = _client(monkeypatch).post(
        "/operator/agent/reset",
        json={"bot": "test"},
    )

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
