from contextlib import contextmanager
from http import HTTPStatus
from typing import List

from langchain_core.messages import BaseMessage
from langchain_postgres import PostgresChatMessageHistory

from config import table_name
from db_pool import get_connection
from system_prompt import get_prompt


# ``PostgresChatMessageHistory.__init__`` only checks that *a* connection was
# supplied. This stands in for one between operations; every method below swaps
# in a real pooled connection before touching the database.
_UNBOUND = object()


class PooledChatMessageHistory(PostgresChatMessageHistory):
    """
    Chat history that borrows its connection from the pool per operation.

    Upstream keeps one ``psycopg.Connection`` for the lifetime of the object and
    commits on it. That cannot work with a pool: the connection would never go
    back, and once a database restart killed it every later call on that object
    would fail. Each method here checks a connection out, delegates to the
    upstream SQL, and returns it.

    Instances are per-request (``get_session_history`` builds a new one on every
    call), so swapping ``_connection`` in and out is not shared across threads.
    """

    def __init__(self, table: str, session_id: str) -> None:
        super().__init__(table, session_id, sync_connection=_UNBOUND)

    @contextmanager
    def _borrowed_connection(self):
        """Hold a pooled connection for the duration of one operation."""
        with get_connection() as conn:
            self._connection = conn
            try:
                yield
            finally:
                self._connection = _UNBOUND

    def get_messages(self) -> List[BaseMessage]:
        with self._borrowed_connection():
            return super().get_messages()

    def add_messages(self, messages) -> None:
        with self._borrowed_connection():
            super().add_messages(messages)

    def clear(self) -> None:
        with self._borrowed_connection():
            super().clear()


def get_session_history(session_id):
    return PooledChatMessageHistory(table_name, session_id)


def _message_mapping(messages):
    return [
        {
            "type": msg.type,   # "human" or "ai"
            "content": msg.content
        }
        for msg in messages
    ]

def get_history(session_id: str, domain):
    """
    Retrieve chat history for a session_id as a list of dicts.

    Failures propagate: the endpoint turns them into the one error shape this
    API uses. Returning an error dict from here as well gave the same endpoint
    two different failure bodies depending on where it broke.
    """
    history = get_session_history(session_id)
    status = HTTPStatus.OK
    # Read once: every access to .messages is now a round trip to the pool.
    messages = history.messages
    if not messages:
        # session exists
        first_message = get_prompt(domain, "sales", "intro-message")
        history.add_ai_message(first_message)
        messages = history.messages
        status = HTTPStatus.CREATED

    return {
        "session_id": session_id,
        "history": _message_mapping(messages)
    }, status
