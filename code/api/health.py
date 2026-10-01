from flask import Blueprint, jsonify

from db import ping, pool_status, schema_ready

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
def health():
    """
    Report the state of the app and the database. The probe uses the same pool
    as the other endpoints. It also checks the schema, because SELECT 1 passes
    without one.
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
    except Exception as e:
        status["database_error"] = str(e)
        try:
            status["pool"] = pool_status()
        except Exception:
            pass
        return jsonify(status), 503

    if not schema_ready():
        status["schema_error"] = (
            "The database is reachable but the schema has not been created. "
            "Data endpoints will fail until it is."
        )
        return jsonify(status), 503

    return jsonify(status), 200
