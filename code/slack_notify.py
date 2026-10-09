"""
Send the chat summary to a Slack channel through an incoming webhook.

Message format, one plain-text message. The header is a link to the
dashboard (config.DASHBOARD_URL); the time is when the job ran:

    Zer0 Chat Summary - 7th Oct 2026 - 5 pm IST
    4:13 pm - SMALLTECH - Anjali - Enquiry for AI training in Bangalore.
    4:17 pm - ZERO - Rahul - Wants to approach a partnership for small businesses.
    4:17 pm - SILVERWAVE - Unknown - Just said hi.

Each line: time of the session's first recent message, the domain key, the
visitor's name (Unknown when none was shared), and the summary. Times are
shown in config.PERIODIC_SUMMARY_TIMEZONE.

Conversations whose LLM call failed do not get a line. The run reports them
in one sentence after the lines, so an outage reads as one event:

    Zer0 Chat Summary - 7th Oct 2026 - 6 pm IST
    Failed to generate a summary. LLM calls failed with error: rate_limit_exceeded.

When only some conversations failed, the sentence says how many, after the
lines of the ones that worked. Those conversations are not stamped as
notified, so the next run tries them again.

Silent sessions, where the visitor never wrote (``session["silent"]``), get
no line: the channel is for conversations. A run with only silent sessions
sends nothing.

The webhook URL comes from config.SLACK_WEBHOOK_URL. When it is not set the
sender logs that and does nothing, so the summary job still runs and saves
its results in development. A run with no sessions waiting for a summary
sends nothing either: the channel only gets a message when there is
something to read.
"""
from datetime import datetime
from typing import Any, Dict, List, Optional
from zoneinfo import ZoneInfo

import requests

import config

# Slack accepts about 40,000 characters in ``text``. Stay well under it and
# split a long run into several messages.
MESSAGE_LIMIT = 30_000
# Longest single conversation line.
LINE_LIMIT = 3_000

TITLE = "Zer0 Chat Summary"
# Separates distinct LLM errors in the failure sentence when a run saw more
# than one kind.
ERROR_SEPARATOR = "; "


def _ordinal(day: int) -> str:
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def _localize(value: datetime) -> datetime:
    """
    Put a timestamp in the display timezone. A naive value is taken to be in
    that timezone already; an aware one (TIMESTAMPTZ from the database) is
    converted.
    """
    tz = ZoneInfo(config.PERIODIC_SUMMARY_TIMEZONE)
    if value.tzinfo is None:
        return value.replace(tzinfo=tz)
    return value.astimezone(tz)


def _clock(value: datetime) -> str:
    """4:13 pm. Whole hours drop the minutes: 5 pm."""
    hour = value.strftime("%I").lstrip("0")
    ampm = value.strftime("%p").lower()
    if value.minute == 0:
        return f"{hour} {ampm}"
    return f"{hour}:{value.minute:02d} {ampm}"


def format_header_time(value: datetime) -> str:
    """7th Oct 2026 - 5 pm IST"""
    value = _localize(value)
    return f"{_ordinal(value.day)} {value.strftime('%b %Y')} - {_clock(value)} {value.strftime('%Z')}"


def format_line_time(value: datetime) -> str:
    """4:13 pm"""
    return _clock(_localize(value))


def _escape(text: str) -> str:
    """Slack reads &, < and > as markup in text. Escape them."""
    return text.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _header(result: Dict[str, Any]) -> str:
    run_at: Optional[datetime] = result.get("run_at")
    title = TITLE
    if isinstance(run_at, datetime):
        title = f"{TITLE} - {format_header_time(run_at)}"
    # Slack link syntax: <url|text>. Incoming webhooks render it by default.
    return f"<{config.DASHBOARD_URL}|{title}>"


def _conversation_line(session: Dict[str, Any]) -> str:
    lead = session.get("lead") or {}

    first = session.get("first_message")
    when = format_line_time(first) if isinstance(first, datetime) else "unknown time"

    domain = (lead.get("domain") or "UNKNOWN").upper()
    name = (lead.get("name") or "").strip() or "Unknown"

    if "error" in session:
        body = f"summary failed ({session['error']})"
    else:
        body = (session.get("summary") or "no summary").replace("\n", " ").strip()

    line = f"{when} - {domain} - {_escape(name)} - {_escape(body)}"
    return _clip(line, LINE_LIMIT)


def _llm_failure_line(failed: List[Dict[str, Any]], total: int) -> str:
    """
    One sentence for the sessions whose LLM call failed. Distinct errors are
    listed once each, in the order they were first seen. The count is only
    shown when the run also had conversations that worked.
    """
    errors: List[str] = []
    for session in failed:
        error = str(session.get("llm_error") or "unknown error")
        if error not in errors:
            errors.append(error)

    if len(failed) == total:
        what = "Failed to generate a summary."
    elif len(failed) == 1:
        what = "Failed to generate a summary for 1 conversation."
    else:
        what = f"Failed to generate a summary for {len(failed)} conversations."
    line = f"{what} LLM calls failed with error: {_escape(ERROR_SEPARATOR.join(errors))}."
    return _clip(line, LINE_LIMIT)


def build_messages(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Turn a periodic_summary() result into webhook payloads. Normally one; a very
    long run is split, and each part repeats the header. Sessions whose LLM
    call failed become one sentence at the end instead of a line each.
    """
    sessions = result.get("sessions", [])
    if not sessions:
        return []

    # Silent sessions are not reported and do not count towards "all failed".
    reportable = [s for s in sessions if not s.get("silent")]
    llm_failed = [s for s in reportable if "llm_error" in s]
    lines = [_conversation_line(s) for s in reportable if "llm_error" not in s]
    if llm_failed:
        lines.append(_llm_failure_line(llm_failed, len(reportable)))
    if not lines:
        return []

    header = _header(result)

    messages: List[Dict[str, Any]] = []
    current = [header]
    size = len(header)
    for line in lines:
        if size + len(line) + 1 > MESSAGE_LIMIT and len(current) > 1:
            messages.append({"text": "\n".join(current)})
            current = [header]
            size = len(header)
        current.append(line)
        size += len(line) + 1
    messages.append({"text": "\n".join(current)})
    return messages


def post_to_slack(payload: Dict[str, Any], webhook_url: str = None) -> None:
    """POST one payload to the webhook. Raises on a non-2xx answer."""
    url = webhook_url or config.SLACK_WEBHOOK_URL
    response = requests.post(url, json=payload, timeout=config.SLACK_TIMEOUT)
    if not response.ok:
        raise RuntimeError(f"Slack webhook returned {response.status_code}: {response.text[:200]}")


def send_summaries_to_slack(result: Dict[str, Any]) -> Dict[str, Any]:
    """
    Send the whole summary. Returns {"sent": n, "skipped": reason?}.
    Nothing is sent when there are no sessions, only silent sessions, or no
    webhook. Raises if a post fails, so the caller can record it.
    """
    if not result.get("sessions"):
        print("[SLACK] No sessions waiting for a summary. Skipping Slack delivery.")
        return {"sent": 0, "skipped": "no sessions"}

    if not config.SLACK_WEBHOOK_URL:
        print("[SLACK] SLACK_WEBHOOK_URL is not set. Skipping Slack delivery.")
        return {"sent": 0, "skipped": "SLACK_WEBHOOK_URL not set"}

    messages = build_messages(result)
    if not messages:
        print("[SLACK] Only silent sessions in this run. Skipping Slack delivery.")
        return {"sent": 0, "skipped": "nothing to report"}

    for payload in messages:
        post_to_slack(payload)
    print(f"[SLACK] Sent {len(messages)} message(s) to Slack.")
    return {"sent": len(messages)}
