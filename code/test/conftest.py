import sys
import os
import pytest

# Add the project's root directory (the parent of 'test') to Python's search path
# This allows pytest to find and import the 'app' module.
sys.path.insert(0, os.path.abspath(os.path.join(os.path.dirname(__file__), '..')))

# Now that the path is set, we can import the app. Importing app configures
# logging, so anything a test logs is formatted the way the deployed app is.
from app import app as flask_app

@pytest.fixture
def app():
    """Create and configure a new app instance for each test."""
    yield flask_app

@pytest.fixture
def client(app):
    """A test client for the app."""
    return app.test_client()
