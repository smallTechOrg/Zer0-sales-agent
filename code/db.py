"""
Database bootstrap: make the database and its tables if they do not exist.
Connections come from the pool in db_pool.
"""
import logging
import threading
import time

import psycopg

import config
from db_pool import RETRYABLE_ERRORS, run_with_retry
from langchain_postgres import PostgresChatMessageHistory
from prompts_table import check_and_insert_default_prompts

log = logging.getLogger(__name__)

_bootstrap_lock = threading.Lock()
_bootstrap_thread = None


def ensure_database_exists(database_url=config.DATABASE_URL, database=config.db_name):
    """
    Connect to the 'postgres' system database and create *database* if missing.

    This one cannot use the pool: the pool targets the application database,
    which may not exist yet.
    """
    base_url = database_url.rsplit('/', 1)[0]
    postgres_url = f"{base_url}/postgres"

    with psycopg.connect(postgres_url, connect_timeout=config.DB_CONNECT_TIMEOUT) as temp_conn:
        temp_conn.autocommit = True
        with temp_conn.cursor() as cur:
            cur.execute("SELECT 1 FROM pg_database WHERE datname = %s", (database,))
            if not cur.fetchone():
                cur.execute(f'CREATE DATABASE "{database}"')
                log.info("Database '%s' created successfully.", database)
            else:
                log.info("Database '%s' already exists.", database)


def ensure_chat_table_exists(sync_connection, table):
    """
    Use LangChain's helper to make sure the chat history table exists.
    """
    PostgresChatMessageHistory.create_tables(sync_connection, table)
    log.info("Table '%s' created or verified.", table)


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

        # Summary columns written by periodic_summary. Older databases call
        # summary_generated_at summary_updated_at: rename it to keep the data.
        cur.execute(
            """
            DO $$
            BEGIN
                IF EXISTS (SELECT 1 FROM information_schema.columns
                           WHERE table_name = 'chat_info' AND column_name = 'summary_updated_at')
                   AND NOT EXISTS (SELECT 1 FROM information_schema.columns
                                   WHERE table_name = 'chat_info' AND column_name = 'summary_generated_at')
                THEN
                    ALTER TABLE chat_info RENAME COLUMN summary_updated_at TO summary_generated_at;
                END IF;
            END $$;
            """
        )
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS summary TEXT;")
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS summary_generated_at TIMESTAMPTZ;")
        cur.execute("ALTER TABLE chat_info ADD COLUMN IF NOT EXISTS summary_notified_at TIMESTAMPTZ;")

    log.info("Table 'chat_info' created/verified successfully.")


def ensure_prompts_table_exists(sync_connection):
    """Make the prompts table. Columns: domain, agent_type, type, text."""
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

    log.info("Table 'prompts' created/verified successfully.")


def ensure_domains_table_exists(sync_connection):
    """Make the domains table. Columns: key, address, parent."""
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

    log.info("Table 'domains' created/verified successfully.")


def _create_schema(sync_connection):
    """
    Create every table this app needs, on the given connection.

    Each step is committed as it completes so a failure in a later step cannot
    roll back the tables the earlier ones already created.
    """
    steps = (
        lambda conn: ensure_chat_table_exists(conn, config.table_name),
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
        # A managed database usually denies the 'postgres' database. This is
        # not a fault: the schema step below still works.
        log.info("Skipping the database-creation check: %s", exc)

    run_with_retry(_create_schema, attempts=1, label="schema bootstrap")


def _bootstrap_loop(limit=config.DB_BOOTSTRAP_ATTEMPTS):
    """Make the schema. Retry with an increasing delay until it is done."""
    delay = config.DB_RETRY_BASE_DELAY
    for attempt in range(1, limit + 1):
        try:
            _bootstrap_once()
            log.info("Database schema ready (attempt %d).", attempt)
            return
        except RETRYABLE_ERRORS as exc:
            log.warning(
                "Schema bootstrap attempt %d/%d failed: %s", attempt, limit, exc
            )
        except Exception:
            # Continue to retry. Some faults are temporary but are not
            # connection errors.
            log.exception(
                "Schema bootstrap attempt %d/%d failed unexpectedly", attempt, limit
            )
        time.sleep(delay)
        delay = min(delay * 2, config.DB_RETRY_MAX_DELAY)

    log.error(
        "Giving up on schema bootstrap after %d attempts. The app stays up.", limit
    )


def init_db(wait=config.DB_STARTUP_WAIT):
    """
    Make the database and tables if they do not exist. Never raise, and never
    wait more than *wait* seconds. A background thread continues the work.
    """
    global _bootstrap_thread

    with _bootstrap_lock:
        if _bootstrap_thread is None:
            _bootstrap_thread = threading.Thread(
                target=_bootstrap_loop, name="db-bootstrap", daemon=True
            )
            _bootstrap_thread.start()

    _bootstrap_thread.join(timeout=wait)
