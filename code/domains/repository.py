"""
Data access for the Domains resource. All SQL is here. Each method takes a
connection from the pool for one query. The repository keeps no connection.
"""
from __future__ import annotations

from typing import Optional

from db_pool import run_with_retry


class DomainRepository:
    """CRUD operations for the ``domains`` table."""

    # Column order returned by every SELECT / RETURNING clause.
    _COLUMNS = ("id", "key", "address", "parent_id", "created_at")

    # ------------------------------------------------------------------
    # Queries
    # ------------------------------------------------------------------

    def find_by_address(self, address: str) -> Optional[dict]:
        """Return the domain record matching *address*, or ``None``."""

        def query(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, key, address, parent, created_at "
                    "FROM domains WHERE address = %s;",
                    (address,),
                )
                return cur.fetchone()

        row = run_with_retry(query, label="DomainRepository.find_by_address")
        return self._to_dict(row) if row else None

    def find_by_id(self, domain_id: int) -> Optional[dict]:
        """Return the domain record with the given primary key, or ``None``."""

        def query(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, key, address, parent, created_at "
                    "FROM domains WHERE id = %s;",
                    (domain_id,),
                )
                return cur.fetchone()

        row = run_with_retry(query, label="DomainRepository.find_by_id")
        return self._to_dict(row) if row else None

    def list_all(self) -> list[dict]:
        """Return all domain records ordered by creation time."""

        def query(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "SELECT id, key, address, parent, created_at "
                    "FROM domains ORDER BY created_at ASC;"
                )
                return cur.fetchall()

        return [self._to_dict(row) for row in run_with_retry(query, label="DomainRepository.list_all")]

    # ------------------------------------------------------------------
    # Mutations
    # ------------------------------------------------------------------

    def create(
        self,
        key: str,
        address: str,
        parent_id: Optional[int] = None,
    ) -> dict:
        """
        Add a domain row and return it. Raise UniqueViolation if the address
        exists. A unique violation is not retried.
        """

        def insert(conn):
            with conn.cursor() as cur:
                cur.execute(
                    "INSERT INTO domains (key, address, parent) "
                    "VALUES (%s, %s, %s) "
                    "RETURNING id, key, address, parent, created_at;",
                    (key, address, parent_id),
                )
                return cur.fetchone()

        return self._to_dict(run_with_retry(insert, label="DomainRepository.create"))

    # ------------------------------------------------------------------
    # Helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _to_dict(row: tuple) -> dict:
        """Map a DB row tuple to a plain dict using canonical field names."""
        id_, key, address, parent, created_at = row
        return {
            "id": id_,
            "key": key,
            "address": address,
            "parent_id": parent,    # alias: DB column is "parent"
            "created_at": created_at,
        }
