# Add other constants as needed
import os
from dotenv import load_dotenv
from enum import Enum
import requests
load_dotenv()
# Flask settings
DEBUG = os.getenv("DEBUG", "False").lower() == "true"

# Fetch API key and LLM model from environment variable set via .env file
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")
GROQ_MODEL_NAME = os.environ.get("GROQ_MODEL_NAME", "openai/gpt-oss-120b")  # default if not set

# Chat input limits
max_input_length = 10000
DEFAULT_DOMAIN = "COMMON"
# Database and table name
db_name = 'chatdb'
table_name  = 'chat_table'
DATABASE_URL = os.getenv('DATABASE_URL')
# For cloud deployment, lets create different db for production and staging

def get_db_name():
    #db based on Production and staging based on GitHub branch name
    METADATA_URL = f"http://metadata.google.internal/computeMetadata/v1/instance/attributes/BRANCH_NAME"
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

class agent_type(str, Enum):
    SALES = "sales"
    GENERIC = "generic"

# Allowed statuses in dashboard
class status_type(str, Enum):
    OPEN = "OPEN"
    CLOSED = "CLOSED"
    QUALIFYING = "QUALIFYING"