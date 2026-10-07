"""
slack_notify turns a daily_summary() result into the plain-text message

    Conversation From 5th Oct 12:00 PM - 6th Oct 12:00 PM
    Conversation 1: <summary>, <website>
    ...

and POSTs it to the webhook. The HTTP call is stubbed; nothing reaches Slack.
"""
import sys
import os
from datetime import datetime

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import slack_notify  # noqa: E402

START = datetime(2026, 10, 5, 12, 0)
END = datetime(2026, 10, 6, 12, 0)


def _session(i, **extra):
    s = {
        "session_id": f"00000000-0000-0000-0000-{i:012d}",
        "message_count": 3,
        "conversation": [],
        "summary": f"Summary {i}",
        "lead": {
            "session_id": f"00000000-0000-0000-0000-{i:012d}",
            "name": f"Lead {i}",
            "domain": "COMMON",
            "website": f"site{i}.example.com",
        },
    }
    s.update(extra)
    return s


def _result(sessions, start=START, end=END):
    return {
        "window_hours": 24.0,
        "window_start": start,
        "window_end": end,
        "session_count": len(sessions),
        "summarised_count": sum(1 for s in sessions if "summary" in s),
        "sessions": sessions,
    }


@pytest.fixture
def posted(monkeypatch):
    """Capture every webhook POST instead of sending it."""
    calls = []

    class _Response:
        ok = True
        status_code = 200
        text = "ok"

    def fake_post(url, json=None, timeout=None):
        calls.append({"url": url, "json": json, "timeout": timeout})
        return _Response()

    monkeypatch.setattr(slack_notify.requests, "post", fake_post)
    return calls


class TestFormatTime:
    @pytest.mark.parametrize("value, expected", [
        (datetime(2026, 10, 5, 12, 0), "5th Oct 12:00 PM"),
        (datetime(2026, 10, 1, 0, 5), "1st Oct 12:05 AM"),
        (datetime(2026, 10, 2, 9, 30), "2nd Oct 9:30 AM"),
        (datetime(2026, 10, 3, 15, 0), "3rd Oct 3:00 PM"),
        (datetime(2026, 10, 11, 23, 59), "11th Oct 11:59 PM"),
        (datetime(2026, 10, 22, 12, 0), "22nd Oct 12:00 PM"),
        (datetime(2026, 11, 13, 1, 0), "13th Nov 1:00 AM"),
    ])
    def test_ordinal_month_and_12_hour_clock(self, value, expected):
        assert slack_notify.format_time(value) == expected


class TestBuildMessages:
    def test_no_sessions_gives_no_messages(self):
        assert slack_notify.build_messages(_result([])) == []

    def test_exact_format(self):
        [message] = slack_notify.build_messages(_result([_session(1), _session(2)]))
        assert message == {
            "text": (
                "Conversation From 5th Oct 12:00 PM - 6th Oct 12:00 PM\n"
                "Conversation 1: Summary 1, site1.example.com\n"
                "Conversation 2: Summary 2, site2.example.com"
            )
        }

    def test_multiline_summary_is_flattened(self):
        [message] = slack_notify.build_messages(_result([_session(1, summary="Line one.\nLine two.")]))
        assert "Conversation 1: Line one. Line two., site1.example.com" in message["text"]

    def test_failed_session_shows_the_error(self):
        session = _session(1)
        del session["summary"]
        session["error"] = "llm down"
        [message] = slack_notify.build_messages(_result([session]))
        assert "Conversation 1: summary failed (llm down), site1.example.com" in message["text"]

    def test_falls_back_to_domain_key_then_unknown(self):
        no_site = _session(1)
        no_site["lead"]["website"] = None
        no_lead = _session(2, lead=None)
        [message] = slack_notify.build_messages(_result([no_site, no_lead]))
        assert "Conversation 1: Summary 1, COMMON" in message["text"]
        assert "Conversation 2: Summary 2, unknown website" in message["text"]

    def test_header_without_window_times(self):
        result = _result([_session(1)], start=None, end=None)
        [message] = slack_notify.build_messages(result)
        assert message["text"].startswith("Conversation From the last 24 hours\n")

    def test_long_day_is_split_and_header_repeats(self):
        sessions = [_session(i, summary="x" * 2500) for i in range(20)]
        messages = slack_notify.build_messages(_result(sessions))

        assert len(messages) > 1
        header = "Conversation From 5th Oct 12:00 PM - 6th Oct 12:00 PM"
        for message in messages:
            assert message["text"].startswith(header)
            assert len(message["text"]) <= slack_notify.MESSAGE_LIMIT
        # Numbering continues across parts and every conversation appears once.
        numbers = [
            int(line.split(":")[0].split()[1])
            for m in messages for line in m["text"].split("\n")[1:]
        ]
        assert numbers == list(range(1, 21))

    def test_long_summary_is_clipped(self):
        [message] = slack_notify.build_messages(_result([_session(1, summary="x" * 5000)]))
        line = message["text"].split("\n")[1]
        assert len(line) <= slack_notify.LINE_LIMIT


class TestSend:
    def test_skips_when_there_are_no_sessions(self, posted, monkeypatch):
        monkeypatch.setattr(slack_notify.config, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")
        outcome = slack_notify.send_summaries_to_slack(_result([]))
        assert outcome == {"sent": 0, "skipped": "no sessions"}
        assert posted == []

    def test_skips_when_webhook_not_set(self, posted, monkeypatch):
        monkeypatch.setattr(slack_notify.config, "SLACK_WEBHOOK_URL", None)
        outcome = slack_notify.send_summaries_to_slack(_result([_session(1)]))
        assert outcome["sent"] == 0
        assert "skipped" in outcome
        assert posted == []

    def test_posts_the_message_to_the_webhook(self, posted, monkeypatch):
        monkeypatch.setattr(slack_notify.config, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")
        monkeypatch.setattr(slack_notify.config, "SLACK_TIMEOUT", 7.0)
        outcome = slack_notify.send_summaries_to_slack(_result([_session(1), _session(2)]))

        assert outcome == {"sent": 1}
        assert len(posted) == 1
        assert posted[0]["url"] == "https://hooks.slack.test/abc"
        assert posted[0]["timeout"] == 7.0
        assert posted[0]["json"]["text"].startswith("Conversation From 5th Oct 12:00 PM - 6th Oct 12:00 PM\n")

    def test_raises_on_slack_error(self, monkeypatch):
        class _Bad:
            ok = False
            status_code = 400
            text = "invalid_payload"

        monkeypatch.setattr(slack_notify.config, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")
        monkeypatch.setattr(slack_notify.requests, "post", lambda *a, **k: _Bad())
        with pytest.raises(RuntimeError, match="400"):
            slack_notify.send_summaries_to_slack(_result([_session(1)]))
