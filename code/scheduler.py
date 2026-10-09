"""
Background scheduler that runs periodic_summary() in a thread of the Flask process,
on config.PERIODIC_SUMMARY_CRON in config.PERIODIC_SUMMARY_TIMEZONE.
"""
import atexit
import logging
import threading
from typing import Optional

from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.cron import CronTrigger

import config
from periodic_summary import periodic_summary

log = logging.getLogger(__name__)

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
    log.info(
        "Periodic summary job scheduled on '%s' (%s); next run at %s",
        config.PERIODIC_SUMMARY_CRON,
        config.PERIODIC_SUMMARY_TIMEZONE,
        job.next_run_time,
    )
    return scheduler


def run_periodic_summary_job() -> None:
    """
    The scheduled job. Catches everything: an exception inside a job is only
    logged by APScheduler, and the next run must still happen.
    """
    log.info("periodic summary job started")
    try:
        result = periodic_summary()
        log.info(
            "periodic summary job finished: %s/%s session(s) summarised, "
            "%s resent, slack=%s",
            result["summarised_count"],
            result["session_count"],
            result.get("resent_count", 0),
            result.get("slack"),
        )
    except Exception:
        log.exception("periodic summary job failed")


def stop_scheduler() -> None:
    """Stop the scheduler if it is running. Safe to call more than once."""
    global _scheduler
    with _lock:
        scheduler, _scheduler = _scheduler, None
    if scheduler is not None and scheduler.running:
        scheduler.shutdown(wait=False)
        log.info("scheduler stopped")


def get_scheduler() -> Optional[BackgroundScheduler]:
    """The running scheduler, or None."""
    return _scheduler


def _stop_scheduler_at_exit() -> None:
    """
    Stop the scheduler without logging. Python can close the output stream
    before it runs the atexit handlers, and a log line then fails and reports
    itself on stderr. APScheduler logs inside shutdown() too, so the whole
    call is silenced rather than only our own line. db_pool keeps its atexit
    handler quiet for the same reason.
    """
    logging.disable(logging.CRITICAL)
    stop_scheduler()


atexit.register(_stop_scheduler_at_exit)
