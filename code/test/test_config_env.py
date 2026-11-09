"""
Tests for how config picks the database and dashboard: APP_ENV on Cloud Run,
the instance metadata on the legacy VM, and a local fallback.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402


@pytest.fixture
def no_metadata(monkeypatch):
    """Fail the test if the VM metadata server is consulted."""
    def boom(*a, **k):
        raise AssertionError("metadata server must not be called")

    monkeypatch.setattr(config.requests, "get", boom)


@pytest.mark.parametrize(
    "app_env, expected_db, expected_url",
    [
        ("production", "prod_chat_db", "https://zer0.smalltech.in/dashboard?h=khuljasimsim"),
        ("staging", "staging_chat_db", config.DASHBOARD_URL_STAGING),
        ("local", "chatdb", config.DASHBOARD_URL_STAGING),
    ],
)
def test_app_env_picks_environment(monkeypatch, no_metadata, app_env, expected_db, expected_url):
    monkeypatch.setattr(config, "APP_ENV", app_env)
    monkeypatch.setattr(config, "db_name", "chatdb")
    assert config.get_db_name_and_dashboard_url() == (expected_db, expected_url)


def test_unknown_app_env_is_an_error(monkeypatch, no_metadata):
    monkeypatch.setattr(config, "APP_ENV", "prod")
    with pytest.raises(ValueError):
        config.get_db_name_and_dashboard_url()


class _Resp:
    def __init__(self, status, text=""):
        self.status_code, self.text = status, text


@pytest.mark.parametrize(
    "branch, expected_db",
    [("main", "prod_chat_db"), ("staging", "staging_chat_db")],
)
def test_vm_metadata_still_used_without_app_env(monkeypatch, branch, expected_db):
    monkeypatch.setattr(config, "APP_ENV", "")
    monkeypatch.setattr(config.requests, "get", lambda *a, **k: _Resp(200, branch))
    assert config.get_db_name_and_dashboard_url()[0] == expected_db


def test_cloud_run_without_app_env_falls_back_to_local(monkeypatch):
    """Cloud Run's metadata server has no BRANCH_NAME attribute (404)."""
    monkeypatch.setattr(config, "APP_ENV", "")
    monkeypatch.setattr(config, "db_name", "chatdb")
    monkeypatch.setattr(config.requests, "get", lambda *a, **k: _Resp(404))
    assert config.get_db_name_and_dashboard_url() == ("chatdb", config.DASHBOARD_URL_STAGING)


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


class TestConfigEnvironmentLabel:
    def test_label_follows_app_env(self, monkeypatch):
        for env, db, expected in [("production", "prod_chat_db", None), ("staging", "staging_chat_db", "Staging"), ("local", "chatdb", "Local")]:
            monkeypatch.setattr(config, "APP_ENV", env)
            monkeypatch.setattr(config, "db_name", db)
            assert config.environment_label() == expected

    def test_legacy_deploy_is_told_apart_by_database(self, monkeypatch):
        monkeypatch.setattr(config, "APP_ENV", "")
        monkeypatch.setattr(config, "db_name", "prod_chat_db")
        assert config.environment_label() is None
        monkeypatch.setattr(config, "db_name", "staging_chat_db")
        assert config.environment_label() == "Staging"
        monkeypatch.setattr(config, "db_name", "chatdb")
        assert config.environment_label() == "Local"
