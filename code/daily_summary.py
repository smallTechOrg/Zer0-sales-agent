"""
Daily summary of chat activity.

find_sessions() asks the chat history table (config.table_name,
``chat_table`` by default) for every distinct session that had at least one
message in the last 24 hours. For each of those, find_conversation() fetches
the whole conversation, summarise_conversation() asks the LLM for a short
summary, and save_summary() writes it to the session's chat_info row.
daily_summary() runs the whole chain, then posts every summary to Slack
through slack_notify and, when that succeeds, summary_notified_at() stamps the
rows that were sent.

chat_info keeps three columns for this:
    summary               the text of the last summary
    summary_generated_at  when that summary was written
    summary_notified_at   when it was last sent to Slack (NULL: never)

The chat history table is created by LangChain and has these columns:
    id SERIAL, session_id UUID, message JSONB, created_at TIMESTAMPTZ
"""
import traceback
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List, Optional

from langchain_core.messages import SystemMessage
from langchain_groq import ChatGroq
from psycopg import sql
from psycopg.rows import dict_row

import config
from db_pool import with_connection
from history import get_session_history
from slack_notify import send_summaries_to_slack
from system_prompt import get_prompt

# How far back find_sessions() looks.
SUMMARY_WINDOW = timedelta(hours=24)

# The prompt lives in the prompts table as (DEFAULT_DOMAIN, sales,
# daily-summary) so it can be edited through the prompts API. The file is the
# fallback for a database that was bootstrapped before that row existed.
SUMMARY_PROMPT_TYPE = "daily-summary"
SUMMARY_PROMPT_FILE = Path(__file__).parent / "prompts" / "summary_prompt.txt"

# Longest transcript sent to the LLM, in characters. Keeps a very long chat
# inside the model's context; the summary then covers the most recent part.
MAX_TRANSCRIPT_CHARS = 20_000


@with_connection
def find_sessions(conn, window: timedelta = SUMMARY_WINDOW) -> List[Dict[str, Any]]:
    """
    Return one row per session that has a message within *window* of now.

    Each row has:
        session_id     the session as a string
        message_count  messages written in the window
        first_message  oldest message timestamp in the window
        last_message   newest message timestamp in the window

    Newest sessions first. The window is measured on the database clock
    (NOW()), so it does not depend on the app server's timezone.
    """
    query = sql.SQL(
        """
        SELECT
            session_id::text        AS session_id,
            COUNT(*)                AS message_count,
            MIN(created_at)         AS first_message,
            MAX(created_at)         AS last_message
        FROM {table}
        WHERE created_at >= NOW() - %(window)s::interval
        GROUP BY session_id
        ORDER BY last_message DESC;
        """
    ).format(table=sql.Identifier(config.table_name))

    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, {"window": window})
        return cur.fetchall()


def find_conversation(session_id: str) -> List[Dict[str, str]]:
    """
    Return every message of one session, oldest first. Each item is
    {"type": "human" | "ai", "content": "..."}.

    This reads the whole session, not only the last 24 hours, so the summary
    has the full context of the conversation. It goes through the same pooled
    history reader the /history endpoint uses, so the stored LangChain message
    format is decoded in one place.
    """
    messages = get_session_history(session_id).messages
    return [{"type": msg.type, "content": msg.content} for msg in messages]


def _format_transcript(conversation: List[Dict[str, str]]) -> str:
    """Render the messages as 'Visitor: ...' / 'Agent: ...' lines."""
    label = {"human": "Visitor", "ai": "Agent"}
    lines = [f"{label.get(m['type'], m['type'])}: {m['content']}" for m in conversation]
    transcript = "\n".join(lines)
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = "[earlier messages cut]\n" + transcript[-MAX_TRANSCRIPT_CHARS:]
    return transcript


def _summary_prompt() -> str:
    """The prompt template from the database, or the file if it is not there."""
    text = get_prompt(config.DEFAULT_DOMAIN, config.agent_type.SALES, SUMMARY_PROMPT_TYPE)
    return text or SUMMARY_PROMPT_FILE.read_text(encoding="utf-8")


def summarise_conversation(conversation: List[Dict[str, str]]) -> str:
    """
    Ask the LLM for a short plain-text summary of one conversation. Raises if
    the LLM call fails; daily_summary() decides what to do with that.
    """
    if not any(m["type"] == "human" for m in conversation):
        # Only the intro message was shown. No need to spend an LLM call.
        return "The visitor opened the chat but did not write anything."

    prompt = _summary_prompt().replace("{conversation}", _format_transcript(conversation))
    llm = ChatGroq(groq_api_key=config.GROQ_API_KEY, model=config.GROQ_MODEL_NAME)
    response = llm.invoke([SystemMessage(content=prompt)])
    return response.content.strip()


@with_connection
def save_summary(conn, session_id: str, summary: str) -> Optional[Dict[str, Any]]:
    """
    Write the summary and summary_generated_at to the session's chat_info row
    and return that row with the lead details and the website the chat ran
    on. A session that has no chat_info row yet (the processor never ran for
    it) gets one, so no summary is lost. summary_notified_at is not touched
    here: summary_notified_at() sets it once Slack has the summary.

    chat_info.domain holds the domain key (COMMON, ...). The domains table maps
    that key to the site address, which is what the Slack line shows.
    """
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(
            """
            INSERT INTO chat_info (session_id, summary, summary_generated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (session_id) DO UPDATE SET
                summary              = EXCLUDED.summary,
                summary_generated_at = EXCLUDED.summary_generated_at
            RETURNING
                session_id,
                COALESCE(contact_name, '') AS name,
                COALESCE(email, '')        AS email,
                COALESCE(mobile, '')       AS mobile,
                COALESCE(country, '')      AS country,
                COALESCE(status, 'OPEN')   AS status,
                request_type,
                domain,
                (SELECT address FROM domains d
                 WHERE d.key = chat_info.domain
                 ORDER BY d.id LIMIT 1)        AS website,
                summary,
                summary_generated_at,
                summary_notified_at;
            """,
            (session_id, summary),
        )
        row = cur.fetchone()
    print(f"[DAILY_SUMMARY] summary saved for session {session_id}")
    return row


@with_connection
def summary_notified_at(conn, session_ids: List[str]) -> int:
    """
    Set summary_notified_at = NOW() on the given sessions. Called after Slack
    accepted the message that carried their summaries. Returns the number of
    rows updated.
    """
    if not session_ids:
        return 0
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE chat_info
            SET summary_notified_at = NOW()
            WHERE session_id = ANY(%s::text[]);
            """,
            (list(session_ids),),
        )
        updated = cur.rowcount
    print(f"[DAILY_SUMMARY] {updated} session(s) marked as notified")
    return updated


def daily_summary() -> Dict[str, Any]:
    """
    Build the daily summary: for each session active in the last 24 hours,
    fetch the conversation, summarise it, and save the summary on chat_info.
    Then send all of them to Slack.

    One session failing does not stop the others. The failure is logged and
    the session is returned with an ``error`` field instead of a summary. A
    Slack failure is recorded under ``slack`` and does not raise: the
    summaries are already saved by then. After a successful Slack delivery
    the summarised sessions get summary_notified_at set; the count is
    returned under ``notified_count``.
    """
    # The window the Slack header shows. find_sessions() measures the same
    # window on the database clock, so the two can differ by the query time.
    window_end = datetime.now()
    window_start = window_end - SUMMARY_WINDOW

    sessions = find_sessions()
    window_hours = SUMMARY_WINDOW.total_seconds() / 3600
    print(f"[DAILY_SUMMARY] {len(sessions)} session(s) active in the last {window_hours:g} hours")

    # One database call at a time. find_sessions() has released its connection
    # by now, so none of these nest two pooled connections.
    for session in sessions:
        session_id = session["session_id"]
        try:
            session["conversation"] = find_conversation(session_id)
            print(f"[DAILY_SUMMARY] session {session_id}: "
                  f"{len(session['conversation'])} message(s) in full conversation")

            summary = summarise_conversation(session["conversation"])
            session["lead"] = save_summary(session_id, summary)
            session["summary"] = summary
        except Exception as exc:
            print(f"[DAILY_SUMMARY] session {session_id} failed: {exc}")
            print(traceback.format_exc())
            session["error"] = str(exc)

    result = {
        "window_hours": window_hours,
        "window_start": window_start,
        "window_end": window_end,
        "session_count": len(sessions),
        "summarised_count": sum(1 for s in sessions if "summary" in s),
        "sessions": sessions,
    }

    try:
        result["slack"] = send_summaries_to_slack(result)
    except Exception as exc:
        print(f"[DAILY_SUMMARY] Slack delivery failed: {exc}")
        print(traceback.format_exc())
        result["slack"] = {"sent": 0, "error": str(exc)}

    # Only sessions whose summary reached Slack are stamped. Sessions that
    # failed to summarise appear in the message as an error line, not a
    # summary, so they stay unnotified and are tried again next run.
    result["notified_count"] = 0
    if result["slack"].get("sent", 0) > 0:
        try:
            result["notified_count"] = summary_notified_at(
                [s["session_id"] for s in sessions if "summary" in s]
            )
        except Exception as exc:
            print(f"[DAILY_SUMMARY] could not mark sessions as notified: {exc}")
            print(traceback.format_exc())

    return result


if __name__ == "__main__":
    import json

    print(json.dumps(daily_summary(), indent=2, default=str))
