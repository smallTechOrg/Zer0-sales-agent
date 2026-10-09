import json
import logging
from pathlib import Path
from functools import lru_cache
from config import agent_type, DEFAULT_DOMAIN
from db_pool import with_connection

log = logging.getLogger(__name__)


def load_json(path: Path):
    """
    Load file content. If valid JSON, return as JSON string. Otherwise return as plain text.
    """
    try:
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        # Try to parse as JSON; if it succeeds, return canonical JSON string
        try:
            data = json.loads(content)
            return json.dumps(data)
        except json.JSONDecodeError:
            # Not JSON — return plain text as-is
            return content.strip()
    except Exception:
        log.exception("Failed to load file %s", path)
        return None

def check_and_insert_default_prompts(sync_connection):
    """
    Check if the prompts table is empty, and insert default rows only if empty.

    Runs as part of the schema bootstrap on a caller-supplied connection, so it
    neither commits nor rolls back: the bootstrap owns the transaction.
    """
    with sync_connection.cursor() as cur:
        # Check if the table is empty
        cur.execute("SELECT COUNT(*) FROM prompts;")
        count = cur.fetchone()[0]
        if count != 0:
            log.info("Prompts table already contains data. No insertion needed.")
            return

        log.info("Prompts table is empty. Inserting default prompts...")

        default_prompts = [
            (DEFAULT_DOMAIN, 'generic', 'fetch-name', Path("prompts/name_prompt.txt")),
            (DEFAULT_DOMAIN, 'sales', 'fetch-contact-info',  Path("prompts/info_prompt.txt")),
            (DEFAULT_DOMAIN, 'sales', 'base-prompt', Path("prompts/sales_prompt.txt")),
            (DEFAULT_DOMAIN, 'sales', 'company', Path("prompts/company.txt")),
            (DEFAULT_DOMAIN, 'sales', 'intro-message', Path("prompts/intro_message.txt")),
            (DEFAULT_DOMAIN, 'generic', 'system', Path("prompts/generic_prompt.txt")),
        ]

        for domain, agent_type, prompt_type, text in default_prompts:

            # If text is a file path -> load JSON
            if isinstance(text, Path):
                text_json = load_json(text)
                if text_json is None:
                    log.warning("Skipping insertion for %s", text)
                    continue
                text_to_insert = text_json
            else:
                text_to_insert = text

            # ON CONFLICT: the COUNT above is not a lock. Two processes can
            # both read an empty table and both insert.
            cur.execute("""
                INSERT INTO prompts (domain, agent_type, type, text)
                VALUES (%s, %s, %s, %s)
                ON CONFLICT (domain, agent_type, type) DO NOTHING;
            """, (domain, agent_type, prompt_type, text_to_insert))

        log.info("Default prompts inserted successfully.")


def check_and_insert_default_domains(sync_connection):
    with sync_connection.cursor() as cur:
        create_table_sql = """
        CREATE TABLE IF NOT EXISTS domains (
            id SERIAL PRIMARY KEY,
            key TEXT NOT NULL,
            address TEXT,
            parent INTEGER REFERENCES domains(id) ON DELETE SET NULL,
            created_at TIMESTAMP WITH TIME ZONE DEFAULT CURRENT_TIMESTAMP,
            UNIQUE (key, address)
        );
        
        -- Insert a default row if it doesn't exist
        INSERT INTO domains (key, address, parent)
        VALUES ('COMMON', 'example.com', NULL)
        ON CONFLICT (key, address) DO NOTHING;
        """
        cur.execute(create_table_sql)
        sync_connection.commit()

# --- New prompt API helpers ---
@with_connection
def get_all_prompts(conn):
    """
    Fetch all prompts from the prompts table.
    Returns a list of dicts.
    """
    with conn.cursor() as cur:
        cur.execute("SELECT id, domain, agent_type, type, text, created_at FROM prompts;")
        rows = cur.fetchall()
        columns = [desc[0] for desc in cur.description]
        return [dict(zip(columns, row)) for row in rows]


@with_connection
def upsert_prompt(conn, domain, agent_type, prompt_type, text):
    """
    Insert or update a prompt based on (domain, agent_type, type).
    Returns True if successful, False otherwise.
    """
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO prompts (domain, agent_type, type, text)
            VALUES (%s, %s, %s, %s)
            ON CONFLICT (domain, agent_type, type)
            DO UPDATE SET text = EXCLUDED.text, created_at = CURRENT_TIMESTAMP;
            """,
            (domain, agent_type, prompt_type, text)
        )
        return True
