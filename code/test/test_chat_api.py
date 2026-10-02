"""
Tests for the chat API's request/response contract.

Covers three fixes:

1. ``PATCH /chat-info/contact`` wrote to a column named ``name``, but the table
   has ``contact_name`` -- the endpoint could never succeed.
2. ``/chat`` and ``/history`` ran their database-backed validation outside the
   try/except, so a database outage escaped as a bare framework error with a
   different JSON shape than every other failure.
3. That fix must not relabel malformed requests: a body that is not JSON still
   has to come back as 415, not 500.
"""
import sys
import os
import uuid
from unittest.mock import patch

import pytest
from psycopg.rows import dict_row

sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), "..")))

import db_pool  # noqa: E402

ORIGIN = {"Origin": "http://example.com"}


@pytest.fixture
def session_id():
    """A chat_info row to update, removed afterwards."""
    sid = str(uuid.uuid4())
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute(
                "INSERT INTO chat_info (session_id) VALUES (%s) "
                "ON CONFLICT (session_id) DO NOTHING;",
                (sid,),
            )
    yield sid
    with db_pool.get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM chat_info WHERE session_id = %s;", (sid,))


# ---------------------------------------------------------------------------
# PATCH /chat-info/contact
# ---------------------------------------------------------------------------

class TestContactUpdate:
    def test_contact_details_are_saved(self, client, session_id):
        """The endpoint used to fail every time on an undefined column."""
        response = client.patch(
            "/chat-info/contact",
            json={
                "session_id": session_id,
                "name": "Ada Lovelace",
                "email": "ada@example.com",
                "mobile": "+441234567",
                "country": "UK",
            },
        )
        assert response.status_code == 200
        assert response.get_json()["success"] is True

        with db_pool.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT contact_name, email, mobile, country "
                    "FROM chat_info WHERE session_id = %s;",
                    (session_id,),
                )
                row = cur.fetchone()

        assert row == {
            "contact_name": "Ada Lovelace",
            "email": "ada@example.com",
            "mobile": "+441234567",
            "country": "UK",
        }

    def test_omitted_fields_keep_their_value(self, client, session_id):
        """COALESCE must not blank out details a later call leaves out."""
        client.patch("/chat-info/contact",
                     json={"session_id": session_id, "name": "Ada", "country": "UK"})
        client.patch("/chat-info/contact",
                     json={"session_id": session_id, "email": "ada@example.com"})

        with db_pool.get_connection() as conn:
            with conn.cursor(row_factory=dict_row) as cur:
                cur.execute(
                    "SELECT contact_name, email, country FROM chat_info "
                    "WHERE session_id = %s;",
                    (session_id,),
                )
                row = cur.fetchone()

        assert row["contact_name"] == "Ada"
        assert row["country"] == "UK"
        assert row["email"] == "ada@example.com"


# ---------------------------------------------------------------------------
# Malformed requests
# ---------------------------------------------------------------------------

class TestMalformedRequests:
    """
    Wrapping the handlers in try/except must not swallow the framework's own
    HTTP errors and report them as 500.
    """

    @pytest.mark.parametrize(
        "method,path",
        [
            ("post", "/chat"),
            ("patch", "/chat-info"),
            ("patch", "/chat-info/contact"),
        ],
    )
    def test_non_json_body_is_415_not_500(self, client, method, path):
        response = getattr(client, method)(
            path, data="this is not json", content_type="text/plain"
        )
        assert response.status_code == 415

    def test_invalid_session_id_is_still_400_with_a_reason(self, client):
        response = client.get(
            "/history", query_string={"session_id": "not-a-uuid"}, headers=ORIGIN
        )
        assert response.status_code == 400
        assert response.get_json()["error"] == "Invalid session id format"


# ---------------------------------------------------------------------------
# /health
# ---------------------------------------------------------------------------

class TestHealthSeesTheSchema:
    """
    SELECT 1 touches no table, so it passes against a database with no schema
    while every data endpoint returns 500. The probe asks for the tables.
    """

    def test_a_missing_table_is_a_503(self, client):
        with patch("api.health.missing_tables", return_value=["prompts"]):
            response = client.get("/health")
        assert response.status_code == 503
        body = response.get_json()
        assert body["database"] == "connected"
        assert "prompts" in body["schema_error"]

    def test_a_complete_schema_is_a_200(self, client):
        response = client.get("/health")
        assert response.status_code == 200
        assert "schema_error" not in response.get_json()


# ---------------------------------------------------------------------------
# Behaviour when the database is unavailable
# ---------------------------------------------------------------------------

class TestDatabaseOutageResponses:
    """
    Every endpoint must fail in the same JSON shape, so the frontend has one
    error path. The database-backed validation step used to bypass it.
    """

    ENDPOINTS = [
        ("get", "/history", {"query_string": {"session_id": str(uuid.uuid4())},
                             "headers": ORIGIN}),
        ("post", "/chat", {"json": {"input": "hi",
                                    "session_id": str(uuid.uuid4()),
                                    "request_type": "sales"},
                           "headers": ORIGIN}),
    ]

    @pytest.mark.parametrize("method,path,kwargs", ENDPOINTS)
    def test_outage_returns_the_standard_error_shape(self, client, method, path, kwargs):
        outage = db_pool.DatabaseUnavailable("no database connection")
        with patch("api.validators._find_domain_key", side_effect=outage):
            response = getattr(client, method)(path, **kwargs)

        assert response.status_code == 500
        assert response.content_type.startswith("application/json")
        body = response.get_json()
        assert body == {
            "success": False,
            "error": "Sorry, something went wrong. Please try again later.",
        }
