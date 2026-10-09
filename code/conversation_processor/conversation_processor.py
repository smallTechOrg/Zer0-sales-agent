import json
from datetime import datetime
from db_pool import with_connection
from langchain_groq import ChatGroq
from config import GROQ_API_KEY, GROQ_MODEL_NAME, agent_type
from langchain_core.messages import SystemMessage, HumanMessage, AIMessage
from system_prompt import get_prompt

def process_conversation(user_input, session_id, request_type, domain):
    """
    Main hook function that processes each conversation exchange.
    Uses LLM to detect if user provided their contact info in the current input.
    """

    try:
        print(f"[PROCESSOR] Processing session {session_id} for contact info detection...")  
        # Update request_type for the new messages in this session
        _update_session_request_type(session_id, request_type, domain)
        # Choose llm function based on request type
        info_data = _detect_info_with_llm(user_input, request_type, domain)
        
        if info_data and _has_valid_info(info_data, request_type):
            print(f"[PROCESSOR] info detected in session {session_id}")
                
            # Save information to database
            _save_info_to_database(session_id, info_data, user_input, request_type, domain)
            print(f"[PROCESSOR] information saved for session {session_id}.")
        else:
            print(f"[PROCESSOR] No Info detected in current message for session {session_id}")

        
    except Exception as e:
        print(f"[PROCESSOR] Error in conversation processor: {e}")
        # Don't let processing errors break the chat flow

@with_connection
def _update_session_request_type(conn, session_id, request_type, domain):
    """
    Insert a new chat_info row with (session_id, request_type)
    only if session_id does not already exist.
    If the session_id exists, do nothing.
    """
    try:
        with conn.cursor() as cur:
            insert_query = """
            INSERT INTO chat_info (
                session_id,
                request_type,
                domain
            ) VALUES (%s, %s, %s)
            ON CONFLICT (session_id) DO NOTHING
            """

            cur.execute(insert_query, (session_id, request_type, domain))

            if cur.rowcount and cur.rowcount > 0:
                print(f"[CREATE] Inserted new chat_info for session_id={session_id} with request_type='{request_type}'and domain ='{domain}'")
            else:
                print(f"[CREATE] session_id={session_id} already exists - no action taken")

    except Exception as e:
        print(f"Error inserting request_type row: {e}")
        raise


def _has_valid_info(info_data, request_type):
    """
    Check if the extracted info contains the at least one required fields.
    
    Args:
        info_data (dict): Extracted information
        
    Returns:
        bool: True if required info is present
    """
    if not info_data:
        return False
        
    if request_type == agent_type.SALES:
        # For sales, we need at least one of the key fields to be detected
        return any(info_data.get(f, "").strip() for f in ["contact_name", "email", "mobile", "country"])
    else:
        # For non-sales, just check contact)name
        return info_data.get('name_detected', False) and info_data.get('contact_name', '').strip()

def _detect_info_with_llm(message, request_type, domain):
    """
    Use LLM to detect if the user provided their contact info in the message.
    
    Args:
        message (str): User's message to analyze
        
    Returns:
        dict: Contains Contact Us/name info 
    """
    try:
        # Create LLM instance for contact info detection
        llm = ChatGroq(groq_api_key=GROQ_API_KEY, model=GROQ_MODEL_NAME)

        # prompt based on request type
        if request_type == agent_type.SALES:
            prompt_content = get_prompt(domain, request_type, "fetch-contact-info")
            prompt_content = prompt_content.replace("{message}", message)
        else:
            prompt_content = get_prompt(domain, request_type,"fetch-name")
            prompt_content = prompt_content.replace("{message}", message)
        print(f"[INFO_DETECTION] Using prompt: {prompt_content}")   
        # Create the prompt
        full_prompt = [SystemMessage(content=prompt_content)]

        # Get LLM response
        response = llm.invoke(full_prompt)
        response_text = response.content.strip()
        # Clean markdown fences if present
        response_text = response_text.strip("`").replace("json\n", "")        
        print(f"[INFO_DETECTION] LLM Response: {response_text}")
        
        # Parse JSON response
        try:
            contact_info = json.loads(response_text)
            return contact_info
        except json.JSONDecodeError:
            print(f"[INFO_DETECTION] Failed to parse LLM response as JSON: {response_text}")
            return {"contact_name": "", "email": "", "mobile": "", "country": ""}
            
    except Exception as e:
        print(f"[INFO_DETECTION] Error in LLM contact info detection: {e}")
        return {"contact_name": "", "email": "", "mobile": "", "country": ""}
    
@with_connection
def _save_info_to_database(conn, session_id, info_data, original_message, request_type, domain):
    """
    Save detected info to chat_info table.

    Args:
        session_id (str): Session identifier
        info_data: Extracted info
        original_message (str): The original message where contact info was detected
        request_type: type of request
    """
    with conn.cursor() as cur:

        metadata = {
            "info_detected_from_message": original_message,
            "detection_method": request_type,
            "detection_timestamp": datetime.now().isoformat()
        }

        # For sales requests, extract name, email, and country
        contact_name = info_data.get('contact_name', '').strip() or None
        email = info_data.get('email', '').strip() or None
        country = info_data.get('country', '').strip() or None
        mobile = info_data.get('mobile', '').strip() or None

        # Always update with new information (allow corrections)
        # Only keep existing data if new data is explicitly empty/None
        insert_query = """
        INSERT INTO chat_info (
            session_id, 
            contact_name, 
            email,
            country,
            mobile,
            request_type,
            domain,
            metadata,
            created_at
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s,%s)
        ON CONFLICT (session_id) 
        DO UPDATE SET 
            contact_name = CASE 
                WHEN EXCLUDED.contact_name IS NOT NULL THEN EXCLUDED.contact_name 
                ELSE chat_info.contact_name 
            END,
            email = CASE 
                WHEN EXCLUDED.email IS NOT NULL THEN EXCLUDED.email 
                ELSE chat_info.email 
            END,
            country = CASE 
                WHEN EXCLUDED.country IS NOT NULL THEN EXCLUDED.country 
                ELSE chat_info.country 
            END,
            mobile = CASE 
                WHEN EXCLUDED.mobile IS NOT NULL THEN EXCLUDED.mobile 
                ELSE chat_info.mobile 
            END,
            request_type = EXCLUDED.request_type,
            domain = EXCLUDED.domain,
            metadata = EXCLUDED.metadata,
            created_at = CASE 
                WHEN chat_info.created_at IS NULL THEN EXCLUDED.created_at 
                ELSE chat_info.created_at 
            END
        """

        cur.execute(insert_query, (
            session_id,
            contact_name,
            email,
            country,
            mobile,
            request_type,
            domain,
            json.dumps(metadata),
            datetime.now()
        ))

        # Log what was updated
        updates = []
        if contact_name: updates.append(f"contact_name='{contact_name}'")
        if email: updates.append(f"email='{email}'")
        if country: updates.append(f"country='{country}'")
        if mobile: updates.append(f"mobile='{mobile}'")

        print(f"[DATABASE] Info updated for session {session_id}: {', '.join(updates) if updates else 'no new info'}")
