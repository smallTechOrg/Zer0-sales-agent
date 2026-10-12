"""
Background scheduler that runs periodic_summary() in a thread of the Flask process,
on config.PERIODIC_SUMMARY_CRON in config.PERIODIC_SUMMARY_TIMEZONE.
"""
import atexit
import threading
import traceback
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from periodic_summary import periodic_summary

JOB_ID = "periodic_summary"

_scheduler: Optional[BackgroundScheduler] = None
_lock = threading.Lock()


def start_scheduler() -> BackgroundScheduler:
    """
    Start the scheduler with the periodic summary job. Safe to call more than
    once: a second call returns the running scheduler.
    """
    global _scheduler

    with _lock:
        if _scheduler is not None and _scheduler.running:
            return _scheduler

        scheduler = BackgroundScheduler(daemon=True)
        scheduler.add_job(
            run_periodic_summary_job,
            trigger=CronTrigger.from_crontab(
                config.PERIODIC_SUMMARY_CRON, timezone=config.PERIODIC_SUMMARY_TIMEZONE
            ),
            id=JOB_ID,
            name="Periodic chat summary",
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
        f"[SCHEDULER] Periodic summary job scheduled on '{config.PERIODIC_SUMMARY_CRON}' "
        f"({config.PERIODIC_SUMMARY_TIMEZONE}); next run at {job.next_run_time}"
    )
    return scheduler


def run_periodic_summary_job() -> None:
    """
    The scheduled job. Catches everything: an exception inside a job is only
    logged by APScheduler, and the next run must still happen.
    """
    print("[SCHEDULER] periodic summary job started")
    try:
        result = periodic_summary()
        print(
            f"[SCHEDULER] periodic summary job finished: "
            f"{result['summarised_count']}/{result['session_count']} session(s) summarised, "
            f"{result.get('resent_count', 0)} resent, "
            f"slack={result.get('slack')}"
        )
    except Exception as exc:
        print(f"[SCHEDULER] periodic summary job failed: {exc}")
        print(traceback.format_exc())


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
