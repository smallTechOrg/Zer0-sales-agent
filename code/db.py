"""
Database bootstrap: create the database and its tables if they are missing.

Connections come from the shared pool in :mod:`db_pool`; this module owns only
the schema. It exports ``get_connection`` / ``run_with_retry`` / ``ping`` as a
convenience so callers have a single obvious import for database access.
"""
import logging
import threading
import time

import psycopg

from config import (
    DATABASE_URL,
    DB_BOOTSTRAP_ATTEMPTS,
    DB_CONNECT_TIMEOUT,
    DB_RETRY_BASE_DELAY,
    DB_RETRY_MAX_DELAY,
    DB_STARTUP_WAIT,
    db_name,
    table_name,
)
from db_pool import (  # noqa: F401  (re-exported for callers)
    DatabaseUnavailable,
    close_pool,
    get_connection,
    get_pool,
    ping,
    pool_status,
    run_with_retry,
    with_connection,
)
from db_pool import RETRYABLE_ERRORS
from langchain_postgres import PostgresChatMessageHistory
from prompts_table import check_and_insert_default_prompts

logger = logging.getLogger(__name__)

__all__ = [
    # Re-exported pool API, so callers have one obvious import for DB access.
    "DatabaseUnavailable",
    "close_pool",
    "get_connection",
    "get_pool",
    "ping",
    "pool_status",
    "run_with_retry",
    "with_connection",
    # Schema
    "init_db",
    "schema_ready",
    "table_name",
]


_schema_ready = threading.Event()
_bootstrap_lock = threading.Lock()
_bootstrap_thread = None


def schema_ready() -> bool:
    """True once the tables have been created or verified at least once."""
    return _schema_ready.is_set()


def ensure_database_exists(database_url=DATABASE_URL, database=db_name):
    """
    Connect to the 'postgres' system database and create *database* if missing.

    This one cannot use the pool: the pool targets the application database,
    which may not exist yet.
    """
    base_url = database_url.rsplit('/', 1)[0]
    postgres_url = f"{base_url}/postgres"

    with psycopg.connect(postgres_url, connect_timeout=DB_CONNECT_TIMEOUT) as temp_conn:
        temp_conn.autocommit = True
        with temp_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{database}"')
                logger.info("Database '%s' created successfully.", database)
            else:
                logger.info("Database '%s' already exists.", database)


def ensure_chat_table_exists(sync_connection, table):
    """
    Use LangChain's helper to make sure the chat history table exists.
    """
    PostgresChatMessageHistory.create_tables(sync_connection, table)
    logger.info("Table '%s' created or verified.", table)


def ensure_summaries_table_exists(sync_connection):
    """
    Create the chat_info table for storing lead information and summaries.
    """
    with sync_connection.cursor() as cur:
        # Create the chat_info table
        create_table_query = """
        CREATE TABLE IF NOT EXISTS chat_info (
            id SERIAL PRIMARY KEY,
            session_id TEXT NOT NULL,
            contact_name TEXT,
            email TEXT,
            mobile TEXT,
            country TEXT,
            request_type TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            metadata JSONB DEFAULT '{}',

            -- Add constraint to prevent duplicate summaries for same session
            UNIQUE(session_id)
        );

        -- Create indexes for efficient querying
        CREATE INDEX IF NOT EXISTS idx_chat_info_session_id
        ON chat_info(session_id);


        CREATE INDEX IF NOT EXISTS idx_chat_info_created_at
        ON chat_info(created_at);
        """

        cur.execute(create_table_query)

        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS contact_name TEXT;")
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS email TEXT;")
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS country TEXT;")
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS mobile TEXT;")
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS request_type TEXT;")
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP;")
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS metadata JSONB DEFAULT '{}'::jsonb;")

        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS status TEXT DEFAULT 'OPEN';")
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS remarks TEXT;")
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS domain TEXT;")

        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS is_active BOOLEAN DEFAULT TRUE;")

    logger.info("Table 'chat_info' created/verified successfully.")


def ensure_prompts_table_exists(sync_connection):
    """
    Create or verify a 'prompts' table with columns:
      - domain : domain, under which prompt is, example as common, smalltech, client
      - agent_type -- Determines the type of agent, example as Sales, generic
      - type -- What the prompt use for, example as name_prompt, sales prompt, info_prompt, generic
      - text -- prompt itself
    """
    with sync_connection.cursor() as cur:
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS prompts (
            id SERIAL PRIMARY KEY,
            domain TEXT DEFAULT 'common',
            agent_type TEXT NOT NULL,
            type TEXT NOT NULL,
            "text" TEXT,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (domain, agent_type, type)
        );
        """
        cur.execute(create_table_sql)

        # Add unique constraint if it doesn't exist (for existing databases)
        alter_table_sql = """
        DO $$
        BEGIN
            IF NOT EXISTS (
                SELECT 1 FROM pg_constraint
                WHERE conname = 'prompts_domain_agent_type_type_key'
            ) THEN
                ALTER TABLE prompts
                ADD CONSTRAINT prompts_domain_agent_type_type_key
                UNIQUE (domain, agent_type, type);
            END IF;
        END $$;
        """
        cur.execute(alter_table_sql)

    logger.info("Table 'prompts' created/verified successfully.")


def ensure_domains_table_exists(sync_connection):
    """
    Create or verify a 'domains' table with columns:
      - key : unique identifier example common, smalltech, client
      - address : url for the example domain smalltech.in
      - parent key : parent key for domain key
    Note: column name 'key' will be created quoted to avoid ambiguity; it's still a valid column name.
    """
    with sync_connection.cursor() as cur:
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS domains (
            id SERIAL PRIMARY KEY,
            key TEXT NOT NULL,
            address TEXT UNIQUE NOT NULL,
            parent INTEGER REFERENCES domains(id) ON DELETE SET NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP
        );
        -- Insert a default row if it doesn't exist. Without DO NOTHING this
        -- raises a unique violation on every restart after the first.
        INSERT INTO domains (key, address, parent)
        VALUES ('COMMON', 'example.com', NULL)
        ON CONFLICT (address) DO NOTHING;
        """
        cur.execute(create_table_sql)

    logger.info("Table 'domains' created/verified successfully.")


def _create_schema(sync_connection):
    """
    Create every table this app needs, on the given connection.

    Each step is committed as it completes so a failure in a later step cannot
    roll back the tables the earlier ones already created.
    """
    steps = (
        lambda conn: ensure_chat_table_exists(conn, table_name),
        ensure_summaries_table_exists,
        ensure_prompts_table_exists,
        ensure_domains_table_exists,
        check_and_insert_default_prompts,
    )
    for step in steps:
        step(sync_connection)
        sync_connection.commit()


def _bootstrap_once():
    """One full bootstrap pass. Raises if the database is unreachable."""
    try:
        ensure_database_exists()
    except Exception as exc:
        # A shared or managed cloud database usually denies access to the
        # 'postgres' maintenance database, and the application database is
        # provisioned for us anyway. That is not a failure: if the app database
        # is reachable, the schema step below works regardless.
        logger.info("Skipping the database-creation check: %s", exc)

    run_with_retry(_create_schema, attempts=1, label="schema bootstrap")
    _schema_ready.set()


def _bootstrap_loop(limit=DB_BOOTSTRAP_ATTEMPTS):
    """
    Bootstrap the schema, retrying with backoff until it works.

    Runs on a daemon thread so a database that is down never holds up startup.
    Stops early on an error that retrying cannot fix, such as missing CREATE
    privileges, rather than logging the same failure sixty times.
    """
    delay = DB_RETRY_BASE_DELAY
    for attempt in range(1, limit + 1):
        try:
            _bootstrap_once()
            logger.info("Database schema ready (attempt %s).", attempt)
            return
        except RETRYABLE_ERRORS as exc:
            logger.warning(
                "Schema bootstrap attempt %s/%s failed: %s", attempt, limit, exc
            )
        except Exception:
            logger.exception(
                "Schema bootstrap failed for a reason retrying will not fix. "
                "The app stays up; fix the schema or privileges and restart."
            )
            return
        time.sleep(delay)
        delay = min(delay * 2, DB_RETRY_MAX_DELAY)

    logger.error(
        "Giving up on schema bootstrap after %s attempts. The app stays up; "
        "/health will keep reporting the database as down.",
        limit,
    )


def init_db(wait=DB_STARTUP_WAIT):
    """
    Ensure the database and tables exist. Returns True if the schema is ready.

    Never raises, and never blocks longer than *wait* seconds. The work runs on
    a background thread that we simply wait on: when the database is up it
    finishes in milliseconds, so startup is effectively synchronous and tests
    stay deterministic. When the database is down we stop waiting and let Flask
    start serving, so ``/health`` can report the outage within seconds instead
    of the process sitting unresponsive for a minute -- and the thread keeps
    retrying, so the schema lands as soon as the database comes back, with no
    restart or redeploy.
    """
    global _bootstrap_thread

    if _schema_ready.is_set():
        return True

    with _bootstrap_lock:
        if _bootstrap_thread is None:
            _bootstrap_thread = threading.Thread(
                target=_bootstrap_loop, name="db-bootstrap", daemon=True
            )
            _bootstrap_thread.start()

    if not _schema_ready.wait(timeout=wait):
        logger.error(
            "Database not ready after %ss. Starting anyway and retrying in the "
            "background; /health will report the database as down until it is.",
            wait,
        )
    return _schema_ready.is_set()
