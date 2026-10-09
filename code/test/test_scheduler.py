"""
The scheduler registers the periodic summary job on the configured cron pattern,
starts once per process, and the job swallows errors so
the next run still happens. periodic_summary() itself is stubbed here.
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
    def test_registers_job_on_configured_cron_pattern(self, monkeypatch):
        monkeypatch.setattr(scheduler.config, "PERIODIC_SUMMARY_CRON", "30 9 * * *")
        monkeypatch.setattr(scheduler.config, "PERIODIC_SUMMARY_TIMEZONE", "Asia/Kolkata")

        sched = scheduler.start_scheduler()

        assert sched is not None and sched.running
        job = sched.get_job(scheduler.JOB_ID)
        assert job is not None
        assert job.func is scheduler.run_periodic_summary_job
        # The pattern is read in the configured timezone, not the server's.
        assert job.next_run_time.hour == 9
        assert job.next_run_time.minute == 30
        assert str(job.next_run_time.tzinfo) == "Asia/Kolkata"
        assert job.max_instances == 1
        assert job.coalesce is True
        assert job.next_run_time is not None

    def test_second_start_returns_same_scheduler(self):
        first = scheduler.start_scheduler()
        second = scheduler.start_scheduler()
        assert first is second
        assert len(first.get_jobs()) == 1

    def test_stop_is_idempotent(self):
        sched = scheduler.start_scheduler()
        scheduler.stop_scheduler()
        scheduler.stop_scheduler()
        assert not sched.running
        assert scheduler.get_scheduler() is None


class TestJob:
    def test_job_calls_periodic_summary(self, monkeypatch):
        calls = []

        def fake_periodic_summary():
            calls.append(True)
            return {"summarised_count": 2, "session_count": 3, "slack": {"sent": 1}}

        monkeypatch.setattr(scheduler, "periodic_summary", fake_periodic_summary)
        scheduler.run_periodic_summary_job()
        assert calls == [True]

    def test_job_swallows_errors(self, monkeypatch):
        def boom():
            raise RuntimeError("db down")

        monkeypatch.setattr(scheduler, "periodic_summary", boom)
        # Must not raise: APScheduler would log it, and the next run still has
        # to happen either way, but the log line here is ours.
        scheduler.run_periodic_summary_job()
