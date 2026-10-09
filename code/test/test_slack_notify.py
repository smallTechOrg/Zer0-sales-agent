"""
slack_notify turns a periodic_summary() result into the plain-text message

    <dashboard url|Zer0 Chat Summary - 7th Oct 2026 - 5 pm IST>
    4:13 pm - SMALLTECH - Anjali - Enquiry for AI training in Bangalore.
    ...

and POSTs it to the webhook. The HTTP call is stubbed; nothing reaches Slack.
"""
import sys
import os
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import slack_notify  # noqa: E402

IST = ZoneInfo("Asia/Kolkata")
RUN_AT = datetime(2026, 10, 7, 17, 0, tzinfo=IST)
DASHBOARD = "https://zero.smalltech.in/dashboard?h=test-secret"
HEADER = f"<{DASHBOARD}|Zer0 Chat Summary - 7th Oct 2026 - 5 pm IST>"


@pytest.fixture(autouse=True)
def fixed_config(monkeypatch):
    monkeypatch.setattr(slack_notify.config, "PERIODIC_SUMMARY_TIMEZONE", "Asia/Kolkata")
    monkeypatch.setattr(slack_notify.config, "DASHBOARD_URL", DASHBOARD)


def _session(i, **extra):
    s = {
        "session_id": f"00000000-0000-0000-0000-{i:012d}",
        "message_count": 3,
        "first_message": datetime(2026, 10, 7, 16, 10 + i, tzinfo=IST),
        "conversation": [],
        "summary": f"Summary {i}.",
        "lead": {
            "session_id": f"00000000-0000-0000-0000-{i:012d}",
            "name": f"Lead {i}",
            "domain": "SMALLTECH",
            "website": f"site{i}.example.com",
        },
    }
    s.update(extra)
    return s


def _result(sessions, run_at=RUN_AT):
    return {
        "run_at": run_at,
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
        (datetime(2026, 10, 7, 17, 0, tzinfo=IST), "7th Oct 2026 - 5 pm IST"),
        (datetime(2026, 10, 1, 0, 5, tzinfo=IST), "1st Oct 2026 - 12:05 am IST"),
        (datetime(2026, 10, 2, 9, 30, tzinfo=IST), "2nd Oct 2026 - 9:30 am IST"),
        (datetime(2026, 10, 3, 12, 0, tzinfo=IST), "3rd Oct 2026 - 12 pm IST"),
        (datetime(2026, 10, 11, 23, 59, tzinfo=IST), "11th Oct 2026 - 11:59 pm IST"),
        (datetime(2026, 10, 22, 15, 0, tzinfo=IST), "22nd Oct 2026 - 3 pm IST"),
        (datetime(2026, 11, 13, 1, 0, tzinfo=IST), "13th Nov 2026 - 1 am IST"),
    ])
    def test_header_time(self, value, expected):
        assert slack_notify.format_header_time(value) == expected

    @pytest.mark.parametrize("value, expected", [
        (datetime(2026, 10, 7, 16, 13, tzinfo=IST), "4:13 pm"),
        (datetime(2026, 10, 7, 16, 0, tzinfo=IST), "4 pm"),
        (datetime(2026, 10, 7, 0, 7, tzinfo=IST), "12:07 am"),
    ])
    def test_line_time(self, value, expected):
        assert slack_notify.format_line_time(value) == expected

    def test_database_utc_timestamp_is_shown_in_ist(self):
        # 10:43 UTC is 4:13 pm in Kolkata.
        utc = datetime(2026, 10, 7, 10, 43, tzinfo=timezone.utc)
        assert slack_notify.format_line_time(utc) == "4:13 pm"

    def test_naive_timestamp_is_taken_as_display_timezone(self):
        assert slack_notify.format_header_time(datetime(2026, 10, 7, 17, 0)) == "7th Oct 2026 - 5 pm IST"


class TestBuildMessages:
    def test_no_sessions_gives_no_messages(self):
        assert slack_notify.build_messages(_result([])) == []

    def test_exact_format(self):
        [message] = slack_notify.build_messages(_result([_session(1), _session(2)]))
        assert message == {
            "text": (
                f"{HEADER}\n"
                "4:11 pm - SMALLTECH - Lead 1 - Summary 1.\n"
                "4:12 pm - SMALLTECH - Lead 2 - Summary 2."
            )
        }

    def test_header_is_a_dashboard_link(self):
        [message] = slack_notify.build_messages(_result([_session(1)]))
        header = message["text"].split("\n")[0]
        assert header.startswith(f"<{DASHBOARD}|")
        assert header.endswith(">")

    def test_header_without_run_time(self):
        result = _result([_session(1)], run_at=None)
        [message] = slack_notify.build_messages(result)
        assert message["text"].startswith(f"<{DASHBOARD}|Zer0 Chat Summary>\n")

    def test_missing_name_is_unknown(self):
        blank = _session(1)
        blank["lead"]["name"] = "   "
        none = _session(2)
        none["lead"]["name"] = None
        no_lead = _session(3, lead=None)
        [message] = slack_notify.build_messages(_result([blank, none, no_lead]))
        lines = message["text"].split("\n")[1:]
        assert lines[0] == "4:11 pm - SMALLTECH - Unknown - Summary 1."
        assert lines[1] == "4:12 pm - SMALLTECH - Unknown - Summary 2."
        assert lines[2] == "4:13 pm - UNKNOWN - Unknown - Summary 3."

    def test_domain_key_is_upper_cased(self):
        s = _session(1)
        s["lead"]["domain"] = "silverwave"
        [message] = slack_notify.build_messages(_result([s]))
        assert "4:11 pm - SILVERWAVE - Lead 1 - Summary 1." in message["text"]

    def test_multiline_summary_is_flattened(self):
        [message] = slack_notify.build_messages(_result([_session(1, summary="Line one.\nLine two.")]))
        assert "4:11 pm - SMALLTECH - Lead 1 - Line one. Line two." in message["text"]

    def test_failed_session_shows_the_error(self):
        # A failure that was not the LLM (saving the summary, say) keeps its
        # own line, so the reader knows which conversation it was.
        session = _session(1)
        del session["summary"]
        session["error"] = "db down"
        [message] = slack_notify.build_messages(_result([session]))
        assert "4:11 pm - SMALLTECH - Lead 1 - summary failed (db down)" in message["text"]

    def test_silent_session_gets_no_line(self):
        silent = _session(2, silent=True, summary="Opened the chat but did not write anything.")
        [message] = slack_notify.build_messages(_result([_session(1), silent]))
        assert message == {"text": f"{HEADER}\n4:11 pm - SMALLTECH - Lead 1 - Summary 1."}

    def test_only_silent_sessions_give_no_messages(self):
        silent = _session(1, silent=True, summary="Opened the chat but did not write anything.")
        assert slack_notify.build_messages(_result([silent])) == []

    def test_silent_sessions_do_not_count_as_failures(self):
        # One silent, one LLM failure: every reportable session failed, so
        # the sentence has no count.
        silent = _session(1, silent=True, summary="Opened the chat but did not write anything.")
        bad = _session(2)
        del bad["summary"]
        bad["error"] = bad["llm_error"] = "rate_limit_exceeded"
        [message] = slack_notify.build_messages(_result([silent, bad]))
        assert message["text"] == (
            f"{HEADER}\n"
            "Failed to generate a summary. LLM calls failed with error: rate_limit_exceeded."
        )

    def test_llm_failure_exact_format(self):
        session = _session(1)
        del session["summary"]
        session["error"] = "Error code: 429 - {...}"
        session["llm_error"] = "RESOURCE_EXHAUSTED"
        run_at = datetime(2026, 10, 7, 18, 0, tzinfo=IST)
        [message] = slack_notify.build_messages(_result([session], run_at=run_at))
        assert message == {
            "text": (
                f"<{DASHBOARD}|Zer0 Chat Summary - 7th Oct 2026 - 6 pm IST>\n"
                "Failed to generate a summary. LLM calls failed with error: RESOURCE_EXHAUSTED."
            )
        }

    def test_partial_llm_failure_lists_the_good_ones_then_the_count(self):
        bad = _session(2)
        del bad["summary"]
        bad["error"] = bad["llm_error"] = "rate_limit_exceeded"
        worse = _session(3)
        del worse["summary"]
        worse["error"] = worse["llm_error"] = "rate_limit_exceeded"
        [message] = slack_notify.build_messages(_result([_session(1), bad, worse]))
        assert message["text"] == (
            f"{HEADER}\n"
            "4:11 pm - SMALLTECH - Lead 1 - Summary 1.\n"
            "Failed to generate a summary for 2 conversations. "
            "LLM calls failed with error: rate_limit_exceeded."
        )

    def test_single_partial_llm_failure_is_singular(self):
        bad = _session(2)
        del bad["summary"]
        bad["error"] = bad["llm_error"] = "model_not_found"
        [message] = slack_notify.build_messages(_result([_session(1), bad]))
        assert message["text"].endswith(
            "Failed to generate a summary for 1 conversation. "
            "LLM calls failed with error: model_not_found."
        )

    def test_distinct_llm_errors_are_listed_once_each(self):
        sessions = []
        for i, error in enumerate(["rate_limit_exceeded", "timeout", "rate_limit_exceeded"], start=1):
            s = _session(i)
            del s["summary"]
            s["error"] = s["llm_error"] = error
            sessions.append(s)
        [message] = slack_notify.build_messages(_result(sessions))
        assert message["text"].endswith("LLM calls failed with error: rate_limit_exceeded; timeout.")

    def test_llm_error_text_is_escaped(self):
        s = _session(1)
        del s["summary"]
        s["error"] = s["llm_error"] = "<bad & worse>"
        [message] = slack_notify.build_messages(_result([s]))
        assert "&lt;bad &amp; worse&gt;" in message["text"]

    def test_missing_first_message_time(self):
        s = _session(1)
        del s["first_message"]
        [message] = slack_notify.build_messages(_result([s]))
        assert "unknown time - SMALLTECH - Lead 1 - Summary 1." in message["text"]

    def test_slack_markup_characters_are_escaped(self):
        s = _session(1, summary="Asked about <pricing> & plans.")
        [message] = slack_notify.build_messages(_result([s]))
        assert "Asked about &lt;pricing&gt; &amp; plans." in message["text"]
        # The header link itself must stay unescaped.
        assert message["text"].startswith(HEADER)

    def test_long_run_is_split_and_header_repeats(self):
        sessions = [_session(i, summary="x" * 2500) for i in range(20)]
        messages = slack_notify.build_messages(_result(sessions))

        assert len(messages) > 1
        for message in messages:
            assert message["text"].startswith(HEADER)
            assert len(message["text"]) <= slack_notify.MESSAGE_LIMIT
        # Every conversation appears exactly once across the parts.
        lines = [line for m in messages for line in m["text"].split("\n")[1:]]
        assert len(lines) == 20

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

    def test_skips_when_only_silent_sessions(self, posted, monkeypatch):
        monkeypatch.setattr(slack_notify.config, "SLACK_WEBHOOK_URL", "https://hooks.slack.com/x")
        silent = _session(1, silent=True, summary="Opened the chat but did not write anything.")
        outcome = slack_notify.send_summaries_to_slack(_result([silent]))
        assert outcome == {"sent": 0, "skipped": "nothing to report"}
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
        assert posted[0]["json"]["text"].startswith(f"{HEADER}\n")

    def test_raises_on_slack_error(self, monkeypatch):
        class _Bad:
            ok = False
            status_code = 400
            text = "invalid_payload"

        monkeypatch.setattr(slack_notify.config, "SLACK_WEBHOOK_URL", "https://hooks.slack.test/abc")
        monkeypatch.setattr(slack_notify.requests, "post", lambda *a, **k: _Bad())
        with pytest.raises(RuntimeError, match="400"):
            slack_notify.send_summaries_to_slack(_result([_session(1)]))
