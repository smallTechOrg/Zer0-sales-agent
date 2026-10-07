"""
The daily summary chain against the real schema. The LLM call is replaced
with a stub so the tests need no Groq key and are deterministic.

find_sessions() must see a session that just wrote a message and skip one
whose message is older than the window. find_conversation() must return the
whole session. save_summary() must write to chat_info and return the lead row.
daily_summary() must run the chain and keep going when one session fails.
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


@pytest.fixture
def sessions():
    """One session with a fresh message, one with a message 2 days old."""
    recent = str(uuid.uuid4())
    stale = str(uuid.uuid4())
    _insert_message(recent, timedelta(minutes=5))
    _insert_message(recent, timedelta(minutes=1), msg_type="ai", content="hello")
    _insert_message(stale, timedelta(days=2))
    yield recent, stale
    query = sql.SQL("DELETE FROM {table} WHERE session_id = ANY(%s::uuid[]);").format(
        table=sql.Identifier(config.table_name)
    )
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(query, ([recent, stale],))
            cur.execute("DELETE FROM chat_info WHERE session_id = ANY(%s::text[]);", ([recent, stale],))


@pytest.fixture(autouse=True)
def fake_slack(monkeypatch):
    """Never reach Slack from the tests. Records what would have been sent."""
    import daily_summary

    sent = []

    def fake_send(result):
        sent.append(result)
        return {"sent": 1}

    monkeypatch.setattr(daily_summary, "send_summaries_to_slack", fake_send)
    return sent


@pytest.fixture
def fake_llm(monkeypatch):
    """Replace the Groq call. Records the prompt it was given."""
    import daily_summary

    calls = []

    class _Response:
        content = "  Visitor asked about pricing.  "

    class _FakeChatGroq:
        def __init__(self, **kwargs):
            pass

        def invoke(self, messages):
            calls.append(messages[0].content)
            return _Response()

    monkeypatch.setattr(daily_summary, "ChatGroq", _FakeChatGroq)
    return calls


class TestFindSessions:
    def test_returns_recent_and_skips_stale(self, sessions):
        from daily_summary import find_sessions

        recent, stale = sessions
        rows = find_sessions()
        by_id = {row["session_id"]: row for row in rows}

        assert recent in by_id
        assert stale not in by_id
        assert by_id[recent]["message_count"] == 2
        assert by_id[recent]["first_message"] <= by_id[recent]["last_message"]

    def test_window_is_configurable(self, sessions):
        from daily_summary import find_sessions

        recent, stale = sessions
        ids = {row["session_id"] for row in find_sessions(window=timedelta(days=3))}
        assert recent in ids
        assert stale in ids


class TestFindConversation:
    def test_returns_every_message_oldest_first(self, sessions):
        from daily_summary import find_conversation

        recent, stale = sessions
        conversation = find_conversation(recent)
        assert [m["type"] for m in conversation] == ["human", "ai"]
        assert conversation[0]["content"] == "hi"
        assert conversation[1]["content"] == "hello"

        # A stale session still has its full history; it is only excluded
        # from find_sessions().
        assert len(find_conversation(stale)) == 1

    def test_unknown_session_is_empty(self):
        from daily_summary import find_conversation

        assert find_conversation(str(uuid.uuid4())) == []


class TestSummariseConversation:
    def test_calls_llm_with_transcript_and_strips_reply(self, fake_llm):
        from daily_summary import summarise_conversation

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
        from daily_summary import summarise_conversation

        summary = summarise_conversation([{"type": "ai", "content": "Welcome!"}])
        assert "did not write anything" in summary
        assert fake_llm == []

    def test_long_transcript_is_cut_from_the_front(self, fake_llm):
        import daily_summary

        old = {"type": "human", "content": "OLD " * 50}
        new = {"type": "human", "content": "NEW " * 50}
        conversation = [old] * 200 + [new]
        daily_summary.summarise_conversation(conversation)

        prompt = fake_llm[0]
        assert "[earlier messages cut]" in prompt
        assert "NEW" in prompt
        assert len(prompt) < daily_summary.MAX_TRANSCRIPT_CHARS + 2000


class TestSaveSummary:
    def test_updates_existing_lead_and_returns_it(self, sessions):
        from daily_summary import save_summary

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
        from daily_summary import save_summary

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
        from daily_summary import save_summary

        recent, _ = sessions
        assert _chat_info_row(recent) is None

        row = save_summary(recent, "no lead yet")
        assert row["name"] == ""
        assert row["status"] == "OPEN"
        assert _chat_info_row(recent)["summary"] == "no lead yet"


class TestSummaryNotifiedAt:
    def test_stamps_only_the_given_sessions(self, sessions):
        from daily_summary import summary_notified_at, save_summary

        recent, stale = sessions
        save_summary(recent, "a")
        save_summary(stale, "b")

        assert summary_notified_at([recent]) == 1
        assert _chat_info_row(recent)["summary_notified_at"] is not None
        assert _chat_info_row(stale)["summary_notified_at"] is None

    def test_empty_list_is_a_no_op(self):
        from daily_summary import summary_notified_at

        assert summary_notified_at([]) == 0


class TestDailySummary:
    def test_runs_the_chain_and_saves(self, sessions, fake_llm, fake_slack):
        from daily_summary import daily_summary

        recent, _ = sessions
        result = daily_summary()

        assert result["slack"] == {"sent": 1}
        assert len(fake_slack) == 1
        assert fake_slack[0]["sessions"] is result["sessions"]

        assert result["window_hours"] == 24
        assert result["window_end"] - result["window_start"] == timedelta(hours=24)
        assert result["session_count"] == len(result["sessions"])
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

    def test_one_failure_does_not_stop_the_job(self, sessions, monkeypatch):
        import daily_summary

        recent, _ = sessions

        def boom(conversation):
            raise RuntimeError("llm down")

        monkeypatch.setattr(daily_summary, "summarise_conversation", boom)
        result = daily_summary.daily_summary()

        by_id = {s["session_id"]: s for s in result["sessions"]}
        assert by_id[recent]["error"] == "llm down"
        assert "summary" not in by_id[recent]
        assert result["summarised_count"] == 0
        assert _chat_info_row(recent) is None

    def test_slack_failure_is_recorded_not_raised(self, sessions, fake_llm, monkeypatch):
        import daily_summary

        def boom(result):
            raise RuntimeError("webhook 500")

        monkeypatch.setattr(daily_summary, "send_summaries_to_slack", boom)
        result = daily_summary.daily_summary()

        assert result["slack"] == {"sent": 0, "error": "webhook 500"}
        assert result["notified_count"] == 0
        # The summaries were saved before Slack was tried, but nothing was
        # notified, so the sessions are not stamped.
        row = _chat_info_row(sessions[0])
        assert row["summary"] == "Visitor asked about pricing."
        assert row["summary_notified_at"] is None

    def test_nothing_to_send_marks_nothing(self, sessions, fake_llm, monkeypatch):
        import daily_summary

        monkeypatch.setattr(daily_summary, "send_summaries_to_slack",
                            lambda result: {"sent": 0, "skipped": "SLACK_WEBHOOK_URL not set"})
        result = daily_summary.daily_summary()

        assert result["notified_count"] == 0
        assert _chat_info_row(sessions[0])["summary_notified_at"] is None
