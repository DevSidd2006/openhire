"""Tests for the per-IP sliding-window rate limiter middleware."""
import pytest
from fastapi.testclient import TestClient

from api.app import create_app
from core.config import AppSettings, reset_settings_cache


def _make_client(max_requests: int = 5, window_seconds: int = 60) -> TestClient:
    reset_settings_cache()
    settings = AppSettings(
        rate_limit_enabled=True,
        rate_limit_requests=max_requests,
        rate_limit_window_seconds=window_seconds,
    )
    app = create_app(settings=settings)
    return TestClient(app)


class TestRateLimitMiddleware:

    def test_requests_within_limit_succeed(self):
        client = _make_client(max_requests=5)
        for _ in range(5):
            resp = client.get("/health")
            assert resp.status_code == 200

    def test_request_over_limit_returns_429(self):
        client = _make_client(max_requests=3)
        for _ in range(3):
            resp = client.get("/health")
            assert resp.status_code == 200

        resp = client.get("/health")
        assert resp.status_code == 429
        assert resp.json()["error"] == "rate_limit_exceeded"

    def test_429_includes_retry_after_header(self):
        client = _make_client(max_requests=1)
        client.get("/health")
        resp = client.get("/health")
        assert resp.status_code == 429
        assert "Retry-After" in resp.headers
        assert int(resp.headers["Retry-After"]) > 0

    def test_rate_limit_headers_on_success(self):
        client = _make_client(max_requests=10)
        resp = client.get("/health")
        assert resp.status_code == 200
        assert resp.headers["X-RateLimit-Limit"] == "10"
        assert resp.headers["X-RateLimit-Remaining"] == "9"
        assert resp.headers["X-RateLimit-Reset"] == "60"

    def test_remaining_decrements(self):
        client = _make_client(max_requests=5)
        for i in range(5):
            resp = client.get("/health")
            assert resp.headers["X-RateLimit-Remaining"] == str(5 - 1 - i)

    def test_static_pages_are_exempt(self):
        client = _make_client(max_requests=1)
        client.get("/health")
        resp = client.get("/app/index.html")
        assert resp.status_code != 429

    def test_disabled_by_default(self):
        reset_settings_cache()
        settings = AppSettings()
        assert settings.rate_limit_enabled is False

        app = create_app(settings=settings)
        client = TestClient(app)
        for _ in range(200):
            resp = client.get("/health")
            assert resp.status_code == 200
