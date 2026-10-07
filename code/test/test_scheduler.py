"""
The scheduler registers the daily summary job on the configured interval,
can be turned off, starts once per process, and the job swallows errors so
the next run still happens. daily_summary() itself is stubbed here.
"""
import sys
import os

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import scheduler  # noqa: E402


@pytest.fixture(autouse=True)
def fresh_scheduler():
    """Each test starts with no scheduler and leaves none behind."""
    scheduler.stop_scheduler()
    yield
    scheduler.stop_scheduler()


class TestStart:
    def test_registers_job_on_configured_interval(self, monkeypatch):
        monkeypatch.setattr(scheduler.config, "DAILY_SUMMARY_ENABLED", True)
        monkeypatch.setattr(scheduler.config, "DAILY_SUMMARY_INTERVAL_MINUTES", 3)

        sched = scheduler.start_scheduler()

        assert sched is not None and sched.running
        job = sched.get_job(scheduler.JOB_ID)
        assert job is not None
        assert job.func is scheduler.run_daily_summary_job
        assert job.trigger.interval.total_seconds() == 3 * 60
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.next_run_time is not None

    def test_disabled_schedules_nothing(self, monkeypatch):
        monkeypatch.setattr(scheduler.config, "DAILY_SUMMARY_ENABLED", False)
        assert scheduler.start_scheduler() is None
        assert scheduler.get_scheduler() is None

    def test_second_start_returns_same_scheduler(self, monkeypatch):
        monkeypatch.setattr(scheduler.config, "DAILY_SUMMARY_ENABLED", True)
        first = scheduler.start_scheduler()
        second = scheduler.start_scheduler()
        assert first is second
        assert len(first.get_jobs()) == 1

    def test_stop_is_idempotent(self, monkeypatch):
        monkeypatch.setattr(scheduler.config, "DAILY_SUMMARY_ENABLED", True)
        sched = scheduler.start_scheduler()
        scheduler.stop_scheduler()
        scheduler.stop_scheduler()
        assert not sched.running
        assert scheduler.get_scheduler() is None


class TestJob:
    def test_job_calls_daily_summary(self, monkeypatch):
        calls = []

        def fake_daily_summary():
            calls.append(True)
            return {"summarised_count": 2, "session_count": 3, "slack": {"sent": 1}}

        monkeypatch.setattr(scheduler, "daily_summary", fake_daily_summary)
        scheduler.run_daily_summary_job()
        assert calls == [True]

    def test_job_swallows_errors(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(scheduler, "daily_summary", boom)
        # Must not raise: APScheduler would log it, and the next run still has
        # to happen either way, but the log line here is ours.
        scheduler.run_daily_summary_job()
