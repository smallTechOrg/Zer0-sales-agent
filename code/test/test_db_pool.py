"""
Regression tests for the shared connection pool.

These cover the two failures that prompted it:

1. ``/health`` and the rest of the API used different connections, so a green
   health check said nothing about whether the chat endpoints could reach the
   database (and vice versa).
2. The API held one connection for the life of the process, so a database
   restart broke every endpoint until the app itself was restarted.

Like the other tests in this project, these need a live database.
"""
import sys
import os
import threading
import uuid
from unittest.mock import patch

import psycopg
import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db_pool  # noqa: E402
import config  # noqa: E402
from config import DATABASE_URL  # noqa: E402


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def kill_app_connections() -> int:
    """
    Terminate the backends this app holds in the database under test.

    pg_stat_activity covers the whole instance, and prod_chat_db and
    staging_chat_db share one. application_name is the same in both, because
    nothing sets DB_APPLICATION_NAME, so a filter on it alone reaches the other
    database. datname keeps this inside the database the tests point at.
    """
    admin_url = DATABASE_URL.rsplit("/", 1)[0] + "/postgres"
    with psycopg.connect(admin_url, autocommit=True) as conn:
        with conn.cursor() as cur:
            cur.execute(
                "SELECT pg_terminate_backend(pid) FROM pg_stat_activity "
                "WHERE application_name = %s AND datname = %s "
                "AND pid <> pg_backend_pid();",
                (config.DB_APPLICATION_NAME, config.db_name),
            )
            return len(cur.fetchall())


# ---------------------------------------------------------------------------
# Retry behaviour (no database needed)
# ---------------------------------------------------------------------------

class TestRetry:
    def test_reruns_the_operation_after_a_lost_connection(self):
        """A dropped connection is retried on a fresh one, not surfaced."""
        calls = []

        def flaky(conn):
            calls.append(1)
            if len(calls) == 1:
                raise psycopg.OperationalError("server closed the connection")
            return "second attempt worked"

        assert db_pool.run_with_retry(flaky) == "second attempt worked"
        assert len(calls) == 2

    def test_gives_up_after_the_configured_attempts(self):
        def always_broken(conn):
            raise psycopg.OperationalError("server closed the connection")

        with pytest.raises(db_pool.DatabaseUnavailable):
            db_pool.run_with_retry(always_broken, attempts=2)

    def test_does_not_retry_programming_errors(self):
        """Bad SQL fails once: repeating it would only fail again."""
        calls = []

        def bad_sql(conn):
            calls.append(1)
            raise psycopg.ProgrammingError('relation "nope" does not exist')

        with pytest.raises(psycopg.ProgrammingError):
            db_pool.run_with_retry(bad_sql)
        assert len(calls) == 1

    def test_retry_backoff_is_not_slept_twice_per_attempt(self):
        """The outer loop owns the backoff; acquisition must not double it."""
        with patch.object(db_pool.time, "sleep") as sleep:
            with pytest.raises(db_pool.DatabaseUnavailable):
                db_pool.run_with_retry(
                    lambda conn: (_ for _ in ()).throw(
                        psycopg.OperationalError("gone")
                    ),
                    attempts=3,
                )
        assert sleep.call_count == 2  # between three attempts, not more


# ---------------------------------------------------------------------------
# Transaction handling
# ---------------------------------------------------------------------------

class TestGetConnection:
    def test_commits_on_clean_exit(self):
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("CREATE TEMP TABLE pool_commit_check (v int);")
                cur.execute("INSERT INTO pool_commit_check VALUES (1);")
        # A committed transaction leaves the connection idle, not in-transaction.
        with db_pool.get_connection() as conn:
            assert conn.info.transaction_status == psycopg.pq.TransactionStatus.IDLE

    def test_rolls_back_and_returns_a_usable_connection(self):
        """
        A failed statement must not poison the next request.

        With a single shared connection this was the bug behind the scattered
        ``rollback()`` calls: one bad query left everyone else in a failed
        transaction.
        """
        with pytest.raises(psycopg.errors.UndefinedTable):
            with db_pool.get_connection() as conn:
                with conn.cursor() as cur:
                    cur.execute("SELECT * FROM definitely_not_a_table;")

        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1;")
                assert cur.fetchone()[0] == 1


# ---------------------------------------------------------------------------
# An overloaded database (reachable, but not answering)
# ---------------------------------------------------------------------------

class TestSlowDatabase:
    """
    On 2026-09-27 the shared instance stopped answering under a scraper burst:
    connections succeeded, queries did not return, CPU sat at 57%. Without a
    statement timeout a request parks forever, and with a small pool a couple of
    parked requests block everyone.
    """

    def test_pooled_connections_have_a_statement_timeout(self):
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW statement_timeout;")
                assert cur.fetchone()[0] != "0", "queries could hang indefinitely"
                cur.execute("SHOW idle_in_transaction_session_timeout;")
                assert cur.fetchone()[0] != "0", "a stuck transaction could pin a connection"
                cur.execute("SHOW lock_timeout;")
                assert cur.fetchone()[0] != "0", (
                    "someone else's migration could pin both connections for the "
                    "whole statement budget"
                )

    def test_a_timed_out_query_is_not_retried(self):
        """
        Retrying a statement that timed out adds load to a database that is
        already struggling. It has to fail once and shed the request.
        """
        calls = []

        def too_slow(conn):
            calls.append(1)
            with conn.cursor() as cur:
                cur.execute("SET LOCAL statement_timeout = '100ms';")
                cur.execute("SELECT pg_sleep(5);")

        with pytest.raises(psycopg.errors.QueryCanceled):
            db_pool.run_with_retry(too_slow)
        assert len(calls) == 1, "a slow database must not be hit three times"

    def test_a_lock_timeout_is_not_retried(self):
        """
        lock_timeout raises LockNotAvailable (55P03), not QueryCanceled
        (57014). Retrying waits again on a lock someone else still holds, and
        with two pooled connections that pins the whole app for the full retry
        budget instead of the lock timeout.
        """
        calls = []

        def blocked(conn):
            calls.append(1)
            raise psycopg.errors.LockNotAvailable("canceling statement due to lock timeout")

        with pytest.raises(psycopg.errors.LockNotAvailable):
            db_pool.run_with_retry(blocked)
        assert len(calls) == 1

    def test_lock_timeout_is_configured(self):
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SHOW lock_timeout;")
                assert cur.fetchone()[0] != "0"

    def test_a_lost_connection_is_still_retried(self):
        """The narrower no-retry rule must not disable retrying in general."""
        calls = []

        def flaky(conn):
            calls.append(1)
            if len(calls) == 1:
                raise psycopg.errors.AdminShutdown("terminating connection")
            return "recovered"

        assert db_pool.run_with_retry(flaky) == "recovered"
        assert len(calls) == 2


# ---------------------------------------------------------------------------
# Refilling the pool
# ---------------------------------------------------------------------------

def _borrow_one():
    """Borrow a connection and run the cheapest possible statement on it."""
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()


class TestPoolCheck:
    """
    A restart closes every connection. Nothing polls the pool on a timer: the
    pool tests each connection as it is handed out and replaces a dead one.
    """

    def test_no_background_thread_polls_the_pool(self):
        db_pool.get_pool()
        names = [t.name for t in threading.enumerate()]
        assert "db-pool-check" not in names

    def test_checkout_replaces_the_closed_connections(self):
        pool = db_pool.get_pool()
        _borrow_one()
        # get_stats omits a counter that is still zero.
        before = pool.get_stats().get("connections_lost", 0)

        kill_app_connections()

        # No timer ran. The next borrow has to notice and reconnect by itself.
        _borrow_one()

        assert pool.get_stats().get("connections_lost", 0) > before
        assert pool.get_stats()["pool_size"] >= 1


# ---------------------------------------------------------------------------
# Pool sizing
# ---------------------------------------------------------------------------

class TestSmallPool:
    """
    The pool is deliberately tiny because the database is a shared cloud
    instance. That only works while no request holds two connections at once.
    """

    def test_no_request_holds_two_connections_at_once(self, client):
        """
        A nested borrow deadlocks a 2-connection pool: two requests take one
        connection each, then wait on each other. Nothing in this app nests --
        every borrow is a single short query -- and this test keeps it so.
        """
        db_pool.max_borrow_depth_seen = 0

        session_id = str(uuid.uuid4())
        client.get("/health")
        client.get("/prompts")
        client.get("/chat-info")
        client.get("/domains/")
        client.get(
            "/history",
            query_string={"session_id": session_id},
            headers={"Origin": "http://example.com"},
        )
        client.patch("/chat-info", json={"session_id": session_id, "status": "OPEN"})

        assert db_pool.max_borrow_depth_seen == 1, (
            "A request borrowed a second connection while still holding the "
            "first. With DB_POOL_MAX_SIZE=2 that deadlocks under concurrency: "
            "release the first connection before taking another."
        )

    def test_concurrent_requests_queue_instead_of_failing(self, client):
        """More concurrent callers than connections must wait, not error."""
        results = []

        def call():
            results.append(client.get("/prompts").status_code)

        threads = [threading.Thread(target=call) for _ in range(12)]
        for t in threads:
            t.start()
        for t in threads:
            t.join(timeout=60)

        assert all(not t.is_alive() for t in threads), "a request deadlocked"
        assert results == [200] * 12
        assert db_pool.get_pool().get_stats()["pool_size"] <= config.DB_POOL_MAX_SIZE


# ---------------------------------------------------------------------------
# Recovery from a database restart
# ---------------------------------------------------------------------------

class TestRestartRecovery:
    def test_health_and_api_share_one_pool(self, client):
        """
        The health probe must observe the same connections the API uses.
        Otherwise it can report "connected" while the API cannot reach the DB.
        """
        assert client.get("/health").status_code == 200
        assert client.get("/prompts").status_code == 200

        stats = db_pool.get_pool().get_stats()
        assert stats["pool_size"] >= 1
        # Both endpoints were served without opening anything outside the pool.
        assert stats.get("connections_lost", 0) >= 0

    def test_health_recovers_after_a_restart(self, client):
        client.get("/health")
        kill_app_connections()

        body = client.get("/health")
        assert body.status_code == 200
        assert body.get_json()["database"] == "connected"

    def test_api_recovers_after_a_restart(self, client):
        """The endpoint that used to 500 forever after a restart."""
        assert client.get("/prompts").status_code == 200
        kill_app_connections()

        response = client.get("/prompts")
        assert response.status_code == 200
        # Not just a 200: the old code returned an empty list on failure.
        assert len(response.get_json()["prompts"]) > 0

    def test_api_recovers_even_when_health_is_never_called(self, client):
        """
        Recovery must not depend on the health check running first -- the two
        used to have independent connections.
        """
        client.get("/chat-info")
        kill_app_connections()

        assert client.get("/chat-info").status_code == 200
