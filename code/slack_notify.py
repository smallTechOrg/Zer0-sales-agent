"""
Send the daily summary to a Slack channel through an incoming webhook.

Message format, one plain-text message:

    Conversation From 5th Oct 12:00 PM - 6th Oct 12:00 PM
    Conversation 1: <summary>, <website>
    Conversation 2: <summary>, <website>

The webhook URL comes from config.SLACK_WEBHOOK_URL. When it is not set the
sender logs that and does nothing, so the summary job still runs and saves
its results in development. A day with no sessions sends nothing either: the
channel only gets a message when there is something to read.
"""
from datetime import datetime
from typing import Any, Dict, List

import requests

import config

# Slack accepts about 40,000 characters in ``text``. Stay well under it and
# split a long day into several messages.
MESSAGE_LIMIT = 30_000
# Longest single conversation line.
LINE_LIMIT = 3_000


def _ordinal(day: int) -> str:
    if 11 <= day % 100 <= 13:
        suffix = "th"
    else:
        suffix = {1: "st", 2: "nd", 3: "rd"}.get(day % 10, "th")
    return f"{day}{suffix}"


def format_time(value: datetime) -> str:
    """5th Oct 12:00 PM"""
    clock = value.strftime("%I:%M %p").lstrip("0")
    return f"{_ordinal(value.day)} {value.strftime('%b')} {clock}"


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _header(result: Dict[str, Any]) -> str:
    start = result.get("window_start")
    end = result.get("window_end")
    if isinstance(start, datetime) and isinstance(end, datetime):
        return f"Conversation From {format_time(start)} - {format_time(end)}"
    return "Conversation From the last 24 hours"


def _conversation_line(index: int, session: Dict[str, Any]) -> str:
    lead = session.get("lead") or {}
    website = lead.get("website") or lead.get("domain") or "unknown website"

    if "error" in session:
        body = f"summary failed ({session['error']})"
    else:
        body = (session.get("summary") or "no summary").replace("\n", " ").strip()

    return _clip(f"Conversation {index}: {body}, {website}", LINE_LIMIT)


def build_messages(result: Dict[str, Any]) -> List[Dict[str, Any]]:
    """
    Turn a daily_summary() result into webhook payloads. Normally one; a very
    long day is split, and each part repeats the header.
    """
    sessions = result.get("sessions", [])
    if not sessions:
        return []

    header = _header(result)
    lines = [_conversation_line(i, s) for i, s in enumerate(sessions, start=1)]

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
    Send the whole daily summary. Returns {"sent": n, "skipped": reason?}.
    Nothing is sent when there are no sessions or no webhook. Raises if a
    post fails, so the caller can record it.
    """
    if not result.get("sessions"):
        print("[SLACK] No sessions in the window. Skipping Slack delivery.")
        return {"sent": 0, "skipped": "no sessions"}

    if not config.SLACK_WEBHOOK_URL:
        print("[SLACK] SLACK_WEBHOOK_URL is not set. Skipping Slack delivery.")
        return {"sent": 0, "skipped": "SLACK_WEBHOOK_URL not set"}

    messages = build_messages(result)
    for payload in messages:
        post_to_slack(payload)
    print(f"[SLACK] Sent {len(messages)} message(s) to Slack.")
    return {"sent": len(messages)}
