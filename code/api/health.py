from flask import Blueprint, jsonify

from db_pool import get_pool, ping

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
def health():
    """
    Report the state of the app and the database. The probe borrows from the
    same pool as every other endpoint, so a green check means the API can
    reach the database.

    Schema state is not reported here. Creating and migrating tables is the
    deployment's job, not something a liveness probe should discover.
    """
    status = {
        "message": "Hello World",
        "database": "disconnected",
    }

    try:
        ping()
        status["database"] = "connected"
        code = 200
    except Exception as e:
        status["database_error"] = str(e)
        code = 503

    try:
        status["pool"] = get_pool().get_stats()
    except Exception:  # the pool could not even be built
        pass

    return jsonify(status), code
