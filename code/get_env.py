from google.cloud import secretmanager
import sys

# CONFIGURE PROJECT AND SECRETS 
PROJECT_ID = "ai-agent-boilerplate0"  
SECRET_MAPPING = {
    "GROQ_API_KEY": "GROQ_API_KEY",           
    "POSTGRES_URL": "POSTGRES_URL",
    "SLACK_WEBHOOK_URL": "SLACK_WEBHOOK_URL"
}
ENV_FILE_PATH = ".env"

def access_secret(secret_id: str, project_id: str, version_id: str = "latest") -> str:
    try:
        client = secretmanager.SecretManagerServiceClient()
        secret_path = f"projects/{project_id}/secrets/{secret_id}/versions/{version_id}"
        response = client.access_secret_version(request={"name": secret_path})
        return response.payload.data.decode("UTF-8")
    except Exception as e:
        print(f"❌ Error accessing secret '{secret_id}': {e}", file=sys.stderr)
        return None
def generate_env_file(secrets: dict, env_path: str = ".env"):
    try:
        # Ensure required secrets exist
        missing = [k for k, v in secrets.items() if not v]
        if missing:
            raise ValueError(f"Missing secrets: {', '.join(missing)}")

        env_content = f"""DEBUG=True
GROQ_API_KEY={secrets['GROQ_API_KEY']}
GROQ_MODEL_NAME=openai/gpt-oss-120b

# PostgreSQL Database Configuration
DATABASE_URL={secrets['POSTGRES_URL']}

# Slack
SLACK_WEBHOOK_URL={secrets['SLACK_WEBHOOK_URL']}
SLACK_TIMEOUT=10
"""

        with open(env_path, "w") as f:
            f.write(env_content)
        print(f"✅ Generated {env_path} with all secrets.")
    except Exception as e:
        print(f"❌ Error generating {env_path}: {e}", file=sys.stderr)
       

if __name__ == "__main__":
    secrets = {}
    for env_var, gcp_secret_name in SECRET_MAPPING.items():
        value = access_secret(gcp_secret_name, PROJECT_ID)
        secrets[env_var] = value

    generate_env_file(secrets, ENV_FILE_PATH)
