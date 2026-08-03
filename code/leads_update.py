from db import sync_connection

def update_contact_info(session_id: str, name: str = None, email: str = None, mobile: str = None, country: str = None):
    """
    Update contact details (name, email, mobile, country) for a session in chat_info.
    """
    try:
        sync_connection.rollback()

        with sync_connection.cursor() as cur:
            update_query = """
                UPDATE chat_info
                SET
                    contact_name = COALESCE(%s, contact_name),
                    email   = COALESCE(%s, email),
                    mobile  = COALESCE(%s, mobile),
                    country = COALESCE(%s, country)
                WHERE session_id = %s
                RETURNING *;
            """

            cur.execute(update_query, (name, email, mobile, country, session_id))
            updated_row = cur.fetchone()
            sync_connection.commit()

            updates = []
            if name:    updates.append(f"name='{name}'")
            if email:   updates.append(f"email='{email}'")
            if mobile:  updates.append(f"mobile='{mobile}'")
            if country: updates.append(f"country='{country}'")

            print(f"[DATABASE] Contact updated for session {session_id}: {', '.join(updates) if updates else 'no new info'}")
            return bool(updated_row)

    except Exception as e:
        sync_connection.rollback()
        print(f"[DATABASE ERROR] Failed to update contact for {session_id}: {str(e)}")
        raise


def update_chat_info(session_id: str, status: str = None, remarks: str = None, is_active: bool = None):
    """
    Insert or update a row in chat_info and return the updated row.
    """
    try:
        sync_connection.rollback()
        
        with sync_connection.cursor() as cur:
            update_query = """
                UPDATE chat_info 
                SET
                    status = COALESCE(%s, status),
                    remarks = COALESCE(%s, remarks),
                    is_active = COALESCE(%s, is_active)
                WHERE session_id = %s
                RETURNING *;
            """
            
            cur.execute(update_query, (
                status,
                remarks,
                is_active,
                session_id
            ))

            updated_row = cur.fetchone() 
            sync_connection.commit()            
            
            # Log what was updated
            updates = []
            if status: updates.append(f"status='{status}'")
            if remarks: updates.append(f"remarks='{remarks}'")
            if is_active is not None: updates.append(f"is_active={1 if is_active else 0}")
            
            print(f"[DATABASE] Info updated for session {session_id}: {', '.join(updates) if updates else 'no new info'}")
            return bool(updated_row)

    
    except Exception as e:
        sync_connection.rollback()
        print(f"[DATABASE ERROR] Failed to update lead for {session_id}: {str(e)}")
        raise