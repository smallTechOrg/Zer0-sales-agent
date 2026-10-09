
import logging
from http import HTTPStatus
from flask import Blueprint, jsonify, request
from werkzeug.exceptions import HTTPException
from api.models import APIResponse
from history import get_history
from leads import get_all_chat_info
from leads_update import update_chat_info, update_contact_info
from llm_api import get_groq_response
from api.validators import validate_contact_data, validate_history_data, validate_update_data, chat_api_validate

log = logging.getLogger(__name__)

chat_bp = Blueprint("chat", __name__)

#Rendering response
# chat_api is a Flask route function defined that acts as the backend API endpoint for chat exchanges. It is the API endpoint your frontend calls to send user messages and receive chatbot responses.
# It receives a JSON request containing the user's chat input from the frontend, validates the input, sends the validated input to the LLM, and returns a JSON response.
@chat_bp.route('/chat', methods=['POST'])
def chat_api():
    # Validation uses the database. Keep it in the try block.
    try:
        chat_validation_response = chat_api_validate(request)
        if not chat_validation_response.is_valid:
            return APIResponse(chat_validation_response).response(HTTPStatus.BAD_REQUEST)

        # Get response from LLM
        data = request.get_json()
        input = data.get('input', '')
        session_id = data.get('session_id')
        request_type = chat_validation_response.data["request_type"]
        domain = chat_validation_response.data["domain"]

        bot_response = get_groq_response(input.strip(), session_id, request_type, domain)
        return APIResponse(None,{'response': bot_response}).response(HTTPStatus.OK)
    except HTTPException:
        # A bad request already has the correct status code. Do not change it.
        raise
    except Exception:
        log.exception("Error during LLM call")
        return APIResponse().response(HTTPStatus.INTERNAL_SERVER_ERROR)


@chat_bp.route('/chat-info', methods=['PATCH'])
def patch_updates():
    try:
        chat_info_validation_response = validate_update_data(request)
        if not chat_info_validation_response.is_valid:
            return APIResponse(chat_info_validation_response).response(HTTPStatus.BAD_REQUEST)
        data = request.get_json()
        session_id = data.get("session_id")
        status = data.get("status")
        remarks = data.get("remarks")
        is_active = data.get("is_active")
        update_chat_info(session_id, status, remarks, is_active)
        return APIResponse(None,{'message': "chat-info updated"}).response(HTTPStatus.OK)
    except HTTPException:
        raise
    except Exception:
        log.exception("Error in chat-info PATCH endpoint")
        return APIResponse().response(HTTPStatus.INTERNAL_SERVER_ERROR)


@chat_bp.route('/chat-info/contact', methods=['PATCH'])
def patch_contact_info():
    try:
        contact_validation_response = validate_contact_data(request)
        if not contact_validation_response.is_valid:
            return APIResponse(contact_validation_response).response(HTTPStatus.BAD_REQUEST)
        data = request.get_json()
        session_id = data.get("session_id")
        name    = data.get("name")
        email   = data.get("email")
        mobile  = data.get("mobile")
        country = data.get("country")
        update_contact_info(session_id, name, email, mobile, country)
        return APIResponse(None, {'message': "contact info updated"}).response(HTTPStatus.OK)
    except HTTPException:
        raise
    except Exception:
        log.exception("Error in chat-info/contact PATCH endpoint")
        return APIResponse().response(HTTPStatus.INTERNAL_SERVER_ERROR)


@chat_bp.route('/chat-info', methods=['GET'])
def get_chat_info():
    try:
        leads_data, status = get_all_chat_info()
        return APIResponse(None,{'leads': leads_data}).response(HTTPStatus.OK)
    except Exception:
        log.exception("Error in chat-info GET endpoint")
        return APIResponse().response(HTTPStatus.INTERNAL_SERVER_ERROR)


# History API to load previous messages while loading the page
@chat_bp.route("/history", methods=["GET"])
def history_endpoint():
    # validate_history_data uses the database. Keep it in the try block.
    try:
        history_validation_response = validate_history_data(request)
        if not history_validation_response.is_valid:
            return APIResponse(history_validation_response).response(HTTPStatus.BAD_REQUEST)
        session_id = request.args.get("session_id")
        history_data, status = get_history(session_id, history_validation_response.data["domain"])
        return jsonify(history_data), status
    except HTTPException:
        raise
    except Exception:
        log.exception("Error in history endpoint")
        return APIResponse().response(HTTPStatus.INTERNAL_SERVER_ERROR)
