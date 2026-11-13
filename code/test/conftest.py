import sys
import os
import pytest

# Add the project's root directory (the parent of 'test') to Python's search path
# This allows pytest to find and import the 'app' module.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Tests always run as a local deployment on chatdb, whatever .env says, so they
# never touch the staging or production database. Set before config is loaded:
# load_dotenv() does not override these.
os.environ["APP_ENV"] = "local"
os.environ["DB_NAME"] = "chatdb"
os.environ["DASHBOARD_URL"] = "http://localhost:3001/dashboard"

# Now that the path is set, we can import the app
from app import app as flask_app

@pytest.fixture
def app():
    """Create and configure a new app instance for each test."""
    yield flask_app

@pytest.fixture
def client(app):
    """A test client for the app."""
    return app.test_client()
