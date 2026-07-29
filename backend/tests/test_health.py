"""Tests for the health endpoints.

Anatomy of a test:
  - `TestClient` starts your FastAPI app *in memory*. No server, no network,
    no ports. This is why tests run in milliseconds.
  - We make a request, then `assert` what we expect. If an assert is false,
    pytest fails and shows you exactly which value differed.

Rule we will follow all project long: a bug is never "fixed" until a test
exists that would have caught it.
"""

from fastapi.testclient import TestClient

from app.db.session import get_db
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


def test_database_health_reports_the_migration_revision(db):
    """An integration test: the endpoint really queries a real database.

    `app.dependency_overrides` swaps the app's `get_db` for our transactional
    test session. This is FastAPI's built-in mechanism for exactly this, and
    it is why `get_db` was written as a dependency rather than called directly
    inside each endpoint -- testable seams have to be designed in.
    """
    app.dependency_overrides[get_db] = lambda: db
    try:
        response = client.get("/health/db")
    finally:
        # Always undo the override, even on failure, or every later test in
        # the process inherits it.
        app.dependency_overrides.clear()

    assert response.status_code == 200
    body = response.json()
    assert body["status"] == "ok"
    assert body["database"] == "connected"
    # Proves migrations have been applied to the database being served.
    assert body["migration_revision"] is not None


def test_database_health_returns_503_when_the_database_is_down(db):
    """A dependency failure must be reported as 'not ready', not 'crashed'.

    503 tells a load balancer to route elsewhere; a 500 would suggest a bug in
    the code, and restarting the container would not help.
    """

    class BrokenSession:
        def execute(self, *args, **kwargs):
            from sqlalchemy.exc import OperationalError

            raise OperationalError("SELECT 1", {}, Exception("connection refused"))

    app.dependency_overrides[get_db] = lambda: BrokenSession()
    try:
        response = client.get("/health/db")
    finally:
        app.dependency_overrides.clear()

    assert response.status_code == 503
    body = response.json()
    assert body["status"] == "error"
    # The failure detail must never leak the connection string or password.
    assert "connection refused" not in str(body)
