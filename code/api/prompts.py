from http import HTTPStatus
import traceback
from flask import Blueprint, request, jsonify
from api.models import APIResponse
from prompts_table import get_all_prompts, upsert_prompt


prompt_bp = Blueprint("prompts", __name__)

# --- New Prompt APIs ---
@prompt_bp.route('/prompts', methods=['GET'])
def get_prompts():
    try:
        return jsonify({"prompts": get_all_prompts()}), 200
    except Exception:
        print(traceback.format_exc())
        return APIResponse().response(HTTPStatus.INTERNAL_SERVER_ERROR)

@prompt_bp.route('/prompt', methods=['POST'])
def create_or_update_prompt():
    try:
        data = request.get_json()
        domain = data.get('domain')
        agent_type = data.get('agent_type')
        prompt_type = data.get('type')
        text = data.get('text')
        if not all([domain, agent_type, prompt_type, text]):
            return jsonify({"success": False, "error": "Missing required fields: domain, agent_type, type, text"}), 400
        success = upsert_prompt(domain, agent_type, prompt_type, text)
        if success:
            return jsonify({"success": True, "message": "Prompt created/updated."}), 200
        else:
            return jsonify({"success": False, "error": "Failed to create/update prompt."}), 500
    except Exception as e:
        print(traceback.format_exc())
        return APIResponse().response(HTTPStatus.INTERNAL_SERVER_ERROR)
