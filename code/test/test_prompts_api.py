import pytest
from app import app
from db_pool import get_connection

@pytest.fixture
def client():
    app.config['TESTING'] = True
    with app.test_client() as client:
        yield client

@pytest.fixture(autouse=True)
def cleanup_prompts():
    # Cleanup any test prompts before and after each test
    yield
    with get_connection() as conn:
        with conn.cursor() as cur:
            cur.execute("DELETE FROM prompts WHERE domain='testdomain' AND agent_type='testagent';")

def test_get_prompts(client):
    response = client.get('/prompts')
    assert response.status_code == 200
    data = response.get_json()
    assert 'prompts' in data
    assert isinstance(data['prompts'], list)

def test_post_prompt_create(client):
    payload = {
        "domain": "testdomain",
        "agent_type": "testagent",
        "type": "testtype",
        "text": "Test prompt text"
    }
    response = client.post('/prompt', json=payload)
    assert response.status_code == 200
    data = response.get_json()
    assert data["success"] is True
    assert "Prompt created/updated." in data["message"]

def test_post_prompt_missing_fields(client):
    payload = {
        "domain": "testdomain",
        "agent_type": "testagent",
        # missing 'type' and 'text'
    }
    response = client.post('/prompt', json=payload)
    assert response.status_code == 400
    data = response.get_json()
    assert data["success"] is False
    assert "Missing required fields" in data["error"]
