"""
Tests for the helpers in config.
"""
import os
import sys

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402


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
