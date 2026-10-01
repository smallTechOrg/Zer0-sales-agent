"""
Shared PostgreSQL connection pool.

Every database access in this application goes through the pool created here --
including ``/health``. That is deliberate. When the health check opens its own
connection it answers a different question from the one the rest of the API
cares about: it can report "connected" while the connection the chat API is
holding is already dead, or report a failure while the API is serving fine.
One pool means one answer.

The pool also removes the two failure modes that came with a single long-lived
module-level connection:

* a connection dropped by a database restart was never replaced, so every
  request after the restart failed until the whole app was restarted;
* that one connection was shared by all of Flask's worker threads, so a failed
  statement in one request left the transaction broken for the others.

Connections are validated before they are lent out, recycled once they get old
or idle, and acquisition is retried with exponential backoff so a request that
arrives while PostgreSQL is restarting waits for it instead of failing outright.
"""
from __future__ import annotations

import atexit
import functools
import logging
import signal
import threading
import time
from contextlib import contextmanager
from typing import Any, Callable, Iterator, Optional, TypeVar

import psycopg
from psycopg_pool import ConnectionPool

from config import (
    DATABASE_URL,
    DB_APPLICATION_NAME,
    DB_CONNECT_TIMEOUT,
    DB_HEALTH_TIMEOUT,
    DB_IDLE_TX_TIMEOUT_MS,
    DB_KEEPALIVES_COUNT,
    DB_KEEPALIVES_IDLE,
    DB_KEEPALIVES_INTERVAL,
    DB_LOCK_TIMEOUT_MS,
    DB_POOL_MAX_IDLE,
    DB_POOL_MAX_LIFETIME,
    DB_POOL_MAX_SIZE,
    DB_POOL_MIN_SIZE,
    DB_POOL_TIMEOUT,
    DB_RECONNECT_TIMEOUT,
    DB_RETRY_ATTEMPTS,
    DB_RETRY_BASE_DELAY,
    DB_RETRY_MAX_DELAY,
    DB_STATEMENT_TIMEOUT_MS,
    DB_TCP_USER_TIMEOUT_MS,
)

logger = logging.getLogger(__name__)

T = TypeVar("T")

# ---------------------------------------------------------------------------
# Settings
#
# All of these are declared in config.py, which is the single place anything
# environment-driven is defined. Nothing here reads os.getenv.
# ---------------------------------------------------------------------------

# Connection-level failures: the server went away, is restarting, or the pool
# could not produce a connection. Every psycopg_pool error (PoolTimeout,
# PoolClosed, TooManyRequests) subclasses OperationalError, so it is covered,
# as do AdminShutdown (someone restarted the instance) and TooManyConnections
# (a shared server is momentarily full) -- both worth another try.
# Programming errors -- bad SQL, constraint violations -- are deliberately not
# retried: repeating them would only fail again.
RETRYABLE_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)

# Hitting our own statement_timeout is the exception. It subclasses
# OperationalError, but it does not mean the connection broke -- it means the
# database was too slow to answer. Re-running the query adds load to a database
# that is already struggling, which is how a slow database becomes a down one.
# Fail fast instead and shed the request.
NON_RETRYABLE_ERRORS = (psycopg.errors.QueryCanceled,)


def _is_retryable(exc: BaseException) -> bool:
    return isinstance(exc, RETRYABLE_ERRORS) and not isinstance(
        exc, NON_RETRYABLE_ERRORS
    )


class DatabaseUnavailable(psycopg.OperationalError):
    """Raised when no working connection could be obtained after retrying."""


# ---------------------------------------------------------------------------
# Pool lifecycle
# ---------------------------------------------------------------------------

_pool: Optional[ConnectionPool] = None
_pool_lock = threading.Lock()


def _on_reconnect_failed(pool: ConnectionPool) -> None:
    """Surface a prolonged outage in the logs instead of failing silently."""
    logger.error(
        "Pool %r could not reconnect within %ss. The app stays up and keeps "
        "retrying; /health reports the database as down until it succeeds.",
        pool.name,
        DB_RECONNECT_TIMEOUT,
    )


def _build_pool() -> ConnectionPool:
    pool = ConnectionPool(
        conninfo=DATABASE_URL,
        min_size=DB_POOL_MIN_SIZE,
        max_size=DB_POOL_MAX_SIZE,
        timeout=DB_POOL_TIMEOUT,
        max_idle=DB_POOL_MAX_IDLE,
        max_lifetime=DB_POOL_MAX_LIFETIME,
        reconnect_timeout=DB_RECONNECT_TIMEOUT,
        reconnect_failed=_on_reconnect_failed,
        # Validate the connection before lending it out: one killed by a
        # database restart is discarded and replaced here, instead of being
        # handed to a request that would then fail on its first statement.
        check=ConnectionPool.check_connection,
        kwargs={
            "connect_timeout": DB_CONNECT_TIMEOUT,
            "application_name": DB_APPLICATION_NAME,
            # Server-side ceilings, so no single query can hold a connection
            # (or a transaction) open indefinitely.
            "options": (
                f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS} "
                f"-c lock_timeout={DB_LOCK_TIMEOUT_MS} "
                f"-c idle_in_transaction_session_timeout={DB_IDLE_TX_TIMEOUT_MS}"
            ),
            # Kernel-side ceilings, for when the server stops answering at all
            # and cannot enforce its own timeouts.
            "keepalives": 1,
            "keepalives_idle": DB_KEEPALIVES_IDLE,
            "keepalives_interval": DB_KEEPALIVES_INTERVAL,
            "keepalives_count": DB_KEEPALIVES_COUNT,
            "tcp_user_timeout": DB_TCP_USER_TIMEOUT_MS,
        },
        name="chatdb",
        open=False,
    )
    # wait=False: the app has to start even when the database is down, so that
    # /health can report the outage instead of the process dying at import time.
    # Background workers keep trying to fill the pool.
    pool.open(wait=False)
    logger.info(
        "Database pool opened (min=%s max=%s timeout=%ss)",
        DB_POOL_MIN_SIZE,
        DB_POOL_MAX_SIZE,
        DB_POOL_TIMEOUT,
    )
    return pool


def get_pool() -> ConnectionPool:
    """Return the process-wide pool, creating it on first use."""
    global _pool
    if _pool is None:
        with _pool_lock:
            if _pool is None:
                _pool = _build_pool()
    return _pool


def _take_pool() -> Optional[ConnectionPool]:
    """Detach the current pool, if any, so it can be closed exactly once."""
    global _pool
    with _pool_lock:
        pool, _pool = _pool, None
    return pool


def close_pool() -> None:
    """Close the pool and all its connections. Safe to call more than once."""
    pool = _take_pool()
    if pool is not None:
        pool.close()
        logger.info("Database pool closed")


def _close_pool_at_exit() -> None:
    """
    Release connections on the way out.

    Deliberately silent: at interpreter shutdown the log stream may already be
    closed, and a logging failure here would print a traceback over whatever
    the process was actually reporting.
    """
    pool = _take_pool()
    if pool is not None:
        pool.close()


def _handle_sigterm(signum, frame):
    """
    Close the pool when the deploy script stops the app.

    deploy.sh stops the old process with `kill`, and Python's default SIGTERM
    disposition terminates the process outright -- atexit handlers never run,
    so the connections would be left for the server to reap. On a shared
    instance it is worth handing them back deliberately.

    SIGINT needs no handler: it raises KeyboardInterrupt, the interpreter shuts
    down normally, and atexit runs.
    """
    _close_pool_at_exit()
    raise SystemExit(128 + signum)


atexit.register(_close_pool_at_exit)

try:
    signal.signal(signal.SIGTERM, _handle_sigterm)
except (ValueError, OSError):  # pragma: no cover - not the main thread
    pass


def pool_status() -> dict:
    """Pool counters, for the health endpoint."""
    stats = get_pool().get_stats()
    return {
        "min_size": stats.get("pool_min"),
        "max_size": stats.get("pool_max"),
        "size": stats.get("pool_size"),
        "available": stats.get("pool_available"),
        "waiting": stats.get("requests_waiting"),
        "connections_lost": stats.get("connections_lost", 0),
    }


# ---------------------------------------------------------------------------
# Borrowing a connection
# ---------------------------------------------------------------------------

# Nesting guard. With a pool this small, a request that borrows a second
# connection while still holding the first is a deadlock waiting to happen:
# two such requests take one connection each and then wait on each other until
# they time out. No path in this app nests (every borrow is one short query),
# and this keeps it that way -- it is what fails the test if someone adds one.
_borrow_depth = threading.local()
max_borrow_depth_seen = 0


@contextmanager
def _track_borrow_depth() -> Iterator[None]:
    global max_borrow_depth_seen
    depth = getattr(_borrow_depth, "value", 0) + 1
    _borrow_depth.value = depth
    max_borrow_depth_seen = max(max_borrow_depth_seen, depth)
    if depth > 1:
        logger.warning(
            "Nested database connection (depth %s) in thread %r. With "
            "max_size=%s this risks deadlocking under concurrency: finish the "
            "outer query and release before borrowing again.",
            depth,
            threading.current_thread().name,
            DB_POOL_MAX_SIZE,
            stack_info=True,
        )
    try:
        yield
    finally:
        _borrow_depth.value = depth - 1


def _rollback_quietly(conn: psycopg.Connection) -> None:
    """Roll back without masking the error that got us here."""
    try:
        conn.rollback()
    except Exception as exc:  # pragma: no cover - the connection is already gone
        logger.debug("Rollback on a broken connection failed: %s", exc)


def _getconn_with_retry(
    pool: ConnectionPool,
    attempts: int,
    timeout: Optional[float] = None,
) -> psycopg.Connection:
    """Take a connection from the pool, retrying while the database is away."""
    delay = DB_RETRY_BASE_DELAY
    last_error: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            return pool.getconn(timeout=timeout)
        except RETRYABLE_ERRORS as exc:
            if not _is_retryable(exc):
                raise
            last_error = exc
            if attempt == attempts:
                break
            logger.warning(
                "Database unreachable (attempt %s/%s): %s -- retrying in %.1fs",
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, DB_RETRY_MAX_DELAY)

    raise DatabaseUnavailable(
        f"No database connection after {attempts} attempt(s): {last_error}"
    ) from last_error


@contextmanager
def get_connection(
    *,
    attempts: Optional[int] = None,
    timeout: Optional[float] = None,
) -> Iterator[psycopg.Connection]:
    """
    Borrow a validated connection from the pool for one unit of work.

    Commits on a clean exit, rolls back on an exception, and always returns the
    connection to the pool. Only the *acquisition* is retried here: once the
    caller body has started running we cannot safely re-run it, so use
    :func:`run_with_retry` when the whole operation should be retried.

        with get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("SELECT 1")
    """
    pool = get_pool()
    with _track_borrow_depth():
        conn = _getconn_with_retry(
            pool,
            DB_RETRY_ATTEMPTS if attempts is None else attempts,
            timeout,
        )
        try:
            yield conn
        except Exception:
            _rollback_quietly(conn)
            raise
        else:
            try:
                conn.commit()
            except Exception:
                _rollback_quietly(conn)
                raise
        finally:
            # putconn discards a connection it cannot reset, so a broken one
            # never goes back into circulation.
            pool.putconn(conn)


def run_with_retry(
    operation: Callable[[psycopg.Connection], T],
    *,
    attempts: Optional[int] = None,
    timeout: Optional[float] = None,
    label: Optional[str] = None,
) -> T:
    """
    Run ``operation(conn)`` on a pooled connection, retrying the whole
    operation on a fresh connection when the connection itself fails.

    This is what recovers from a database restart mid-request: the first
    statement raises, the dead connection is dropped, and the operation runs
    again on a new one.

    ``operation`` must be safe to run twice. Every caller in this codebase is
    a read or an idempotent upsert.
    """
    attempts = DB_RETRY_ATTEMPTS if attempts is None else attempts
    what = label or getattr(operation, "__name__", "database operation")
    delay = DB_RETRY_BASE_DELAY
    last_error: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            # attempts=1: this loop owns the backoff, so acquisition must not
            # also back off and multiply the total wait.
            with get_connection(attempts=1, timeout=timeout) as conn:
                return operation(conn)
        except RETRYABLE_ERRORS as exc:
            if not _is_retryable(exc):
                raise
            last_error = exc
            if attempt == attempts:
                break
            logger.warning(
                "%s failed on a lost connection (attempt %s/%s): %s -- retrying in %.1fs",
                what,
                attempt,
                attempts,
                exc,
                delay,
            )
            time.sleep(delay)
            delay = min(delay * 2, DB_RETRY_MAX_DELAY)

    raise DatabaseUnavailable(
        f"{what} failed after {attempts} attempt(s): {last_error}"
    ) from last_error


def with_connection(fn: Callable[..., T]) -> Callable[..., T]:
    """
    Give a function a pooled connection as its first argument, with retry.

        @with_connection
        def get_rows(conn, limit):
            ...

        get_rows(10)   # the connection is supplied by the decorator

    For methods, call :func:`run_with_retry` directly: this decorator would
    hand the connection in ahead of ``self``.
    """

    @functools.wraps(fn)
    def wrapper(*args: Any, **kwargs: Any) -> T:
        return run_with_retry(
            lambda conn: fn(conn, *args, **kwargs),
            label=fn.__qualname__,
        )

    return wrapper


# ---------------------------------------------------------------------------
# Health
# ---------------------------------------------------------------------------

def ping() -> None:
    """
    Verify the database over a pooled connection. Raises on failure.

    Uses the same pool the rest of the API uses, so a green health check means
    the chat endpoints can reach the database too. One attempt and a short
    timeout keep the probe fast.
    """

    def _select_one(conn: psycopg.Connection) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    run_with_retry(
        _select_one, attempts=1, timeout=DB_HEALTH_TIMEOUT, label="health check"
    )
