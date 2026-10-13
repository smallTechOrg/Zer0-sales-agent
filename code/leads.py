from typing import List, Dict, Any, Tuple
from psycopg import sql
from psycopg.rows import dict_row
import config
from db_pool import with_connection
from http import HTTPStatus


@with_connection
def _select_chat_info(conn) -> List[Dict[str, Any]]:
    # Only chats where the visitor wrote at least one message. Opening the
    # widget saves the intro message, and the periodic summary then makes a
    # chat_info row for that session; those rows are not leads.
    query = sql.SQL("""
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
              AND EXISTS (
                  SELECT 1 FROM {table} h
                  WHERE h.session_id::text = chat_info.session_id
                    AND h.message->>'type' = 'human'
              )
            ORDER BY created_at DESC;
        """).format(table=sql.Identifier(config.table_name))
    with conn.cursor(row_factory=dict_row) as cur:
        cur.execute(query)
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
