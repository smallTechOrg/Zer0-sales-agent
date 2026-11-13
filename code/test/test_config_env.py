"""
Tests for config: settings with no default stop startup when missing, the
environment label, and the password redaction used in logs.
"""
import os
import subprocess
import sys

import pytest

CODE_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), ".."))
sys.path.insert(0, CODE_DIR)

import config  # noqa: E402


def _load_config(code, **settings):
    """Run *code* after importing config in a fresh process with *settings*."""
    return subprocess.run(
        [sys.executable, "-c", f"import config; {code}"],
        cwd=CODE_DIR,
        env={**os.environ, **settings},
        capture_output=True,
        text=True,
    )


@pytest.mark.parametrize("name", ["APP_ENV", "DB_NAME", "DASHBOARD_URL", "DATABASE_URL"])
def test_startup_stops_when_a_setting_is_missing(name):
    result = _load_config("", **{name: " "})
    assert result.returncode != 0
    assert f"{name} is not set" in result.stderr


def test_startup_stops_on_an_unknown_app_env():
    result = _load_config("", APP_ENV="prod")
    assert result.returncode != 0
    assert "APP_ENV='prod' is not one of" in result.stderr


def test_settings_reach_config():
    result = _load_config(
        "print(config.APP_ENV, config.DATABASE_URL.rsplit('/', 1)[1], config.DASHBOARD_URL)",
        APP_ENV="Staging",
        DB_NAME="staging_chat_db",
        DASHBOARD_URL="https://example.com/dashboard?h=x",
    )
    assert result.returncode == 0, result.stderr
    assert result.stdout.splitlines()[-1] == "staging staging_chat_db https://example.com/dashboard?h=x"


@pytest.mark.parametrize(
    "app_env, expected",
    [("production", None), ("staging", "Staging"), ("local", "Local")],
)
def test_environment_label(monkeypatch, app_env, expected):
    monkeypatch.setattr(config, "APP_ENV", app_env)
    assert config.environment_label() == expected


@pytest.mark.parametrize(
    "url, expected",
    [
        ("postgresql://user:s3cret@10.0.0.5:5432/db", "postgresql://user:***@10.0.0.5:5432/db"),
        ("postgresql://user:p@ss@10.0.0.5:5432/db", "postgresql://user:***@10.0.0.5:5432/db"),
        ("postgresql://postgres@localhost:5432/db", "postgresql://postgres@localhost:5432/db"),
    ],
)
def test_password_is_redacted_from_logs(url, expected):
    assert config._redact_password(url) == expected
