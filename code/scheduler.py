"""
Background scheduler for the daily chat summary.

An APScheduler BackgroundScheduler runs in a thread inside the Flask process
and calls daily_summary.daily_summary() on a cron schedule. Settings come
from config:

    DAILY_SUMMARY_CRON      five-field cron pattern, e.g. "0 * * * *"
    DAILY_SUMMARY_TIMEZONE  timezone the pattern is read in

One scheduler per process.
"""
import atexit
import threading
import traceback
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from daily_summary import daily_summary

JOB_ID = "daily_summary"

_scheduler: Optional[BackgroundScheduler] = None
_lock = threading.Lock()


def run_daily_summary_job() -> None:
    """
    The scheduled job. Catches everything: an exception inside a job is only
    logged by APScheduler, and the next run must still happen.
    """
    print("[SCHEDULER] daily summary job started")
    try:
        result = daily_summary()
        print(
            f"[SCHEDULER] daily summary job finished: "
            f"{result['summarised_count']}/{result['session_count']} session(s) summarised, "
            f"slack={result.get('slack')}"
        )
    except Exception as exc:
        print(f"[SCHEDULER] daily summary job failed: {exc}")
        print(traceback.format_exc())


def start_scheduler() -> BackgroundScheduler:
    """
    Start the scheduler with the daily summary job. Safe to call more than
    once: a second call returns the running scheduler.
    """
    global _scheduler

    with _lock:
        if _scheduler is not None and _scheduler.running:
            return _scheduler

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            run_daily_summary_job,
            trigger=CronTrigger.from_crontab(
                config.DAILY_SUMMARY_CRON, timezone=config.DAILY_SUMMARY_TIMEZONE
            ),
            id=JOB_ID,
            name="Daily chat summary",
            # A run that is still going when the next one is due: skip the new
            # one instead of running two summaries at the same time.
            max_instances=1,
            # Several missed runs (the process was paused) become one run.
            coalesce=True,
            misfire_grace_time=60,
            replace_existing=True,
        )
        scheduler.start()
        _scheduler = scheduler

    job = scheduler.get_job(JOB_ID)
    print(
        f"[SCHEDULER] Daily summary job scheduled on '{config.DAILY_SUMMARY_CRON}' "
        f"({config.DAILY_SUMMARY_TIMEZONE}); next run at {job.next_run_time}"
    )
    return scheduler


def stop_scheduler() -> None:
    """Stop the scheduler if it is running. Safe to call more than once."""
    global _scheduler
    with _lock:
        scheduler, _scheduler = _scheduler, None
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        print("[SCHEDULER] stopped")


def get_scheduler() -> Optional[BackgroundScheduler]:
    """The running scheduler, or None."""
    return _scheduler


atexit.register(stop_scheduler)
