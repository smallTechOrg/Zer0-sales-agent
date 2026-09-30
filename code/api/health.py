from flask import Blueprint, jsonify

from db import ping, pool_status, schema_ready

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
def health():
    """
    Health check endpoint that verifies application and database connectivity.

    The probe runs through the same connection pool the chat and prompt
    endpoints use. Opening a separate connection here would answer a different
    question: it could report "connected" while the pool the API actually
    serves from is broken, or the other way round.
    """
    status = {
        "message": "Hello World",
        "database": "disconnected",
        "schema": "ready" if schema_ready() else "pending",
    }

    try:
        ping()
        status["database"] = "connected"
        status["pool"] = pool_status()
        return jsonify(status), 200
    except Exception as e:
        status["database_error"] = str(e)
        try:
            status["pool"] = pool_status()
        except Exception:
            pass
        return jsonify(status), 503
