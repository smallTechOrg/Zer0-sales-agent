"""
Every setting this app reads from the environment is declared here.

Nothing else calls ``os.getenv``. Modules import the name they need from this
module, so there is one place to see what is configurable, what its default is,
and what belongs in ``.env`` -- which carries values only, never definitions.
"""
import os
from dotenv import load_dotenv
from enum import Enum
import requests
load_dotenv()

# ---------------------------------------------------------------------------
# Flask
# ---------------------------------------------------------------------------
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

# Only used by `python app.py`. Deployment runs `flask run --port=5000`, which
# sets the port itself.
PORT = int(os.getenv("PORT", "5001"))

# ---------------------------------------------------------------------------
# LLM (Groq)
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL_NAME = os.environ.get("GROQ_MODEL_NAME", "meta-llama/llama-4-scout-17b-16e-instruct")  # default if not set
GROQ_BACKUP_MODEL_NAME = os.environ.get("GROQ_BACKUP_MODEL_NAME")

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
max_input_length = int(os.getenv("MAX_INPUT_LENGTH", "10000"))
DEFAULT_DOMAIN = os.getenv("DEFAULT_DOMAIN", "COMMON")

# ---------------------------------------------------------------------------
# Database
# ---------------------------------------------------------------------------
db_name = 'chatdb'
table_name = os.getenv("DB_TABLE_NAME", "chat_table")
DATABASE_URL = os.getenv('DATABASE_URL')
# For cloud deployment, lets create different db for production and staging

def get_db_name():
    #db based on Production and staging based on GitHub branch name
    METADATA_URL = "http://metadata.google.internal/computeMetadata/v1/instance/attributes/BRANCH_NAME"
    headers = {"Metadata-Flavor": "Google"}
    try:
        response = requests.get(METADATA_URL, headers=headers, timeout=2)
        if response.status_code == 200:
            branch_name  = response.text.strip()
            print(f"Detected branch: {branch_name}")
            if branch_name == 'main':
                return 'prod_chat_db'
            else:
                return 'staging_chat_db'
    except Exception as e:
        print(f"Could not fetch metadata (defaulting to local DB): {e}")

    # Fallback if metadata not found or error occurs
    return db_name

db_name = get_db_name()
DATABASE_URL = DATABASE_URL + db_name

print("Connecting to:", DATABASE_URL)

# ---------------------------------------------------------------------------
# Database connection pool (see code/db_pool.py for how each one is used)
# ---------------------------------------------------------------------------

# Deliberately small: the database is a shared, size-limited cloud instance, so
# this app is a considerate tenant rather than one sized for its own peak. Two
# is enough because no request ever holds a connection across an LLM call --
# every borrow is a single query lasting milliseconds. Raising it is safe;
# lowering it to 1 is not, since a queued request would then wait behind any
# slow query with nothing else to run on.
DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", "2"))

# How long one attempt waits for a working connection. While the database is
# down psycopg keeps retrying the connect inside this window, so this is also
# how long a request rides out a restart before that attempt gives up.
DB_POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "5"))

# Recycle connections so we never hand out one that a firewall, a proxy or the
# server itself has silently closed.
DB_POOL_MAX_IDLE = float(os.getenv("DB_POOL_MAX_IDLE", "300"))
DB_POOL_MAX_LIFETIME = float(os.getenv("DB_POOL_MAX_LIFETIME", "3600"))

# TCP-level connect timeout, so a dead host fails fast instead of hanging.
DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))

# Ceiling on a single statement. Without this, a database that is reachable but
# overloaded -- accepting connections, answering nothing -- parks a request
# forever, and with a small pool a couple of those block every other caller.
# Every query this app runs is a few milliseconds, so 10s is pure headroom.
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "10000"))

# Waiting on a lock is the one slow case that is not this app's fault -- a
# migration or a manual ALTER TABLE elsewhere. Failing at 3s instead of burning
# the whole statement budget keeps both pooled connections from being pinned
# for 10s by someone else's DDL.
DB_LOCK_TIMEOUT_MS = int(os.getenv("DB_LOCK_TIMEOUT_MS", "3000"))

# A transaction should never sit open: get_connection commits or rolls back on
# the way out. This is the backstop if a thread dies mid-transaction, so it
# cannot pin one of our two connections indefinitely.
DB_IDLE_TX_TIMEOUT_MS = int(os.getenv("DB_IDLE_TX_TIMEOUT_MS", "30000"))

# Notice a server that has gone silent rather than waiting on the socket
# forever. statement_timeout is enforced by the server, so it cannot help when
# the server stops answering at all; these are enforced by the kernel.
# tcp_user_timeout bounds unacknowledged data (a hung query) and is a no-op on
# platforms without TCP_USER_TIMEOUT, such as Windows.
DB_TCP_USER_TIMEOUT_MS = int(os.getenv("DB_TCP_USER_TIMEOUT_MS", "20000"))
DB_KEEPALIVES_IDLE = int(os.getenv("DB_KEEPALIVES_IDLE", "10"))
DB_KEEPALIVES_INTERVAL = int(os.getenv("DB_KEEPALIVES_INTERVAL", "5"))
DB_KEEPALIVES_COUNT = int(os.getenv("DB_KEEPALIVES_COUNT", "3"))

# How long the pool's background reconnect keeps backing off (1s, 2s, 4s...)
# before giving up on an attempt. psycopg defaults this to 300s, which means
# that after a long outage the worker can be asleep for a minute or more and
# the app stays down well after PostgreSQL is back. Capping it low keeps the
# backoff short, and the next request schedules a fresh attempt immediately.
DB_RECONNECT_TIMEOUT = float(os.getenv("DB_RECONNECT_TIMEOUT", "10"))

# Retry policy for connection-level failures.
#
# When the database is up, a retry costs nothing: the broken connection is
# dropped and the next one works immediately. When it is down, the worst case a
# caller waits is roughly
#     DB_RETRY_ATTEMPTS * DB_POOL_TIMEOUT + the backoff between attempts
# which with these defaults is about 16s. Long enough to ride out a database
# restart, short enough not to pile up requests behind a real outage.
DB_RETRY_ATTEMPTS = int(os.getenv("DB_RETRY_ATTEMPTS", "3"))
DB_RETRY_BASE_DELAY = float(os.getenv("DB_RETRY_BASE_DELAY", "0.5"))
DB_RETRY_MAX_DELAY = float(os.getenv("DB_RETRY_MAX_DELAY", "4"))

# The health probe gets one normal attempt: long enough to see what a real
# request would see, short enough that an external check gets a prompt 503
# instead of timing out.
DB_HEALTH_TIMEOUT = float(os.getenv("DB_HEALTH_TIMEOUT", str(DB_POOL_TIMEOUT)))

# Shows up in pg_stat_activity, so this app's connections are identifiable on a
# shared instance.
DB_APPLICATION_NAME = os.getenv("DB_APPLICATION_NAME", "ai-agent-boilerplate")

# How long startup waits for the schema before serving anyway, and how many
# times the background bootstrap retries after that.
DB_STARTUP_WAIT = float(os.getenv("DB_STARTUP_WAIT", "5"))
DB_BOOTSTRAP_ATTEMPTS = int(os.getenv("DB_BOOTSTRAP_ATTEMPTS", "60"))


class agent_type(str, Enum):
    SALES = "sales"
    GENERIC = "generic"

# Allowed statuses in dashboard
class status_type(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    QUALIFYING = "QUALIFYING"
