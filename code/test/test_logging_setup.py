"""
Tests for the logging configuration: the format a record comes out in, where
the level comes from, and that the database password never reaches a log line.
"""
import sys
import os
import io
import logging

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import logging_setup  # noqa: E402


@pytest.fixture
def fresh_logging(monkeypatch):
    """
    Let a test call configure_logging() as if it were the first time, and put
    the real configuration back afterwards. Importing app already configured
    logging for the rest of the suite.
    """
    root = logging.getLogger()
    saved_handlers = list(root.handlers)
    saved_level = root.level
    saved_noisy = {n: logging.getLogger(n).level for n in logging_setup.NOISY_LOGGERS}

    monkeypatch.setattr(logging_setup, "_configured", False)
    monkeypatch.delenv("LOG_LEVEL", raising=False)
    try:
        yield
    finally:
        for handler in list(root.handlers):
            root.removeHandler(handler)
        for handler in saved_handlers:
            root.addHandler(handler)
        root.setLevel(saved_level)
        for name, level in saved_noisy.items():
            logging.getLogger(name).setLevel(level)


def _configure_to_buffer(**kwargs):
    buffer = io.StringIO()
    logging_setup.configure_logging(stream=buffer, **kwargs)
    return buffer


# ---------------------------------------------------------------------------
# redact_url
# ---------------------------------------------------------------------------

@pytest.mark.parametrize(
    "url, expected",
    [
        # The deployed DATABASE_URL shape: the password must not survive.
        (
            "postgresql://postgres:s3cret@10.0.0.4:5432/staging_chat_db",
            "postgresql://postgres:***@10.0.0.4:5432/staging_chat_db",
        ),
        # A password may itself contain an encoded '@'.
        ("postgresql://u:p%40ss@host/db", "postgresql://u:***@host/db"),
        # Nothing to hide: returned unchanged.
        ("postgresql://host:5432/db", "postgresql://host:5432/db"),
        ("postgres://user@host/db", "postgres://user@host/db"),
        ("", ""),
        (None, None),
    ],
)
def test_redact_url(url, expected):
    assert logging_setup.redact_url(url) == expected


def test_redacted_url_does_not_contain_the_password():
    assert "s3cret" not in logging_setup.redact_url(
        "postgresql://postgres:s3cret@10.0.0.4:5432/db"
    )


# ---------------------------------------------------------------------------
# configure_logging
# ---------------------------------------------------------------------------

def test_record_carries_time_level_and_logger_name(fresh_logging):
    buffer = _configure_to_buffer()
    logging.getLogger("db_pool").info("Database pool opened")

    line = buffer.getvalue().strip()
    # 2026-10-09 17:32:47 INFO     db_pool  Database pool opened
    assert line.endswith("INFO     db_pool  Database pool opened")
    assert line[:4].isdigit()


def test_level_comes_from_the_environment(fresh_logging, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "WARNING")
    buffer = _configure_to_buffer()

    log = logging.getLogger("db")
    log.info("schema ready")
    log.warning("bootstrap attempt failed")

    output = buffer.getvalue()
    assert "schema ready" not in output
    assert "bootstrap attempt failed" in output


def test_an_explicit_level_wins_over_the_environment(fresh_logging, monkeypatch):
    monkeypatch.setenv("LOG_LEVEL", "ERROR")
    buffer = _configure_to_buffer(level="DEBUG")

    logging.getLogger("conversation_processor").debug("Using prompt: ...")
    assert "Using prompt: ..." in buffer.getvalue()


def test_an_unknown_level_falls_back_instead_of_raising(fresh_logging, monkeypatch):
    # A typo in .env must not stop the app from starting.
    monkeypatch.setenv("LOG_LEVEL", "LOUD")
    buffer = _configure_to_buffer()

    assert logging.getLogger().level == logging.INFO
    logging.getLogger("db").info("schema ready")
    assert "schema ready" in buffer.getvalue()


def test_calling_it_twice_does_not_log_every_line_twice(fresh_logging):
    buffer = _configure_to_buffer()
    logging_setup.configure_logging()

    assert len(logging.getLogger().handlers) == 1
    logging.getLogger("scheduler").info("periodic summary job started")
    assert buffer.getvalue().count("periodic summary job started") == 1


def test_noisy_libraries_stay_at_warning_on_debug(fresh_logging):
    _configure_to_buffer(level="DEBUG")

    # DEBUG on urllib3 is a line per connection, with headers.
    for name in logging_setup.NOISY_LOGGERS:
        assert logging.getLogger(name).level == logging.WARNING


def test_exception_renders_the_traceback(fresh_logging):
    buffer = _configure_to_buffer()

    try:
        raise ValueError("no summary")
    except ValueError:
        logging.getLogger("periodic_summary").exception("session %s failed", "abc")

    output = buffer.getvalue()
    assert "session abc failed" in output
    assert "Traceback (most recent call last):" in output
    assert "ValueError: no summary" in output
