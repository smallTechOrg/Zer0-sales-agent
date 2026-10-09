import logging
from http import HTTPStatus
from urllib.parse import urlparse
from api.models import ValidationResponse
from config import max_input_length, agent_type , status_type
import uuid
from db_pool import with_connection

log = logging.getLogger(__name__)

def chat_api_validate(request) -> ValidationResponse:
    chat_input_validation_response = validate_chat_user_input(request)
    if not chat_input_validation_response.is_valid:
        return chat_input_validation_response
    
    request_type = request.get_json().get('request_type').lower().strip()
    if request_type not in agent_type:
        return ValidationResponse(False, "Need a valid request type")

    address_validation_response = validate_address(request)
    if not address_validation_response.is_valid:
        return address_validation_response 
    
    return ValidationResponse(True, "", {"request_type":request_type, "domain":address_validation_response.data})
    

def validate_contact_data(request):
    data = request.get_json()
    if not data:
        return ValidationResponse(False, "data is required")
    session_validation = validate_session_id(request)
    if not session_validation.is_valid:
        return session_validation
    name    = data.get("name")
    email   = data.get("email")
    mobile  = data.get("mobile")
    country = data.get("country")
    if not any([name, email, mobile, country]):
        return ValidationResponse(False, "At least one of name, email, mobile, or country is required")
    if email and ("@" not in email or "." not in email):
        return ValidationResponse(False, "Invalid email address")
    if mobile and not str(mobile).replace("+", "").replace(" ", "").isdigit():
        return ValidationResponse(False, "Invalid mobile number")
    return ValidationResponse(True, "")


def validate_update_data(request):
    data = request.get_json()
    status = data.get("status")
    is_active = data.get("is_active")
    if not data:
        return ValidationResponse(False,"data is required")
    if not validate_session_id(request).is_valid:
        return validate_session_id(request)
    if status and status not in status_type.__members__ and status not in [s.value for s in status_type]:
        return ValidationResponse(False,"Status not allowed")
    if is_active and not isinstance(is_active, bool):
        return ValidationResponse(False,"Invalid input")
    return ValidationResponse(True, "")


def validate_history_data(request):
    session_validation_response = validate_session_id(request)
    if not session_validation_response.is_valid:
        return session_validation_response
    
    address_validation_response  = validate_address(request)
    if not address_validation_response.is_valid:
        return address_validation_response 
    return ValidationResponse(True, None, {"domain": address_validation_response.data})


def validate_chat_user_input(request) -> ValidationResponse:
    input = request.get_json().get('input', '')
    input = input.strip()
    if not input:
        return ValidationResponse(False,"Please enter a message before sending.")
    if len(input) > max_input_length:
        return ValidationResponse(False, "Your message is too long. Please limit to {max_input_length} characters.")
    return ValidationResponse(True, "")
    

def validate_session_id(request):
    session_id = request.args.get("session_id")
    if not session_id:
        session_id = request.get_json().get("session_id")

    """Validate session_id and return chat history or create new session."""
    try:
        if session_id is None:
            return ValidationResponse(False, "session_id is required")
        if not is_valid_uuid(session_id):
            return ValidationResponse(False, "Invalid session id format")
        return ValidationResponse(True)

    except Exception:
        log.exception("session_id validation failed")
        return {"is_valid":False, "message":"Internal Server Error", "status":HTTPStatus.INTERNAL_SERVER_ERROR}

    
def validate_address(request):
    """Checks whether the given address exists. Returns (is_valid, domain or message)."""
    address = get_request_address(request)
    row = _find_domain_key(address)

    if not row:
        return ValidationResponse(False, "Incorrect Address")

    return ValidationResponse(True, "", row[0])


@with_connection
def _find_domain_key(conn, address):
    with conn.cursor() as cur:
        cur.execute("SELECT key FROM domains WHERE address = %s;", (address,))
        return cur.fetchone()


def get_request_address(request):
    origin = request.args.get("origin")
    if not origin or not str(origin).strip():
        origin = request.headers.get("Origin")
    if origin:
        return urlparse(origin).netloc
    return None


def is_valid_uuid(value: str) -> bool:
    """Check if a string is a valid UUID."""
    try:
        uuid.UUID(value)
        return True
    except ValueError:
        return False