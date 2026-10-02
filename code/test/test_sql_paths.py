"""
Smoke test: every database-touching function runs against the real schema.

This exists because of a bug that nothing else would have caught. The contact
update wrote to a column named ``name`` while the table has ``contact_name``,
so ``PATCH /chat-info/contact`` failed on every single call -- and because no
test ever executed that statement, it stayed broken silently.

A query that is never run is a query that is never checked. These tests call
each one once, so a column that does not exist fails here instead of in
production. They assert on the results only lightly; the point is that the SQL
executes and touches the columns it claims to.
"""
import sys
import os
import uuid

import pytest
from psycopg.rows import dict_row

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db_pool  # noqa: E402

AUDIT_PROMPT_DOMAIN = "_sqlpaths_domain"
AUDIT_ADDRESS = "sqlpaths-example.invalid"


@pytest.fixture
def lead():
    """A chat_info row plus its chat history, removed afterwards."""
    session_id = str(uuid.uuid4())
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_info (session_id) VALUES (%s) "
                "ON CONFLICT (session_id) DO NOTHING;",
                (session_id,),
            )
    yield session_id
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_info WHERE session_id = %s;", (session_id,))
            cur.execute("DELETE FROM chat_table WHERE session_id = %s;", (session_id,))


@pytest.fixture(autouse=True)
def cleanup_audit_rows():
    yield
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prompts WHERE domain = %s;", (AUDIT_PROMPT_DOMAIN,))
            cur.execute("DELETE FROM domains WHERE address = %s;", (AUDIT_ADDRESS,))


def row_for(session_id):
    with db_pool.get_connection() as conn:
        with conn.cursor(row_factory=dict_row) as cur:
            cur.execute(
                "SELECT contact_name, email, mobile, country, status, remarks, "
                "request_type, domain FROM chat_info WHERE session_id = %s;",
                (session_id,),
            )
            return cur.fetchone()


# ---------------------------------------------------------------------------
# Reads and writes on chat_info
# ---------------------------------------------------------------------------

class TestChatInfoPaths:
    def test_select_all_leads(self):
        from leads import get_all_chat_info

        records, _ = get_all_chat_info()
        assert isinstance(records, list)

    def test_contact_update_writes_every_column_it_names(self, lead):
        """The statement that was broken: each field must land."""
        from leads_update import update_contact_info

        assert update_contact_info(
            lead, name="Ada", email="ada@example.test",
            mobile="+441234567", country="UK",
        )
        row = row_for(lead)
        assert (row["contact_name"], row["email"], row["mobile"], row["country"]) == (
            "Ada", "ada@example.test", "+441234567", "UK",
        )

    def test_status_update_writes_every_column_it_names(self, lead):
        from leads_update import update_chat_info

        assert update_chat_info(lead, status="QUALIFYING", remarks="note", is_active=True)
        row = row_for(lead)
        assert row["status"] == "QUALIFYING"
        assert row["remarks"] == "note"

    def test_session_request_type_insert(self, lead):
        from conversation_processor.conversation_processor import (
            _update_session_request_type,
        )

        _update_session_request_type(lead, "sales", "COMMON")
        assert row_for(lead) is not None

    def test_detected_info_upsert(self, lead):
        from conversation_processor.conversation_processor import _save_info_to_database

        _save_info_to_database(
            lead,
            {"contact_name": "Grace", "email": "grace@example.test",
             "country": "US", "mobile": "+1555"},
            "the original message", "sales", "COMMON",
        )
        row = row_for(lead)
        assert row["contact_name"] == "Grace"
        assert row["request_type"] == "sales"
        assert row["domain"] == "COMMON"


# ---------------------------------------------------------------------------
# Prompts and domains
# ---------------------------------------------------------------------------

class TestPromptPaths:
    def test_select_all_prompts(self):
        from prompts_table import get_all_prompts

        assert isinstance(get_all_prompts(), list)

    def test_upsert_prompt_round_trips(self):
        from prompts_table import get_all_prompts, upsert_prompt

        assert upsert_prompt(AUDIT_PROMPT_DOMAIN, "agent", "kind", "first")
        assert upsert_prompt(AUDIT_PROMPT_DOMAIN, "agent", "kind", "second")
        stored = [
            p for p in get_all_prompts()
            if p["domain"] == AUDIT_PROMPT_DOMAIN and p["type"] == "kind"
        ]
        assert len(stored) == 1, "the ON CONFLICT clause should update, not duplicate"
        assert stored[0]["text"] == "second"

    def test_prompt_lookups(self):
        from system_prompt import find_parent_key, find_prompt, get_prompt

        assert find_prompt("COMMON", "sales", "base-prompt") is not None
        find_parent_key("COMMON")          # nullable, just has to execute
        assert get_prompt("COMMON", "sales", "system")
        assert get_prompt("COMMON", "sales", "intro-message")


class TestDomainPaths:
    def test_domain_lookup_by_origin(self):
        from api.validators import _find_domain_key

        assert _find_domain_key("example.com") is not None

    def test_repository_crud(self):
        from domains.repository import DomainRepository

        repo = DomainRepository()
        assert isinstance(repo.list_all(), list)
        assert repo.find_by_address("example.com") is not None
        assert repo.find_by_id(1) is not None

        created = repo.create(key="SQLPATHS", address=AUDIT_ADDRESS, parent_id=None)
        assert created["address"] == AUDIT_ADDRESS
        assert repo.find_by_address(AUDIT_ADDRESS)["key"] == "SQLPATHS"


# ---------------------------------------------------------------------------
# Chat history (the pooled LangChain subclass)
# ---------------------------------------------------------------------------

class TestHistoryPaths:
    def test_read_write_and_clear(self, lead):
        from history import get_session_history

        hist = get_session_history(lead)
        assert hist.messages == []

        hist.add_ai_message("hello from the audit")
        messages = hist.messages
        assert len(messages) == 1
        assert messages[0].content == "hello from the audit"

        hist.clear()
        assert hist.messages == []
