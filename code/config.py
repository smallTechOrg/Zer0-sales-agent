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

# Set by Werkzeug in the process that serves requests when the debug reloader
# is on. The watcher parent does not have it. See the __main__ block in app.py.
WERKZEUG_RUN_MAIN = os.getenv("WERKZEUG_RUN_MAIN") == "true"

# ---------------------------------------------------------------------------
# LLM (Groq)
# ---------------------------------------------------------------------------
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL_NAME = os.environ.get("GROQ_MODEL_NAME", "openai/gpt-oss-120b")  # default if not set
GROQ_BACKUP_MODEL_NAME = os.environ.get("GROQ_BACKUP_MODEL_NAME")

# ---------------------------------------------------------------------------
# Chat
# ---------------------------------------------------------------------------
max_input_length = int(os.getenv("MAX_INPUT_LENGTH", "10000"))
DEFAULT_DOMAIN = os.getenv("DEFAULT_DOMAIN", "COMMON")

# ---------------------------------------------------------------------------
# Slack (daily summary)
# ---------------------------------------------------------------------------
# Incoming webhook for the channel that receives the daily chat summary.
# Unset: the summary job runs and saves to the database, and skips Slack.
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL")
# Seconds to wait for one webhook POST.
SLACK_TIMEOUT = float(os.getenv("SLACK_TIMEOUT", "10"))
# The Slack message header links here.
DASHBOARD_URL = os.getenv("DASHBOARD_URL", "https://zero.smalltech.in/dashboard?h=khuljasimsim")
# How many days back the summary job looks for sessions without a summary.
# Only a cap on retries: a session is summarised once and then left alone, so
# the first run after a long outage does not go through the whole history.
SUMMARY_WINDOW_DAYS = int(os.getenv("SUMMARY_WINDOW_DAYS", "7"))

# ---------------------------------------------------------------------------
# Scheduler (see code/scheduler.py)
# ---------------------------------------------------------------------------
# When the daily summary job runs, as a five-field cron pattern
# (minute hour day month weekday). Default: at the start of every hour.
DAILY_SUMMARY_CRON = os.getenv("DAILY_SUMMARY_CRON", "0 * * * *")
# Timezone the pattern is read in. Without this a server running in UTC
# would fire at the wrong local time.
DAILY_SUMMARY_TIMEZONE = os.getenv("DAILY_SUMMARY_TIMEZONE", "Asia/Kolkata")

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
# Database connection pool (see code/db_pool.py)
# ---------------------------------------------------------------------------

# The database is a small shared instance. Keep the pool small. Do not set the
# maximum to 1: one slow query then blocks all requests.
DB_POOL_MIN_SIZE = int(os.getenv("DB_POOL_MIN_SIZE", "1"))
DB_POOL_MAX_SIZE = int(os.getenv("DB_POOL_MAX_SIZE", "2"))

# Seconds that one attempt waits for a connection. The pool retries the connect
# in this time, so a request can wait for a database restart.
DB_POOL_TIMEOUT = float(os.getenv("DB_POOL_TIMEOUT", "5"))

# Replace old connections. A firewall or the server can close one without
# telling the client.
DB_POOL_MAX_IDLE = float(os.getenv("DB_POOL_MAX_IDLE", "300"))
DB_POOL_MAX_LIFETIME = float(os.getenv("DB_POOL_MAX_LIFETIME", "3600"))

DB_CONNECT_TIMEOUT = int(os.getenv("DB_CONNECT_TIMEOUT", "10"))

# Maximum time for one query. A busy database can accept a connection and then
# not answer. Without this limit the request waits forever.
DB_STATEMENT_TIMEOUT_MS = int(os.getenv("DB_STATEMENT_TIMEOUT_MS", "10000"))

# Maximum time to wait for a lock. Another process can hold a lock during a
# migration. Fail before the statement timeout.
DB_LOCK_TIMEOUT_MS = int(os.getenv("DB_LOCK_TIMEOUT_MS", "3000"))

# Close a transaction that stays open. get_connection usually does this.
DB_IDLE_TX_TIMEOUT_MS = int(os.getenv("DB_IDLE_TX_TIMEOUT_MS", "30000"))

# Find a server that stopped to answer. The server applies the statement
# timeout, so it cannot help here. Linux applies tcp_user_timeout. Windows
# ignores it.
DB_TCP_USER_TIMEOUT_MS = int(os.getenv("DB_TCP_USER_TIMEOUT_MS", "20000"))
DB_KEEPALIVES_IDLE = int(os.getenv("DB_KEEPALIVES_IDLE", "10"))
DB_KEEPALIVES_INTERVAL = int(os.getenv("DB_KEEPALIVES_INTERVAL", "5"))
DB_KEEPALIVES_COUNT = int(os.getenv("DB_KEEPALIVES_COUNT", "3"))

# Maximum time the pool waits between reconnect attempts. The psycopg default
# is 300s, which keeps the app down after the database comes back.
DB_RECONNECT_TIMEOUT = float(os.getenv("DB_RECONNECT_TIMEOUT", "10"))

# Retries for connection failures. The longest wait is approximately
# DB_RETRY_ATTEMPTS x DB_POOL_TIMEOUT plus the delays.
DB_RETRY_ATTEMPTS = int(os.getenv("DB_RETRY_ATTEMPTS", "3"))
DB_RETRY_BASE_DELAY = float(os.getenv("DB_RETRY_BASE_DELAY", "0.5"))
DB_RETRY_MAX_DELAY = float(os.getenv("DB_RETRY_MAX_DELAY", "4"))

# Seconds between pool checks. A check replaces connections that the server
# closed, so the pool refills without an API request. The /health probe does
# the same work on each call, so this is a backstop for when it does not run.
# 0 stops the checks.
DB_CHECK_INTERVAL = float(os.getenv("DB_CHECK_INTERVAL", "300"))

# Budget for the /health probe. One attempt.
DB_HEALTH_TIMEOUT = float(os.getenv("DB_HEALTH_TIMEOUT", str(DB_POOL_TIMEOUT)))

# Shows in pg_stat_activity. It identifies this app on the shared server.
DB_APPLICATION_NAME = os.getenv("DB_APPLICATION_NAME", "zero-sales-agent")

# Seconds that startup waits for the schema, then the number of retries.
DB_STARTUP_WAIT = float(os.getenv("DB_STARTUP_WAIT", "5"))
DB_BOOTSTRAP_ATTEMPTS = int(os.getenv("DB_BOOTSTRAP_ATTEMPTS", "600"))



class agent_type(str, Enum):
    SALES = "sales"
    GENERIC = "generic"

# Allowed statuses in dashboard
class status_type(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    QUALIFYING = "QUALIFYING"
