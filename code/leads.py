from typing import List, Dict, Any, Tuple
from psycopg.rows import dict_row
from db_pool import with_connection
from http import HTTPStatus


@with_connection
def _select_chat_info(conn) -> List[Dict[str, Any]]:
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute("""
            SELECT
                session_id,
                COALESCE(contact_name, '') as name,
                COALESCE(email, '') as email,
                COALESCE(mobile, '') as mobile_number,
                COALESCE(country, '') as country,
                COALESCE(status, 'OPEN') as status,
                COALESCE(remarks, '') as remarks,
                COALESCE(domain) as domain,
                COALESCE(created_at) as time
            FROM chat_info
            WHERE is_active is TRUE
            ORDER BY created_at DESC;
        """)
        return cur.fetchall()


def get_all_chat_info() -> Tuple[List[Dict[str, Any]], HTTPStatus]:
    """
    Retrieve all stored chat info records.
    """
    try:
        return _select_chat_info(), HTTPStatus.OK

    except Exception as e:
        print("Error fetching chat-info:", e)
        raise
