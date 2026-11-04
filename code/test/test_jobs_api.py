"""
Tests for POST /jobs/periodic-summary, the endpoint Cloud Scheduler calls on
Cloud Run. Token verification and periodic_summary() are stubbed; the
advisory lock runs against the test database.
"""
import os
import sys
import threading

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
from api import jobs  # noqa: E402
from app import app as flask_app  # noqa: E402

INVOKER = "zero-scheduler@example.iam.gserviceaccount.com"
AUDIENCE = "zero-sales-agent-jobs"
URL = "/jobs/periodic-summary"
AUTH = {"Authorization": "Bearer good-token"}

FAKE_RESULT = {
    "session_count": 3,
    "summarised_count": 2,
    "llm_failed_count": 1,
    "silent_count": 0,
    "resent_count": 1,
    "notified_count": 3,
    "slack": {"sent": 1},
    "sessions": [{"session_id": "s1", "email": "visitor@example.com"}],
}


@pytest.fixture
def client():
    return flask_app.test_client()


@pytest.fixture
def configured(monkeypatch):
    """Job auth configured, and a token stub that accepts 'good-token'."""
    monkeypatch.setattr(config, "JOBS_OIDC_AUDIENCE", AUDIENCE)
    monkeypatch.setattr(config, "JOBS_ALLOWED_INVOKERS", frozenset({INVOKER}))
    seen = {}

    def fake_verify(token, request, audience):
        seen["audience"] = audience
        if token != "good-token":
            raise ValueError("bad signature")
        return {"email": INVOKER.upper(), "email_verified": True}

    monkeypatch.setattr(jobs.id_token, "verify_oauth2_token", fake_verify)
    return seen


@pytest.fixture
def summary(monkeypatch):
    calls = []

    def fake_periodic_summary():
        calls.append(True)
        return FAKE_RESULT

    monkeypatch.setattr(jobs, "periodic_summary", fake_periodic_summary)
    return calls


class TestAuth:
    def test_refused_when_not_configured(self, client, summary, monkeypatch):
        monkeypatch.setattr(config, "JOBS_OIDC_AUDIENCE", "")
        monkeypatch.setattr(config, "JOBS_ALLOWED_INVOKERS", frozenset())
        resp = client.post(URL, headers=AUTH)
        assert resp.status_code == 401
        assert summary == []

    def test_refused_without_token(self, client, configured, summary):
        resp = client.post(URL)
        assert resp.status_code == 401
        assert summary == []

    def test_refused_with_invalid_token(self, client, configured, summary):
        resp = client.post(URL, headers={"Authorization": "Bearer forged"})
        assert resp.status_code == 401
        assert summary == []

    def test_refused_for_other_service_account(self, client, configured, summary, monkeypatch):
        monkeypatch.setattr(
            jobs.id_token,
            "verify_oauth2_token",
            lambda *a, **k: {"email": "someone@else.com", "email_verified": True},
        )
        resp = client.post(URL, headers=AUTH)
        assert resp.status_code == 401
        assert summary == []

    def test_refused_for_unverified_email(self, client, configured, summary, monkeypatch):
        monkeypatch.setattr(
            jobs.id_token,
            "verify_oauth2_token",
            lambda *a, **k: {"email": INVOKER, "email_verified": False},
        )
        resp = client.post(URL, headers=AUTH)
        assert resp.status_code == 401

    def test_get_is_not_allowed(self, client, configured, summary):
        # The legacy swagger UI blueprint answers unknown GETs, so not 405.
        assert client.get(URL, headers=AUTH).status_code in (404, 405)
        assert summary == []


class TestRun:
    def test_runs_and_returns_counts_only(self, client, configured, summary):
        resp = client.post(URL, headers=AUTH)

        assert resp.status_code == 200
        assert summary == [True]
        assert configured["audience"] == AUDIENCE
        body = resp.get_json()
        assert body == {
            "status": "ok",
            "session_count": 3,
            "summarised_count": 2,
            "llm_failed_count": 1,
            "silent_count": 0,
            "resent_count": 1,
            "notified_count": 3,
            "slack_sent": 1,
        }
        # Visitor details never leave in the response.
        assert "sessions" not in body

    def test_failure_is_500(self, client, configured, monkeypatch):
        def boom():
            raise RuntimeError("llm down")

        monkeypatch.setattr(jobs, "periodic_summary", boom)
        resp = client.post(URL, headers=AUTH)
        assert resp.status_code == 500
        assert resp.get_json()["status"] == "failed"

    def test_lock_is_released_after_a_failure(self, client, configured, monkeypatch):
        monkeypatch.setattr(jobs, "periodic_summary", lambda: 1 / 0)
        assert client.post(URL, headers=AUTH).status_code == 500
        monkeypatch.setattr(jobs, "periodic_summary", lambda: FAKE_RESULT)
        assert client.post(URL, headers=AUTH).status_code == 200


class TestSingleRun:
    def test_second_call_in_same_process_is_skipped(self, client, configured, summary):
        assert jobs._local_lock.acquire(blocking=False)
        try:
            resp = client.post(URL, headers=AUTH)
        finally:
            jobs._local_lock.release()
        assert resp.status_code == 409
        assert summary == []

    def test_run_on_another_instance_is_skipped(self, client, configured, summary):
        """Another instance holds the advisory lock in the database."""
        import psycopg

        with psycopg.connect(config.DATABASE_URL, autocommit=True) as other:
            assert other.execute(
                "SELECT pg_try_advisory_lock(%s)", (jobs.PERIODIC_SUMMARY_LOCK_KEY,)
            ).fetchone()[0]
            resp = client.post(URL, headers=AUTH)
        assert resp.status_code == 409
        assert summary == []

        # Once that instance is gone, the next call runs.
        assert client.post(URL, headers=AUTH).status_code == 200

    def test_concurrent_calls_run_once(self, client, configured, monkeypatch):
        started, release = threading.Event(), threading.Event()
        calls = []

        def slow_summary():
            calls.append(True)
            started.set()
            release.wait(5)
            return FAKE_RESULT

        monkeypatch.setattr(jobs, "periodic_summary", slow_summary)
        first = {}
        t = threading.Thread(
            target=lambda: first.setdefault("resp", flask_app.test_client().post(URL, headers=AUTH))
        )
        t.start()
        assert started.wait(5)
        second = client.post(URL, headers=AUTH)
        release.set()
        t.join(5)

        assert second.status_code == 409
        assert first["resp"].status_code == 200
        assert calls == [True]


class TestDatabaseDown:
    def test_unreachable_database_is_503(self, client, configured, summary, monkeypatch):
        monkeypatch.setattr(
            config, "DATABASE_URL", "postgresql://nobody@127.0.0.1:1/none"
        )
        monkeypatch.setattr(config, "DB_CONNECT_TIMEOUT", 1)
        resp = client.post(URL, headers=AUTH)
        assert resp.status_code == 503
        assert summary == []
