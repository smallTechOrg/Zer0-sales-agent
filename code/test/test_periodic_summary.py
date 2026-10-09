"""
Tests for the periodic summary chain against the real schema.
The LLM call is stubbed, so no Groq key is needed.
"""
import sys
import os
import uuid
from datetime import timedelta

import pytest

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import config  # noqa: E402
import db_pool  # noqa: E402
from psycopg import sql  # noqa: E402
from psycopg.rows import dict_row  # noqa: E402


def _insert_message(session_id, age, msg_type="human", content="hi"):
    """Write one chat_table row whose created_at is *age* ago."""
    query = sql.SQL(
        "INSERT INTO {table} (session_id, message, created_at) "
        "VALUES (%s, %s::jsonb, NOW() - %s::interval);"
    ).format(table=sql.Identifier(config.table_name))
    message = '{"type": "%s", "data": {"content": "%s", "type": "%s"}}' % (msg_type, content, msg_type)
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (session_id, message, age))


def _chat_info_row(session_id):
    with db_pool.get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT contact_name, summary, summary_generated_at, summary_notified_at "
                "FROM chat_info WHERE session_id = %s;",
                (session_id,),
            )
            return cur.fetchone()


def _mark_summarised(session_id, generated_age, notified=True):
    """
    Give a session a chat_info row that looks like an earlier run summarised
    it *generated_age* ago and, unless notified=False, sent it to Slack.
    """
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                """
                INSERT INTO chat_info (session_id, summary, summary_generated_at, summary_notified_at)
                VALUES (%s, 'old summary', NOW() - %s::interval,
                        CASE WHEN %s THEN NOW() - %s::interval ELSE NULL END);
                """,
                (session_id, generated_age, notified, generated_age),
            )


def _cleanup(session_ids):
    query = sql.SQL("DELETE FROM {table} WHERE session_id = ANY(%s::uuid[]);").format(
        table=sql.Identifier(config.table_name)
    )
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, (session_ids,))
            cur.execute("DELETE FROM chat_info WHERE session_id = ANY(%s::text[]);", (session_ids,))


@pytest.fixture
def sessions():
    """
    recent  fresh messages, no chat_info row: needs a summary
    stale   one message 10 days old, outside the window: skipped
    """
    recent = str(uuid.uuid4())
    stale = str(uuid.uuid4())
    _insert_message(recent, timedelta(minutes=5))
    _insert_message(recent, timedelta(minutes=1), msg_type="ai", content="hello")
    _insert_message(stale, timedelta(days=10))
    yield recent, stale
    _cleanup([recent, stale])


@pytest.fixture
def summarised_sessions():
    """
    done       summarised and sent to Slack after its last message: skipped
    continued  summarised and sent, then the visitor wrote again: resummarise
    unsent     summarised, but Slack never got it: needs to go out again
    """
    done = str(uuid.uuid4())
    continued = str(uuid.uuid4())
    unsent = str(uuid.uuid4())
    _insert_message(done, timedelta(minutes=30))
    _mark_summarised(done, timedelta(minutes=10))
    _insert_message(continued, timedelta(minutes=30))
    _mark_summarised(continued, timedelta(minutes=10))
    _insert_message(continued, timedelta(minutes=2), content="one more thing")
    _insert_message(unsent, timedelta(minutes=30))
    _mark_summarised(unsent, timedelta(minutes=10), notified=False)
    yield done, continued, unsent
    _cleanup([done, continued, unsent])


@pytest.fixture(autouse=True)
def fake_slack(monkeypatch):
    """Never reach Slack from the tests. Records what would have been sent."""
    import periodic_summary

    sent = []

    def fake_send(result):
        sent.append(result)
        return {"sent": 1}

    monkeypatch.setattr(periodic_summary, "send_summaries_to_slack", fake_send)
    return sent


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace the Groq call. Records the prompt it was given."""
    import periodic_summary

    calls = []

    class _Response:
        content = "  Visitor asked about pricing.  "

    class _FakeChatGroq:
        def __init__(self, **kwargs):
            pass

        def invoke(self, messages):
            calls.append(messages[0].content)
            return _Response()

    monkeypatch.setattr(periodic_summary, "ChatGroq", _FakeChatGroq)
    return calls


class TestFindChatsNeedingSummary:
    def test_returns_recent_and_skips_stale(self, sessions):
        from periodic_summary import find_chats_needing_summary

        recent, stale = sessions
        rows = find_chats_needing_summary()
        by_id = {row["session_id"]: row for row in rows}

        assert recent in by_id
        assert stale not in by_id
        assert by_id[recent]["message_count"] == 2
        assert by_id[recent]["first_message"] <= by_id[recent]["last_message"]

    def test_window_is_configurable(self, sessions):
        from periodic_summary import find_chats_needing_summary

        recent, stale = sessions
        ids = {row["session_id"] for row in find_chats_needing_summary(window=timedelta(days=30))}
        assert recent in ids
        assert stale in ids

    def test_skips_sent_and_unsent_and_keeps_continued(self, summarised_sessions):
        from periodic_summary import find_chats_needing_summary

        done, continued, unsent = summarised_sessions
        by_id = {row["session_id"]: row for row in find_chats_needing_summary()}

        assert done not in by_id
        assert continued in by_id
        # Its summary is saved; the resend pass handles it, not the LLM.
        assert unsent not in by_id
        # Both messages of the continued session are inside the window.
        assert by_id[continued]["message_count"] == 2

    def test_returns_chat_details(self, sessions):
        from periodic_summary import find_chats_needing_summary

        recent, _ = sessions
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO chat_info (session_id, contact_name, email, domain) "
                    "VALUES (%s, %s, %s, %s);",
                    (recent, "Ada", "ada@example.com", config.DEFAULT_DOMAIN),
                )

        row = {r["session_id"]: r for r in find_chats_needing_summary()}[recent]
        assert row["name"] == "Ada"
        assert row["email"] == "ada@example.com"
        assert row["domain"] == config.DEFAULT_DOMAIN
        assert row["status"] == "OPEN"
        assert row["created_at"] is not None

    def test_details_are_empty_without_chat_info_row(self, sessions):
        from periodic_summary import find_chats_needing_summary

        recent, _ = sessions
        row = {r["session_id"]: r for r in find_chats_needing_summary()}[recent]
        assert row["name"] == ""
        assert row["email"] == ""
        assert row["domain"] is None
        assert row["created_at"] is None


class TestFindUnnotifiedSummaries:
    def test_returns_only_saved_but_unsent(self, summarised_sessions):
        from periodic_summary import find_unnotified_summaries

        done, continued, unsent = summarised_sessions
        by_id = {row["session_id"]: row for row in find_unnotified_summaries()}

        assert unsent in by_id
        assert done not in by_id
        assert continued not in by_id
        row = by_id[unsent]
        assert row["summary"] == "old summary"
        assert row["summary_generated_at"] is not None
        assert row["first_message"] is not None
        assert row["name"] == ""
        assert row["status"] == "OPEN"

    def test_row_without_summary_is_not_returned(self, sessions):
        from periodic_summary import find_unnotified_summaries

        recent, _ = sessions
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute("INSERT INTO chat_info (session_id) VALUES (%s);", (recent,))
        assert recent not in {r["session_id"] for r in find_unnotified_summaries()}


class TestFetchFullConversationHistory:
    def test_returns_every_message_oldest_first(self, sessions):
        from periodic_summary import fetch_full_conversation_history

        recent, stale = sessions
        conversation = fetch_full_conversation_history(recent)
        assert [m["type"] for m in conversation] == ["human", "ai"]
        assert conversation[0]["content"] == "hi"
        assert conversation[1]["content"] == "hello"

        # A stale session still has its full history; it is only excluded
        # from find_chats_needing_summary().
        assert len(fetch_full_conversation_history(stale)) == 1

    def test_unknown_session_is_empty(self):
        from periodic_summary import fetch_full_conversation_history

        assert fetch_full_conversation_history(str(uuid.uuid4())) == []


class TestSummariseConversation:
    def test_calls_llm_with_transcript_and_strips_reply(self, fake_llm):
        from periodic_summary import summarise_conversation

        conversation = [
            {"type": "ai", "content": "Welcome!"},
            {"type": "human", "content": "How much is the pro plan?"},
        ]
        summary = summarise_conversation(conversation)

        assert summary == "Visitor asked about pricing."
        assert len(fake_llm) == 1
        prompt = fake_llm[0]
        assert "Agent: Welcome!" in prompt
        assert "Visitor: How much is the pro plan?" in prompt
        assert "{conversation}" not in prompt

    def test_skips_llm_when_visitor_wrote_nothing(self, fake_llm):
        from periodic_summary import summarise_conversation

        summary = summarise_conversation([{"type": "ai", "content": "Welcome!"}])
        assert summary == "Opened the chat but did not write anything."
        assert fake_llm == []

    def test_long_transcript_is_cut_from_the_front(self, fake_llm):
        import periodic_summary

        old = {"type": "human", "content": "OLD " * 50}
        new = {"type": "human", "content": "NEW " * 50}
        conversation = [old] * 200 + [new]
        periodic_summary.summarise_conversation(conversation)

        prompt = fake_llm[0]
        assert "[earlier messages cut]" in prompt
        assert "NEW" in prompt
        assert len(prompt) < periodic_summary.MAX_TRANSCRIPT_CHARS + 2000


class TestSaveSummary:
    def test_updates_existing_lead_and_returns_it(self, sessions):
        from periodic_summary import save_summary

        recent, _ = sessions
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO chat_info (session_id, contact_name, email) VALUES (%s, %s, %s);",
                    (recent, "Ada", "ada@example.com"),
                )

        row = save_summary(recent, "first")
        assert row["session_id"] == recent
        assert row["name"] == "Ada"
        assert row["email"] == "ada@example.com"
        assert row["summary"] == "first"
        assert row["summary_generated_at"] is not None
        assert row["summary_notified_at"] is None  # Slack has not been told yet
        assert row["website"] is None  # no domain on this lead

        # Running the job again overwrites the summary and keeps the contact.
        row = save_summary(recent, "second")
        assert row["summary"] == "second"
        assert row["name"] == "Ada"
        assert _chat_info_row(recent)["summary"] == "second"

    def test_resolves_website_from_domain_key(self, sessions):
        from periodic_summary import save_summary

        recent, _ = sessions
        with db_pool.get_connection() as conn:
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO chat_info (session_id, domain) VALUES (%s, %s);",
                    (recent, config.DEFAULT_DOMAIN),
                )
                cur.execute("SELECT address FROM domains WHERE key = %s ORDER BY id LIMIT 1;",
                            (config.DEFAULT_DOMAIN,))
                expected = cur.fetchone()[0]

        row = save_summary(recent, "s")
        assert row["domain"] == config.DEFAULT_DOMAIN
        assert row["website"] == expected

    def test_creates_row_when_lead_is_missing(self, sessions):
        from periodic_summary import save_summary

        recent, _ = sessions
        assert _chat_info_row(recent) is None

        row = save_summary(recent, "no lead yet")
        assert row["name"] == ""
        assert row["status"] == "OPEN"
        assert _chat_info_row(recent)["summary"] == "no lead yet"


class TestMarkSummariesNotified:
    def test_stamps_only_the_given_sessions(self, sessions):
        from periodic_summary import mark_summaries_notified, save_summary

        recent, stale = sessions
        save_summary(recent, "a")
        save_summary(stale, "b")

        assert mark_summaries_notified([recent]) == 1
        assert _chat_info_row(recent)["summary_notified_at"] is not None
        assert _chat_info_row(stale)["summary_notified_at"] is None

    def test_empty_list_is_a_no_op(self):
        from periodic_summary import mark_summaries_notified

        assert mark_summaries_notified([]) == 0


class TestPeriodicSummary:
    def test_runs_the_chain_and_saves(self, sessions, fake_llm, fake_slack):
        from periodic_summary import periodic_summary

        recent, _ = sessions
        result = periodic_summary()

        assert result["slack"] == {"sent": 1}
        assert len(fake_slack) == 1
        assert fake_slack[0]["sessions"] is result["sessions"]

        assert result["run_at"] is not None
        assert result["session_count"] + result["resent_count"] == len(result["sessions"])
        assert result["summarised_count"] >= 1

        by_id = {s["session_id"]: s for s in result["sessions"]}
        session = by_id[recent]
        assert len(session["conversation"]) == 2
        assert session["summary"] == "Visitor asked about pricing."
        assert session["lead"]["session_id"] == recent
        row = _chat_info_row(recent)
        assert row["summary"] == "Visitor asked about pricing."
        # Slack accepted the message, so the session is stamped as notified.
        assert row["summary_generated_at"] is not None
        assert row["summary_notified_at"] is not None
        assert result["notified_count"] >= 1

    def test_second_run_leaves_a_sent_session_alone(self, sessions, fake_llm, fake_slack):
        from periodic_summary import periodic_summary

        recent, _ = sessions
        periodic_summary()
        calls_after_first = len(fake_llm)

        result = periodic_summary()

        ids = {s["session_id"] for s in result["sessions"]}
        assert recent not in ids
        # No new LLM call was spent on the session already in Slack.
        assert len(fake_llm) == calls_after_first

    def test_failed_session_is_found_again_next_run(self, sessions, fake_llm, fake_slack, monkeypatch):
        import periodic_summary

        recent, _ = sessions
        real_summarise = periodic_summary.summarise_conversation

        def boom(conversation):
            raise RuntimeError("llm down")

        monkeypatch.setattr(periodic_summary, "summarise_conversation", boom)
        first = periodic_summary.periodic_summary()
        assert "error" in {s["session_id"]: s for s in first["sessions"]}[recent]
        # Nothing was saved, so nothing was stamped.
        assert _chat_info_row(recent) is None

        monkeypatch.setattr(periodic_summary, "summarise_conversation", real_summarise)
        second = periodic_summary.periodic_summary()

        by_id = {s["session_id"]: s for s in second["sessions"]}
        assert by_id[recent]["summary"] == "Visitor asked about pricing."
        assert _chat_info_row(recent)["summary_notified_at"] is not None

    def test_one_failure_does_not_stop_the_job(self, sessions, monkeypatch):
        import periodic_summary

        recent, _ = sessions

        def boom(conversation):
            raise RuntimeError("llm down")

        monkeypatch.setattr(periodic_summary, "summarise_conversation", boom)
        result = periodic_summary.periodic_summary()

        by_id = {s["session_id"]: s for s in result["sessions"]}
        assert by_id[recent]["error"] == "llm down"
        assert by_id[recent]["llm_error"] == "llm down"
        assert "summary" not in by_id[recent]
        assert result["summarised_count"] == 0
        assert result["llm_failed_count"] >= 1
        assert _chat_info_row(recent) is None

    def test_silent_session_is_saved_stamped_and_not_sent(self, fake_llm, monkeypatch):
        import periodic_summary

        silent = str(uuid.uuid4())
        _insert_message(silent, timedelta(minutes=5), msg_type="ai", content="Welcome!")
        # Slack has nothing to send for this run, so it reports a skip.
        monkeypatch.setattr(periodic_summary, "send_summaries_to_slack",
                            lambda result: {"sent": 0, "skipped": "nothing to report"})
        try:
            result = periodic_summary.periodic_summary()

            session = {s["session_id"]: s for s in result["sessions"]}[silent]
            assert session["silent"] is True
            assert session["summary"] == periodic_summary.SILENT_SUMMARY
            assert fake_llm == []
            assert result["silent_count"] == 1
            # Stamped even though nothing reached Slack.
            row = _chat_info_row(silent)
            assert row["summary"] == periodic_summary.SILENT_SUMMARY
            assert row["summary_notified_at"] is not None
            assert result["notified_count"] == 1

            # Not selected again.
            assert silent not in {r["session_id"] for r in periodic_summary.find_chats_needing_summary()}

            # The visitor comes back and writes: the session is selected
            # again and gets a real summary this time.
            _insert_message(silent, timedelta(seconds=0), content="hello, pricing?")
            monkeypatch.setattr(periodic_summary, "send_summaries_to_slack", lambda result: {"sent": 1})
            second = periodic_summary.periodic_summary()
            session = {s["session_id"]: s for s in second["sessions"]}[silent]
            assert "silent" not in session
            assert session["summary"] == "Visitor asked about pricing."
            assert len(fake_llm) == 1
        finally:
            _cleanup([silent])

    def test_save_failure_is_not_an_llm_error(self, sessions, fake_llm, monkeypatch):
        import periodic_summary

        recent, _ = sessions

        def boom(session_id, summary):
            raise RuntimeError("db down")

        monkeypatch.setattr(periodic_summary, "save_summary", boom)
        result = periodic_summary.periodic_summary()

        session = {s["session_id"]: s for s in result["sessions"]}[recent]
        assert session["error"] == "db down"
        assert "llm_error" not in session
        assert result["llm_failed_count"] == 0


class TestExtractLlmErrorReason:
    def test_groq_style_body_gives_the_code(self):
        from periodic_summary import extract_llm_error_reason

        exc = RuntimeError("Error code: 429 - {...}")
        exc.body = {"error": {"code": "rate_limit_exceeded", "message": "Rate limit reached"}}
        assert extract_llm_error_reason(exc) == "rate_limit_exceeded"

    def test_body_without_code_falls_back_to_type_then_message(self):
        from periodic_summary import extract_llm_error_reason

        exc = RuntimeError("x")
        exc.body = {"error": {"message": "Service unavailable"}}
        assert extract_llm_error_reason(exc) == "Service unavailable"

    def test_plain_exception_gives_its_text(self):
        from periodic_summary import extract_llm_error_reason

        assert extract_llm_error_reason(RuntimeError("llm down")) == "llm down"

    def test_empty_message_gives_the_class_name(self):
        from periodic_summary import extract_llm_error_reason

        assert extract_llm_error_reason(TimeoutError()) == "TimeoutError"

    def test_slack_failure_is_recorded_not_raised(self, sessions, fake_llm, monkeypatch):
        import periodic_summary

        def boom(result):
            raise RuntimeError("webhook 500")

        monkeypatch.setattr(periodic_summary, "send_summaries_to_slack", boom)
        result = periodic_summary.periodic_summary()

        assert result["slack"] == {"sent": 0, "error": "webhook 500"}
        assert result["notified_count"] == 0
        # The summaries were saved before Slack was tried, but nothing was
        # notified, so the sessions are not stamped.
        row = _chat_info_row(sessions[0])
        assert row["summary"] == "Visitor asked about pricing."
        assert row["summary_notified_at"] is None

    def test_unsent_summary_is_resent_without_llm(self, sessions, fake_llm, fake_slack, monkeypatch):
        import periodic_summary

        recent, _ = sessions

        def boom(result):
            raise RuntimeError("webhook 500")

        monkeypatch.setattr(periodic_summary, "send_summaries_to_slack", boom)
        first = periodic_summary.periodic_summary()
        assert first["resent_count"] == 0
        assert _chat_info_row(recent)["summary_notified_at"] is None
        llm_calls = len(fake_llm)

        # Slack is back. The saved summary goes out; the LLM is not asked again.
        monkeypatch.setattr(periodic_summary, "send_summaries_to_slack",
                            lambda result: fake_slack.append(result) or {"sent": 1})
        second = periodic_summary.periodic_summary()

        assert len(fake_llm) == llm_calls
        # Not re-summarised by pass one: only present as a resend entry.
        pass_one_ids = {s["session_id"] for s in second["sessions"][: second["session_count"]]}
        assert recent not in pass_one_ids
        assert second["resent_count"] >= 1
        entry = {s["session_id"]: s for s in second["sessions"]}[recent]
        assert entry["resent"] is True
        assert entry["summary"] == "Visitor asked about pricing."
        assert entry["lead"]["session_id"] == recent
        assert entry["first_message"] is not None
        # Slack saw it and it is stamped, so a third run leaves it alone.
        assert fake_slack[-1]["sessions"] is second["sessions"]
        assert _chat_info_row(recent)["summary_notified_at"] is not None
        third = periodic_summary.periodic_summary()
        assert recent not in {s["session_id"] for s in third["sessions"]}

    def test_nothing_to_send_marks_nothing(self, sessions, fake_llm, monkeypatch):
        import periodic_summary

        monkeypatch.setattr(periodic_summary, "send_summaries_to_slack",
                            lambda result: {"sent": 0, "skipped": "SLACK_WEBHOOK_URL not set"})
        result = periodic_summary.periodic_summary()

        assert result["notified_count"] == 0
        assert _chat_info_row(sessions[0])["summary_notified_at"] is None
