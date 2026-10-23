"""One-time OAuth tickets must stay one-time under concurrency."""
from concurrent.futures import ThreadPoolExecutor
import threading

from fastapi.testclient import TestClient

from core.config import AppSettings
from services.oauth_ticket_store import OAuthTicketStore
from tests.test_linkedin_oauth import _build_test_app

_TICKET_COOKIE = "openhire_oauth_ticket"


def test_store_allows_only_one_concurrent_consume():
    store = OAuthTicketStore()
    barrier = threading.Barrier(8)

    def attempt(_index: int) -> bool:
        barrier.wait()
        return store.consume("same-ticket", ttl_seconds=60)

    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(attempt, range(8)))

    assert results.count(True) == 1


def test_store_rejects_a_sequential_replay():
    store = OAuthTicketStore()

    assert store.consume("ticket", ttl_seconds=60) is True
    assert store.consume("ticket", ttl_seconds=60) is False


def test_store_prunes_expired_tickets():
    store = OAuthTicketStore()

    assert store.consume("ticket", ttl_seconds=0) is True
    assert store.consume("ticket", ttl_seconds=60) is True


def _issue_ticket(app) -> tuple[str, str]:
    client = TestClient(app)
    authorize = client.get(
        "/auth/linkedin/authorize?role=candidate", follow_redirects=False
    )
    state = authorize.cookies["oauth_state"]
    callback = client.get(
        f"/auth/linkedin/callback?code=mock_test_code&state={state}:candidate",
        follow_redirects=False,
    )

    location = callback.headers["location"]
    ticket = location.split("ticket=")[1].split("&")[0]
    encrypted = callback.cookies[_TICKET_COOKIE]
    return ticket, encrypted


def _client_with_ticket(app, encrypted: str) -> TestClient:
    client = TestClient(app)
    client.cookies.set(_TICKET_COOKIE, encrypted)
    return client


def _settings() -> AppSettings:
    return AppSettings(
        environment="development",
        linkedin_mock_enabled=True,
        auth_enabled=True,
    )


def test_exchange_rejects_a_second_request_with_the_same_cookie():
    app, _, _ = _build_test_app(_settings())
    ticket, encrypted = _issue_ticket(app)

    first = _client_with_ticket(app, encrypted).post(
        "/auth/linkedin/exchange", json={"ticket": ticket}
    )
    assert first.status_code == 200

    second = _client_with_ticket(app, encrypted).post(
        "/auth/linkedin/exchange", json={"ticket": ticket}
    )
    assert second.status_code == 401
    assert second.json()["error"] == "unauthorized"


def test_concurrent_exchange_requests_cannot_both_succeed():
    app, _, _ = _build_test_app(_settings())
    ticket, encrypted = _issue_ticket(app)
    start = threading.Barrier(2)

    def exchange() -> int:
        client = _client_with_ticket(app, encrypted)
        start.wait()
        return client.post(
            "/auth/linkedin/exchange", json={"ticket": ticket}
        ).status_code

    with ThreadPoolExecutor(max_workers=2) as pool:
        statuses = sorted(pool.map(lambda _index: exchange(), range(2)))

    assert statuses == [200, 401]
