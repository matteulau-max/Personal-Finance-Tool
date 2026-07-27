"""Our first test.

Anatomy of a test:
  - `TestClient` starts your FastAPI app *in memory*. No server, no network,
    no ports. This is why tests run in milliseconds.
  - We make a request, then `assert` what we expect. If an assert is false,
    pytest fails and shows you exactly which value differed.

Rule we will follow all project long: a bug is never "fixed" until a test
exists that would have caught it.
"""

from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_health_returns_200():
    response = client.get("/health")
    assert response.status_code == 200


def test_health_reports_ok_status():
    response = client.get("/health")
    body = response.json()
    assert body["status"] == "ok"
    assert "app_name" in body
    assert "environment" in body


def test_unknown_route_returns_404():
    """Guards against accidentally mounting a catch-all route later."""
    response = client.get("/this-route-does-not-exist")
    assert response.status_code == 404
