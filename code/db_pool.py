"""
Shared PostgreSQL connection pool. All database access uses it, including
/health. The pool tests each connection before use, replaces old connections,
and retries when the database is not available.
"""
from __future__ import annotations

import atexit
import functools
import signal
import traceback
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

T = TypeVar("T")

# Settings come from config.py. This module does not read the environment.

# Retry these. The server stopped, restarted, or the pool gave no connection.
# All psycopg_pool errors are OperationalError subclasses. Do not retry a
# programming error: the same SQL fails again.
RETRYABLE_ERRORS = (psycopg.OperationalError, psycopg.InterfaceError)

# Do not retry our own timeouts. QueryCanceled (57014) means the database is
# slow. LockNotAvailable (55P03) means another process holds the lock. A retry
# adds load or waits again.
NON_RETRYABLE_ERRORS = (
    psycopg.errors.QueryCanceled,
    psycopg.errors.LockNotAvailable,
)


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
    print(
        f"Pool {pool.name!r} could not reconnect within {DB_RECONNECT_TIMEOUT}s. "
        "The app stays up and keeps retrying; /health reports the database as "
        "down until it succeeds."
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
        # Test the connection first. Replace a connection that a restart killed.
        check=ConnectionPool.check_connection,
        kwargs={
            "connect_timeout": DB_CONNECT_TIMEOUT,
            "application_name": DB_APPLICATION_NAME,
            # Server limits for one query and one transaction.
            "options": (
                f"-c statement_timeout={DB_STATEMENT_TIMEOUT_MS} "
                f"-c lock_timeout={DB_LOCK_TIMEOUT_MS} "
                f"-c idle_in_transaction_session_timeout={DB_IDLE_TX_TIMEOUT_MS}"
            ),
            # Kernel limits, for a server that stops to answer.
            "keepalives": 1,
            "keepalives_idle": DB_KEEPALIVES_IDLE,
            "keepalives_interval": DB_KEEPALIVES_INTERVAL,
            "keepalives_count": DB_KEEPALIVES_COUNT,
            "tcp_user_timeout": DB_TCP_USER_TIMEOUT_MS,
        },
        name="chatdb",
        open=False,
    )
    # wait=False: the app must start when the database is down. /health then
    # reports the fault.
    pool.open(wait=False)
    print(
        f"Database pool opened (min={DB_POOL_MIN_SIZE} max={DB_POOL_MAX_SIZE} "
        f"timeout={DB_POOL_TIMEOUT}s)"
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
        print("Database pool closed")


def _close_pool_at_exit() -> None:
    """Release the connections. Silent: the output stream can be closed."""
    pool = _take_pool()
    if pool is not None:
        pool.close()


def _handle_sigterm(signum, frame):
    """
    Close the pool when deploy.sh stops the app. Python does not run atexit
    handlers on SIGTERM. SIGINT needs no handler.
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

# A request must not take a second connection while it holds the first. With
# two connections, two such requests wait for each other. The test uses this
# counter.
_borrow_depth = threading.local()
max_borrow_depth_seen = 0


@contextmanager
def _track_borrow_depth() -> Iterator[None]:
    global max_borrow_depth_seen
    depth = getattr(_borrow_depth, "value", 0) + 1
    _borrow_depth.value = depth
    max_borrow_depth_seen = max(max_borrow_depth_seen, depth)
    if depth > 1:
        print(
            f"Nested database connection (depth {depth}) in thread "
            f"{threading.current_thread().name!r}. With "
            f"max_size={DB_POOL_MAX_SIZE} this risks deadlocking under "
            "concurrency: finish the outer query and release before borrowing "
            "again."
        )
        print("".join(traceback.format_stack()))
    try:
        yield
    finally:
        _borrow_depth.value = depth - 1


def _rollback_quietly(conn: psycopg.Connection) -> None:
    """Roll back without masking the error that got us here."""
    try:
        conn.rollback()
    except Exception:  # pragma: no cover - the connection is already gone
        pass


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
            print(
                f"Database unreachable (attempt {attempt}/{attempts}): {exc} "
                f"-- retrying in {delay:.1f}s"
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
    Take one connection from the pool. Commit on success, roll back on an
    error, then return the connection. This retries only the request for a
    connection. Use run_with_retry to retry the operation.
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
            # putconn discards a connection that it cannot reset.
            pool.putconn(conn)


def run_with_retry(
    operation: Callable[[psycopg.Connection], T],
    *,
    attempts: Optional[int] = None,
    timeout: Optional[float] = None,
    label: Optional[str] = None,
) -> T:
    """
    Run operation(conn) on a pooled connection. Retry on a new connection if
    the connection fails. operation must be safe to run two times.
    """
    attempts = DB_RETRY_ATTEMPTS if attempts is None else attempts
    what = label or getattr(operation, "__name__", "database operation")
    delay = DB_RETRY_BASE_DELAY
    last_error: Optional[BaseException] = None

    for attempt in range(1, attempts + 1):
        try:
            # attempts=1: this loop applies the delay.
            with get_connection(attempts=1, timeout=timeout) as conn:
                return operation(conn)
        except RETRYABLE_ERRORS as exc:
            if not _is_retryable(exc):
                raise
            last_error = exc
            if attempt == attempts:
                break
            print(
                f"{what} failed on a lost connection (attempt {attempt}/"
                f"{attempts}): {exc} -- retrying in {delay:.1f}s"
            )
            time.sleep(delay)
            delay = min(delay * 2, DB_RETRY_MAX_DELAY)

    raise DatabaseUnavailable(
        f"{what} failed after {attempts} attempt(s): {last_error}"
    ) from last_error


def with_connection(fn: Callable[..., T]) -> Callable[..., T]:
    """
    Give a function a pooled connection as the first argument, with retry. For
    a method, use run_with_retry: this decorator puts the connection before
    self.
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
    Test the database through the pool. Raise on failure. One attempt and a
    short timeout keep the probe fast.
    """

    def _select_one(conn: psycopg.Connection) -> None:
        with conn.cursor() as cur:
            cur.execute("SELECT 1")
            cur.fetchone()

    run_with_retry(
        _select_one, attempts=1, timeout=DB_HEALTH_TIMEOUT, label="health check"
    )
