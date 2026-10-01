from flask import Blueprint, jsonify

from db_pool import ping, pool_status

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
def health():
    """
    Report the state of the app and the database. The probe uses the same pool
    as the other endpoints.
    """
    status = {
        "message": "Hello World",
        "database": "disconnected",
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

    return jsonify(status), 200
