"""
Periodic summary of chat activity: find chats that need a summary, summarise
each with the LLM, save it on chat_info (summary, summary_generated_at,
summary_notified_at), and post to Slack, resending until Slack has it.
"""
import logging
from datetime import datetime, timedelta
from pathlib import Path
from typing import Any, Dict, List
from zoneinfo import ZoneInfo

from langchain_core.messages import SystemMessage
from langchain_groq import ChatGroq
from psycopg import sql
from psycopg.rows import dict_row

import config
from db_pool import with_connection
from slack_notify import send_summaries_to_slack

log = logging.getLogger(__name__)

# How far back find_chats_needing_summary() looks (SUMMARY_WINDOW_DAYS in .env).
SUMMARY_WINDOW = timedelta(days=config.SUMMARY_WINDOW_DAYS)

# Prompt template for the summary; {conversation} is replaced with the transcript.
SUMMARY_PROMPT_FILE = Path(__file__).parent / "prompts" / "summary_prompt.txt"

# Saved as the summary of a session where the visitor wrote nothing. Shown on
# the dashboard, not sent to Slack.
SILENT_SUMMARY = "Opened the chat but did not write anything."

# Longest transcript sent to the LLM, in characters. Keeps a very long chat
# inside the model's context; the summary then covers the most recent part.
MAX_TRANSCRIPT_CHARS = 20_000


def periodic_summary() -> Dict[str, Any]:
    """
    Summarise chats that need it and save each summary, add saved summaries
    that never reached Slack, post all to Slack, and mark the sent ones as
    notified. One failing session or a Slack failure does not stop the run.
    """
    # When this run happened, in the timezone the Slack message is read in.
    run_at = datetime.now(ZoneInfo(config.PERIODIC_SUMMARY_TIMEZONE))

    sessions = find_chats_needing_summary()
    log.info("%d session(s) waiting for a summary", len(sessions))

    # One database call at a time, so pooled connections are never nested.
    for session in sessions:
        session_id = session["session_id"]
        try:
            session["conversation"] = fetch_full_conversation_history(session_id)
            log.info(
                "session %s: %d message(s) in full conversation",
                session_id,
                len(session["conversation"]),
            )

            if is_silent(session["conversation"]):
                session["silent"] = True
                summary = SILENT_SUMMARY
            else:
                try:
                    summary = summarise_conversation(session["conversation"])
                except Exception as exc:
                    session["llm_error"] = extract_llm_error_reason(exc)
                    raise
            save_summary(session_id, summary)
            session["summary"] = summary
        except Exception as exc:
            log.exception("session %s failed", session_id)
            session["error"] = str(exc)

    # Pass two: summaries saved earlier that never reached Slack. A session
    # pass one touched this run is skipped: it either has a fresher summary
    # already in the list, or it failed and keeps its old row for next time.
    resent: List[Dict[str, Any]] = []
    try:
        handled = {s["session_id"] for s in sessions}
        for row in find_unnotified_summaries():
            if row["session_id"] in handled:
                continue
            entry: Dict[str, Any] = {
                "session_id": row["session_id"],
                "summary": row["summary"],
                "first_message": row["first_message"],
                "domain": row["domain"],
                "name": row["name"],
                "resent": True,
            }
            if row["summary"] == SILENT_SUMMARY:
                # A silent session whose stamp failed. Still not for Slack.
                entry["silent"] = True
            resent.append(entry)
    except Exception:
        log.exception("could not look up unsent summaries")
    log.info("%d saved summary(ies) waiting to be resent", len(resent))

    all_sessions = sessions + resent
    result = {
        "run_at": run_at,
        "session_count": len(sessions),
        "summarised_count": sum(1 for s in sessions if "summary" in s),
        "llm_failed_count": sum(1 for s in sessions if "llm_error" in s),
        "silent_count": sum(1 for s in sessions if s.get("silent")),
        "resent_count": len(resent),
        "sessions": all_sessions,
    }

    try:
        result["slack"] = send_summaries_to_slack(result)
    except Exception as exc:
        log.exception("Slack delivery failed")
        result["slack"] = {"sent": 0, "error": str(exc)}

    # Silent sessions are always stamped; others only once Slack accepted them.
    # Failed sessions have no summary, so they are retried next run.
    to_stamp = [s["session_id"] for s in all_sessions if "summary" in s and s.get("silent")]
    if result["slack"].get("sent", 0) > 0:
        to_stamp += [s["session_id"] for s in all_sessions if "summary" in s and not s.get("silent")]
    result["notified_count"] = 0
    if to_stamp:
        try:
            result["notified_count"] = mark_summaries_notified(to_stamp)
        except Exception:
            log.exception("could not mark sessions as notified")

    return result


@with_connection
def find_chats_needing_summary(conn, window: timedelta = SUMMARY_WINDOW) -> List[Dict[str, Any]]:
    """
    Return one row per chat with a message in *window* that has no summary yet
    or got new messages after its summary was written, joined with its
    chat_info details (domain, name, email, status, ...). Newest first.
    """
    query = sql.SQL(
        """
        SELECT
            h.session_id::text                  AS session_id,
            ci.domain                           AS domain,
            COALESCE(ci.contact_name, '')       AS name,
            COALESCE(ci.email, '')              AS email,
            ci.request_type                     AS request_type,
            COALESCE(ci.status, 'OPEN')         AS status,
            ci.created_at                       AS created_at,
            COUNT(*)                            AS message_count,
            MIN(h.created_at)                   AS first_message,
            MAX(h.created_at)                   AS last_message
        FROM {table} h
        LEFT JOIN chat_info ci ON ci.session_id = h.session_id::text
        WHERE h.created_at >= NOW() - %(window)s::interval
        GROUP BY
            h.session_id, ci.domain, ci.contact_name, ci.email, ci.request_type,
            ci.status, ci.created_at, ci.summary, ci.summary_generated_at
        HAVING ci.summary IS NULL
            OR MAX(h.created_at) > ci.summary_generated_at
        ORDER BY last_message DESC;
        """
    ).format(table=sql.Identifier(config.table_name))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, {"window": window})
        return cur.fetchall()

@with_connection
def fetch_full_conversation_history(conn, session_id: str) -> List[Dict[str, str]]:
    """
    Return every message of one session (not just the window), oldest first,
    as {"type": "human" | "ai", "content": "..."}. Unknown session gives [].
    """
    query = sql.SQL(
        """
        SELECT
            message->>'type'                               AS type,
            COALESCE(message->'data'->>'content', '')      AS content
        FROM {table}
        WHERE session_id = %(session_id)s::uuid
        ORDER BY created_at, id;
        """
    ).format(table=sql.Identifier(config.table_name))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query, {"session_id": session_id})
        return cur.fetchall()

def _format_conversation_transcript(conversation: List[Dict[str, str]]) -> str:
    """Render the messages as 'Visitor: ...' / 'Agent: ...' lines."""
    label = {"human": "Visitor", "ai": "Agent"}
    lines = [f"{label.get(m['type'], m['type'])}: {m['content']}" for m in conversation]
    transcript = "\n".join(lines)
    if len(transcript) > MAX_TRANSCRIPT_CHARS:
        transcript = "[earlier messages cut]\n" + transcript[-MAX_TRANSCRIPT_CHARS:]
    return transcript


def _get_summary_prompt_template() -> str:
    return SUMMARY_PROMPT_FILE.read_text(encoding="utf-8")


def extract_llm_error_reason(exc: BaseException) -> str:
    """
    Short name of an LLM failure for Slack: the Groq error code from
    ``exc.body`` (e.g. rate_limit_exceeded), else the exception text or class name.
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
    the LLM call fails; periodic_summary() decides what to do with that.
    """
    if is_silent(conversation):
        # Only the intro message was shown. No need to spend an LLM call.
        return SILENT_SUMMARY

    prompt = _get_summary_prompt_template().replace("{conversation}", _format_conversation_transcript(conversation))
    llm = ChatGroq(groq_api_key=config.GROQ_API_KEY, model=config.GROQ_MODEL_NAME)
    response = llm.invoke([SystemMessage(content=prompt)])
    return response.content.strip()


@with_connection
def save_summary(conn, session_id: str, summary: str) -> None:
    """
    Upsert the summary and summary_generated_at on the session's chat_info row
    (created if missing).
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO chat_info (session_id, summary, summary_generated_at)
            VALUES (%s, %s, NOW())
            ON CONFLICT (session_id) DO UPDATE SET
                summary              = EXCLUDED.summary,
                summary_generated_at = EXCLUDED.summary_generated_at;
            """,
            (session_id, summary),
        )
    log.info("summary saved for session %s", session_id)


@with_connection
def mark_summaries_notified(conn, session_ids: List[str]) -> int:
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
    log.info("%d session(s) marked as notified", updated)
    return updated


@with_connection
def find_unnotified_summaries(conn) -> List[Dict[str, Any]]:
    """
    Return chats with a saved summary that was never sent to Slack
    (summary_notified_at IS NULL), newest first, with no time limit.
    """
    query = sql.SQL(
        """
        SELECT
            ci.session_id,
            COALESCE(ci.contact_name, '') AS name,
            ci.domain,
            ci.summary,
            (SELECT MIN(h.created_at) FROM {table} h
             WHERE h.session_id::text = ci.session_id) AS first_message
        FROM chat_info ci
        WHERE ci.summary_notified_at IS NULL
          AND ci.summary IS NOT NULL
        ORDER BY ci.summary_generated_at DESC NULLS LAST;
        """
    ).format(table=sql.Identifier(config.table_name))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query)
        return cur.fetchall()
