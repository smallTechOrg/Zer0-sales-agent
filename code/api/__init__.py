from .health import health_bp
from .swagger import swaggerui_blueprint
from .chat import chat_bp
from .prompts import prompt_bp
from .jobs import jobs_bp

def register_blueprints(app):
    app.register_blueprint(health_bp)
    app.register_blueprint(swaggerui_blueprint)
    app.register_blueprint(chat_bp)
    app.register_blueprint(prompt_bp)
    app.register_blueprint(jobs_bp)

