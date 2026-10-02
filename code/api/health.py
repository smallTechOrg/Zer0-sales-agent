from flask import Blueprint, jsonify

from db import missing_tables
from db_pool import pool_status

health_bp = Blueprint("health", __name__)


@health_bp.route("/health", methods=["GET"])
def health():
    """
    Report the state of the app and the database. The probe uses the same pool
    as the other endpoints, and asks for the tables the API needs. SELECT 1
    alone passes on a database with no schema, while every data endpoint fails.
    """
    status = {
        "message": "Hello World",
        "database": "disconnected",
    }

    try:
        absent = missing_tables()
        status["database"] = "connected"
        status["pool"] = pool_status()
        if absent:
            status["schema_error"] = f"missing tables: {', '.join(absent)}"
            return jsonify(status), 503
    except Exception as e:
        status["database_error"] = str(e)
        try:
            status["pool"] = pool_status()
        except Exception:
            pass
        return jsonify(status), 503

    return jsonify(status), 200
