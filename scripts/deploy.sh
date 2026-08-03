#!/bin/bash
set -euo pipefail

# Re-run as root in a non-interactive way if needed.
if [ "${EUID:-$(id -u)}" -ne 0 ]; then
	exec sudo -n "$0" "$@"
fi

echo "==== Starting deploy.sh ===="

cd /opt/ai-agent-boilerplate

echo "Activating virtualenv..."
source venv/bin/activate
 
cd code

echo "Cleaning old .env and Flask logs..."
rm -f .env
rm -f flask.log

echo "Installing dependencies..."
pip install -r requirements.txt

echo "Writing .env from injected config..."
if [ -z "${GROQ_API_KEY_B64:-}" ] || [ -z "${DATABASE_URL_B64:-}" ] || [ -z "${GROQ_MODEL_NAME_B64:-}" ]; then
    echo "❌ Missing GROQ_API_KEY_B64, DATABASE_URL_B64, or GROQ_MODEL_NAME_B64 in environment" >&2
    exit 1
fi

GROQ_API_KEY=$(printf '%s' "$GROQ_API_KEY_B64" | base64 -d)
DATABASE_URL=$(printf '%s' "$DATABASE_URL_B64" | base64 -d)
GROQ_MODEL_NAME=$(printf '%s' "$GROQ_MODEL_NAME_B64" | base64 -d)

cat > .env <<EOF
DEBUG=True
GROQ_API_KEY=${GROQ_API_KEY}
GROQ_MODEL_NAME=${GROQ_MODEL_NAME}

# PostgreSQL Database Configuration
DATABASE_URL=${DATABASE_URL}
EOF
echo "✅ Generated .env"

echo "Restarting service..."

echo "Stopping only ai-agent service..."
if [ -f zero.pid ]; then
    kill $(cat zero.pid) || true
    rm zero.pid
fi

echo "Starting Flask app..."
cd /opt/ai-agent-boilerplate/code
export FLASK_APP=app.py
nohup flask run --host=0.0.0.0 --port=5000 > flask.log 2>&1 &
echo $! > zero.pid

echo "✅ Deployment complete!"
