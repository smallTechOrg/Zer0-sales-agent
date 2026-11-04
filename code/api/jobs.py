"""
Endpoints that run background jobs on request.

On Cloud Run an instance has CPU only while it serves a request, so the
in-process scheduler cannot be trusted there. Cloud Scheduler calls these
endpoints on the cron pattern instead, and the job runs inside the request.

The service is public (the chat widget calls it from the browser), so each job
call is authenticated here: it must carry a Google-signed OIDC token whose
audience is config.JOBS_OIDC_AUDIENCE and whose email is one of
config.JOBS_ALLOWED_INVOKERS. Cloud Scheduler adds that token itself when the
job is created with --oidc-service-account-email.
"""
import contextlib
import threading
import traceback
from typing import Iterator

import psycopg
from flask import Blueprint, jsonify, request
from google.auth.transport import requests as google_requests
from google.oauth2 import id_token

import config
from periodic_summary import periodic_summary

jobs_bp = Blueprint("jobs", __name__, url_prefix="/jobs")

# Key of the PostgreSQL advisory lock that keeps two periodic summaries from
# running at once across all Cloud Run instances. Any constant works; it only
# has to be the same everywhere and unused by anything else in the database.
PERIODIC_SUMMARY_LOCK_KEY = 7_340_001

# The same, inside one process: a second request on this instance is refused
# without touching the database.
_local_lock = threading.Lock()

_google_request = google_requests.Request()


class JobAlreadyRunning(Exception):
    """Another periodic summary holds the lock."""


def verify_invoker(auth_header: str) -> str:
    """
    Check the bearer token and return the caller's email. Raise PermissionError
    when the call must be refused.
    """
    if not config.JOBS_OIDC_AUDIENCE or not config.JOBS_ALLOWED_INVOKERS:
        raise PermissionError("job endpoints are not configured")

    scheme, _, token = (auth_header or "").partition(" ")
    if scheme.lower() != "bearer" or not token:
        raise PermissionError("missing bearer token")

    try:
        claims = id_token.verify_oauth2_token(
            token, _google_request, audience=config.JOBS_OIDC_AUDIENCE
        )
    except Exception as exc:  # expired, wrong audience, bad signature, ...
        raise PermissionError(f"invalid token: {exc}") from exc

    email = (claims.get("email") or "").lower()
    if not claims.get("email_verified") or email not in config.JOBS_ALLOWED_INVOKERS:
        raise PermissionError(f"caller {email or '<no email>'} is not allowed")
    return email


@contextlib.contextmanager
def single_run_lock() -> Iterator[None]:
    """
    Hold the periodic summary lock for the block, or raise JobAlreadyRunning.

    The advisory lock lives on its own connection, opened directly and not
    borrowed from the pool: periodic_summary() borrows pooled connections
    while the lock is held, and one request must never hold two pooled
    connections at once (see db_pool). The lock is released when this
    connection closes, also if the instance dies mid-run.
    """
    if not _local_lock.acquire(blocking=False):
        raise JobAlreadyRunning()
    try:
        with psycopg.connect(
            config.DATABASE_URL,
            connect_timeout=config.DB_CONNECT_TIMEOUT,
            application_name=f"{config.DB_APPLICATION_NAME}-job-lock",
            autocommit=True,
        ) as conn:
            got = conn.execute(
                "SELECT pg_try_advisory_lock(%s)", (PERIODIC_SUMMARY_LOCK_KEY,)
            ).fetchone()[0]
            if not got:
                raise JobAlreadyRunning()
            try:
                yield
            finally:
                with contextlib.suppress(Exception):
                    conn.execute(
                        "SELECT pg_advisory_unlock(%s)", (PERIODIC_SUMMARY_LOCK_KEY,)
                    )
    finally:
        _local_lock.release()


@jobs_bp.route("/periodic-summary", methods=["POST"])
def run_periodic_summary():
    """
    Run the periodic chat summary now and answer with its counts.

    200: the run finished (single sessions or Slack may still have failed; the
         counts say so, and they are retried on the next run).
    401: the caller is not allowed.
    409: a run is already in progress; this one is skipped.
    500: the run itself failed.
    503: the database could not be reached to take the lock.
    """
    try:
        caller = verify_invoker(request.headers.get("Authorization", ""))
    except PermissionError as exc:
        print(f"[JOBS] periodic summary refused: {exc}")
        return jsonify({"error": "unauthorized"}), 401

    print(f"[JOBS] periodic summary requested by {caller}")
    try:
        with single_run_lock():
            result = periodic_summary()
    except JobAlreadyRunning:
        print("[JOBS] periodic summary already running; skipped")
        return jsonify({"status": "skipped", "reason": "already running"}), 409
    except psycopg.OperationalError as exc:
        print(f"[JOBS] periodic summary could not reach the database: {exc}")
        return jsonify({"status": "failed", "error": "database unavailable"}), 503
    except Exception as exc:
        print(f"[JOBS] periodic summary failed: {exc}")
        print(traceback.format_exc())
        return jsonify({"status": "failed", "error": str(exc)}), 500

    # Counts only: the sessions themselves hold visitor details.
    counts = {
        key: result.get(key)
        for key in (
            "session_count",
            "summarised_count",
            "llm_failed_count",
            "silent_count",
            "resent_count",
            "notified_count",
        )
    }
    counts["slack_sent"] = (result.get("slack") or {}).get("sent", 0)
    print(f"[JOBS] periodic summary finished: {counts}")
    return jsonify({"status": "ok", **counts}), 200
