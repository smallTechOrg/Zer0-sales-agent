"""
Hourly summary of chat activity.

find_sessions() asks the chat history table (config.table_name,
``chat_table`` by default) for every session that still needs a summary: one
with recent messages whose chat_info row is missing, was never sent to Slack
(summary_notified_at IS NULL), or got new messages after its summary was
written. In a normal hour that is the conversations of the last hour. When
an earlier run failed, at the LLM or at Slack, its sessions are still
unstamped and are picked up again, so nothing is lost.

For each of those, find_conversation() fetches the whole conversation,
summarise_conversation() asks the LLM for a short summary, and save_summary()
writes it to the session's chat_info row. daily_summary() runs the whole
chain, then posts every summary to Slack through slack_notify and, when that
succeeds, summary_notified_at() stamps the rows that were sent.

A session where the visitor never wrote anything (only the agent's intro was
shown) is "silent". It gets SILENT_SUMMARY saved without an LLM call, is left
out of the Slack message, and is stamped as notified right away so it is not
selected again. If the visitor comes back and writes, the new message is
later than summary_generated_at and the session is summarised for real.

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
from zoneinfo import ZoneInfo

from langchain_core.messages import SystemMessage
from langchain_groq import ChatGroq
from psycopg import sql
from psycopg.rows import dict_row

import config
from db_pool import with_connection
from history import get_session_history
from slack_notify import send_summaries_to_slack
from system_prompt import get_prompt

# How far back find_sessions() looks for sessions without a summary. It is
# only a cap on retries: a session is summarised once and then left alone, so
# the first run after a long outage does not go through the whole history.
SUMMARY_WINDOW = timedelta(days=7)

# The prompt lives in the prompts table as (DEFAULT_DOMAIN, sales,
# daily-summary) so it can be edited through the prompts API. The file is the
# fallback for a database that was bootstrapped before that row existed.
SUMMARY_PROMPT_TYPE = "daily-summary"
SUMMARY_PROMPT_FILE = Path(__file__).parent / "prompts" / "summary_prompt.txt"

# Saved as the summary of a session where the visitor wrote nothing. Shown on
# the dashboard, not sent to Slack.
SILENT_SUMMARY = "Opened the chat but did not write anything."

# Longest transcript sent to the LLM, in characters. Keeps a very long chat
# inside the model's context; the summary then covers the most recent part.
MAX_TRANSCRIPT_CHARS = 20_000


@with_connection
def find_sessions(conn, window: timedelta = SUMMARY_WINDOW) -> List[Dict[str, Any]]:
    """
    Return one row per session that has a message within *window* of now and
    still needs a summary. A session needs a summary when:

        it has no chat_info row yet, or
        its summary never reached Slack (summary_notified_at IS NULL), or
        it got a message after the summary was written, so that summary
        is out of date.

    Each row has:
        session_id     the session as a string
        message_count  messages written in the window
        first_message  oldest message timestamp in the window
        last_message   newest message timestamp in the window

    Newest sessions first. The window is measured on the database clock
    (NOW()), so it does not depend on the app server's timezone.

    chat_info.session_id is TEXT and the history table's is UUID; the join
    casts the UUID. chat_info has one row per session (UNIQUE), so the join
    never multiplies the message rows.
    """
    query = sql.SQL(
        """
        SELECT
            h.session_id::text      AS session_id,
            COUNT(*)                AS message_count,
            MIN(h.created_at)       AS first_message,
            MAX(h.created_at)       AS last_message
        FROM {table} h
        LEFT JOIN chat_info ci ON ci.session_id = h.session_id::text
        WHERE h.created_at >= NOW() - %(window)s::interval
        GROUP BY h.session_id, ci.summary_notified_at, ci.summary_generated_at
        HAVING ci.summary_notified_at IS NULL
            OR MAX(h.created_at) > ci.summary_generated_at
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

    This reads the whole session, not only the messages inside the window, so
    the summary has the full context of the conversation. It goes through the same pooled
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


def llm_error_text(exc: BaseException) -> str:
    """
    A short name for an LLM failure, for the Slack message. The Groq SDK puts
    the API's answer on ``exc.body`` as {"error": {"code": ..., "message":
    ...}}; the code (rate_limit_exceeded, model_not_found, ...) is the most
    useful part. Anything else falls back to the exception text, or its class
    name when there is none.
    """
    body = getattr(exc, "body", None)
    error = body.get("error") if isinstance(body, dict) else None
    if isinstance(error, dict):
        code = error.get("code") or error.get("type") or error.get("message")
        if code:
            return str(code)
    return str(exc).strip() or type(exc).__name__


def is_silent(conversation: List[Dict[str, str]]) -> bool:
    """True when the visitor never wrote: only agent messages, or none."""
    return not any(m["type"] == "human" for m in conversation)


def summarise_conversation(conversation: List[Dict[str, str]]) -> str:
    """
    Ask the LLM for a short plain-text summary of one conversation. Raises if
    the LLM call fails; daily_summary() decides what to do with that.
    """
    if is_silent(conversation):
        # Only the intro message was shown. No need to spend an LLM call.
        return SILENT_SUMMARY

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
    Build the summary: for each session that still needs one (see
    find_sessions), fetch the conversation, summarise it, and save the
    summary on chat_info. Then send all of them to Slack.

    One session failing does not stop the others. The failure is logged and
    the session is returned with an ``error`` field instead of a summary.
    When the failure was the LLM call, the session also carries
    ``llm_error`` (see llm_error_text), which Slack reports as one sentence
    for the whole run instead of one line per conversation. A
    Slack failure is recorded under ``slack`` and does not raise: the
    summaries are already saved by then. After a successful Slack delivery
    the summarised sessions get summary_notified_at set; the count is
    returned under ``notified_count``. Sessions that were not stamped, for
    either reason, are found again by the next run.

    A silent session (see is_silent) is marked ``silent`` and stamped whether
    or not anything was sent to Slack: there is nothing to tell the channel,
    and the stamp keeps it out of the next run. ``silent_count`` says how
    many there were.
    """
    # When this run happened, in the timezone the Slack message is read in.
    run_at = datetime.now(ZoneInfo(config.DAILY_SUMMARY_TIMEZONE))

    sessions = find_sessions()
    print(f"[DAILY_SUMMARY] {len(sessions)} session(s) waiting for a summary")

    # One database call at a time. find_sessions() has released its connection
    # by now, so none of these nest two pooled connections.
    for session in sessions:
        session_id = session["session_id"]
        try:
            session["conversation"] = find_conversation(session_id)
            print(f"[DAILY_SUMMARY] session {session_id}: "
                  f"{len(session['conversation'])} message(s) in full conversation")

            if is_silent(session["conversation"]):
                session["silent"] = True
                summary = SILENT_SUMMARY
            else:
                try:
                    summary = summarise_conversation(session["conversation"])
                except Exception as exc:
                    session["llm_error"] = llm_error_text(exc)
                    raise
            session["lead"] = save_summary(session_id, summary)
            session["summary"] = summary
        except Exception as exc:
            print(f"[DAILY_SUMMARY] session {session_id} failed: {exc}")
            print(traceback.format_exc())
            session["error"] = str(exc)

    result = {
        "run_at": run_at,
        "session_count": len(sessions),
        "summarised_count": sum(1 for s in sessions if "summary" in s),
        "llm_failed_count": sum(1 for s in sessions if "llm_error" in s),
        "silent_count": sum(1 for s in sessions if s.get("silent")),
        "sessions": sessions,
    }

    try:
        result["slack"] = send_summaries_to_slack(result)
    except Exception as exc:
        print(f"[DAILY_SUMMARY] Slack delivery failed: {exc}")
        print(traceback.format_exc())
        result["slack"] = {"sent": 0, "error": str(exc)}

    # Silent sessions are stamped whatever Slack did: they were never meant
    # to be sent. Other sessions are stamped only when their summary reached
    # Slack. Sessions that failed to summarise have no summary, so they stay
    # unnotified and are tried again next run.
    to_stamp = [s["session_id"] for s in sessions if "summary" in s and s.get("silent")]
    if result["slack"].get("sent", 0) > 0:
        to_stamp += [s["session_id"] for s in sessions if "summary" in s and not s.get("silent")]
    result["notified_count"] = 0
    if to_stamp:
        try:
            result["notified_count"] = summary_notified_at(to_stamp)
        except Exception as exc:
            print(f"[DAILY_SUMMARY] could not mark sessions as notified: {exc}")
            print(traceback.format_exc())

    return result


if __name__ == "__main__":
    import json

    print(json.dumps(daily_summary(), indent=2, default=str))
