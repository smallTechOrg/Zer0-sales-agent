from db_pool import with_connection


@with_connection
def update_contact_info(conn, session_id: str, name: str = None, email: str = None, mobile: str = None, country: str = None):
    """
    Update contact details (name, email, mobile, country) for a session in chat_info.

    Runs on a pooled connection: it is committed on success and rolled back on
    failure by the caller, so no manual rollback is needed to clear a poisoned
    transaction from an earlier request.
    """
    try:
        with conn.cursor() as cur:
            # The column is contact_name, not name. It read "name" before, so
            # this statement always failed with UndefinedColumn and the endpoint
            # could never succeed.
            update_query = """
                UPDATE chat_info
                SET
                    contact_name = COALESCE(%s, contact_name),
                    email        = COALESCE(%s, email),
                    mobile       = COALESCE(%s, mobile),
                    country      = COALESCE(%s, country)
                WHERE session_id = %s
                RETURNING *;
            """

            cur.execute(update_query, (name, email, mobile, country, session_id))
            updated_row = cur.fetchone()

            updates = []
            if name:    updates.append(f"name='{name}'")
            if email:   updates.append(f"email='{email}'")
            if mobile:  updates.append(f"mobile='{mobile}'")
            if country: updates.append(f"country='{country}'")

            print(f"[DATABASE] Contact updated for session {session_id}: {', '.join(updates) if updates else 'no new info'}")
            return bool(updated_row)

    except Exception as e:
        print(f"[DATABASE ERROR] Failed to update contact for {session_id}: {str(e)}")
        raise


@with_connection
def update_chat_info(conn, session_id: str, status: str = None, remarks: str = None, is_active: bool = None):
    """
    Insert or update a row in chat_info and return the updated row.
    """
    try:
        with conn.cursor() as cur:
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

            # Log what was updated
            updates = []
            if status: updates.append(f"status='{status}'")
            if remarks: updates.append(f"remarks='{remarks}'")
            if is_active is not None: updates.append(f"is_active={1 if is_active else 0}")

            print(f"[DATABASE] Info updated for session {session_id}: {', '.join(updates) if updates else 'no new info'}")
            return bool(updated_row)

    except Exception as e:
        print(f"[DATABASE ERROR] Failed to update lead for {session_id}: {str(e)}")
        raise
