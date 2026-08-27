from demo_contact import create_app


def test_holding_page_serves_its_wordmark_and_subtitle_fonts():
    app = create_app()
    client = app.test_client()

    page = client.get("/")

    assert page.status_code == 200
    assert b"font-family:'Plus Jakarta Sans'" in page.data
    assert b"font-family:'Urbanist'" in page.data
    assert b"/assets/jakarta.woff2" in page.data
    assert b"/assets/urbanist.woff2" in page.data
    assert "font-src 'self'" in page.headers["Content-Security-Policy"]

    for asset in ("jakarta.woff2", "urbanist.woff2"):
        response = client.get(f"/assets/{asset}")
        assert response.status_code == 200
        assert response.mimetype == "font/woff2"
        assert response.data.startswith(b"wOF2")

    assert client.get("/assets/other.woff2").status_code == 404
