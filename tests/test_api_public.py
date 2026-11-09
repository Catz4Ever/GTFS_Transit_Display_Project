from __future__ import annotations

import pytest
from fastapi import FastAPI
from fastapi.testclient import TestClient

from app.api.routers import public
from tests.conftest import FakeConn, FakeResult, conn_context


def make_test_app(monkeypatch, conn: FakeConn) -> TestClient:
    app = FastAPI()
    app.include_router(public.router)
    monkeypatch.setattr(public, "get_conn", lambda: conn_context(conn))
    # Clear the in-process cache so tests always hit the monkeypatched conn
    public._cache.clear()
    return TestClient(app)


def test_health_endpoint_returns_ok_when_database_responds(monkeypatch):
    conn = FakeConn(responses=[FakeResult(one=(1,))])
    client = make_test_app(monkeypatch, conn)

    response = client.get("/health")

    assert response.status_code == 200
    assert response.json() == {"status": "ok"}


def test_health_endpoint_returns_500_when_database_fails(monkeypatch):
    class FailingConn(FakeConn):
        def execute(self, sql, params=None):
            raise RuntimeError("database unavailable")

    client = make_test_app(monkeypatch, FailingConn())

    response = client.get("/health")

    assert response.status_code == 500
    assert "database unavailable" in response.json()["detail"]


def test_cities_endpoint_serializes_city_summary(monkeypatch):
    conn = FakeConn(
        responses=[
            FakeResult(
                rows=[
                    (
                        "sofia",
                        "Sofia",
                        2,
                        1,
                        23.32,
                        42.69,
                        23.30,
                        42.68,
                        23.34,
                        42.70,
                        "Europe/Sofia",
                        3,
                    )
                ]
            )
        ]
    )
    client = make_test_app(monkeypatch, conn)

    response = client.get("/cities")

    assert response.status_code == 200
    assert response.json() == {
        "cities": [
            {
                "city_code": "sofia",
                "city_name": "Sofia",
                "stop_count": 2,
                "route_count": 1,
                "center": [23.32, 42.69],
                "bounds": [[23.30, 42.68], [23.34, 42.70]],
                "timezone_name": "Europe/Sofia",
                "rollover_hour": 3,
            }
        ]
    }
