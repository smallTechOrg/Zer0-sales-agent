from flask import Flask, render_template
from flask_cors import CORS
from flask_smorest import Api

from api import register_blueprints
from api.domains import domains_bp
from config import DEBUG, PORT, WERKZEUG_RUN_MAIN
from db import init_db
from scheduler import start_scheduler, stop_scheduler

# ---------------------------------------------------------------------------
# Application factory
# ---------------------------------------------------------------------------

def create_app() -> Flask:
    flask_app = Flask(__name__)

    # -- Database ------------------------------------------------------------
    # init_db never raises: if PostgreSQL is down the app still starts, /health
    # reports it, and a background thread creates the schema once the database
    # comes back. Previously an unreachable database at import time killed the
    # process, so even the health check was unreachable.
    init_db()

    # -- flask-smorest / OpenAPI configuration ---------------------------------
    # Auto-generated spec is served at /api/openapi.json.
    # Interactive Swagger UI is served at /api/docs.
    # The legacy static swagger.yaml UI continues to run at / unchanged.
    flask_app.config.update(
        API_TITLE="Chat API",
        API_VERSION="v1",
        OPENAPI_VERSION="3.0.3",
        OPENAPI_URL_PREFIX="/api",
        OPENAPI_SWAGGER_UI_PATH="/docs",
        OPENAPI_SWAGGER_UI_URL="https://cdn.jsdelivr.net/npm/swagger-ui-dist/",
    )

    # -- Existing blueprints (health, chat, prompts, legacy swagger UI) --------
    register_blueprints(flask_app)

    # -- Smorest-managed blueprints (auto-documented) --------------------------
    smorest_api = Api(flask_app)
    smorest_api.register_blueprint(domains_bp)

    CORS(flask_app)

    # -- Background jobs -------------------------------------------------------
    # The daily summary runs in a thread of this process. One scheduler per
    # process: see the reloader note in the __main__ block.
    start_scheduler()

    return flask_app


app = create_app()

@app.route('/chat-ui')
def chat_ui():
    return render_template('chat.html')

if __name__ == "__main__":
    # With debug on, Werkzeug's reloader runs this file twice: a parent that
    # watches files and a child that serves. create_app() above already
    # started a scheduler in this process. In the parent, stop it, or the job
    # runs in both processes and Slack gets every summary twice. The child has
    # WERKZEUG_RUN_MAIN set and keeps its scheduler. `flask run` (deployment)
    # has no reloader and is not affected.
    if DEBUG and not WERKZEUG_RUN_MAIN:
        stop_scheduler()
    app.run(debug=DEBUG, port=PORT)
