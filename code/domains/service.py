"""
Business / Application Logic Layer for the Domains resource.

Rules enforced here:
  - URL parsing and hostname normalisation (strip scheme, path, query, leading "www.").
  - Auto-generation of a domain *key* from the address when none is supplied.
  - Duplicate-address guard before hitting the DB.
  - Parent-domain existence check before creating a child domain.

The service depends on ``DomainRepository`` via constructor injection so that
tests can provide a mock without touching the database.
"""
from __future__ import annotations

import re
from typing import Optional
from urllib.parse import urlparse

from domains.repository import DomainRepository


# ---------------------------------------------------------------------------
# Domain-specific exceptions
# ---------------------------------------------------------------------------

class DomainAlreadyExistsError(Exception):
    """Raised when a domain with the same extracted address already exists."""


class ParentDomainNotFoundError(Exception):
    """Raised when the supplied parent_id does not match any existing domain."""


class InvalidWebsiteURLError(Exception):
    """Raised when the website URL cannot be parsed into a valid hostname."""


# ---------------------------------------------------------------------------
# Service
# ---------------------------------------------------------------------------

class DomainService:
    """
    Orchestrates domain creation and retrieval.

    All methods are pure Python – they have no direct knowledge of HTTP or SQL.
    """

    def __init__(self, repository: DomainRepository) -> None:
        self._repo = repository

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def add_domain(
        self,
        website_url: str,
        key: Optional[str] = None,
        parent_id: Optional[int] = None,
    ) -> dict:
        """
        Validate, extract and persist a new domain derived from *website_url*.

        Parameters
        ----------
        website_url:
            Raw website address supplied by the caller (e.g. "https://www.acme.com/about").
        key:
            Optional override for the domain key.  When omitted the key is
            auto-generated from the extracted address.
        parent_id:
            Optional foreign-key reference to an existing parent domain.

        Returns
        -------
        dict
            The persisted domain record.

        Raises
        ------
        InvalidWebsiteURLError
            If a valid hostname cannot be extracted from *website_url*.
        DomainAlreadyExistsError
            If a domain with the extracted address already exists.
        ParentDomainNotFoundError
            If *parent_id* is provided but does not match any domain.
        """
        address = self.extract_address(website_url)

        existing = self._repo.find_by_address(address)
        if existing is not None:
            # The two rows are written in two transactions. An earlier call can
            # have written this one and stopped before the www variant, which
            # then no request can reach. Finish it here, so a retry repairs the
            # registration instead of failing on the address that exists.
            self._ensure_www(
                address, existing.get("key"), existing.get("parent_id")
            )
            raise DomainAlreadyExistsError(
                f"A domain for '{address}' already exists."
            )

        if parent_id is not None and self._repo.find_by_id(parent_id) is None:
            raise ParentDomainNotFoundError(
                f"Parent domain with id={parent_id} does not exist."
            )

        resolved_key = self._resolve_key(key, address)
        domain = self._repo.create(key=resolved_key, address=address, parent_id=parent_id)

        # Register the www variant too, under the same key, so a lookup gives
        # the same key with or without the prefix.
        self._ensure_www(address, resolved_key, parent_id)

        return domain

    def get_domain(self, domain_id: int) -> Optional[dict]:
        """Return the domain with *domain_id*, or ``None`` if not found."""
        return self._repo.find_by_id(domain_id)

    def list_domains(self) -> list[dict]:
        """Return all domains ordered by creation time (oldest first)."""
        return self._repo.list_all()

    # ------------------------------------------------------------------
    # Static helpers – kept on the class so tests can call them directly.
    # ------------------------------------------------------------------

    def _ensure_www(self, address: str, key: str, parent_id: Optional[int]) -> None:
        """Add the www variant of *address* if it is not there yet."""
        www_address = f"www.{address}"
        if self._repo.find_by_address(www_address) is None:
            self._repo.create(key=key, address=www_address, parent_id=parent_id)

    @staticmethod
    def extract_address(website_url: str) -> str:
        """
        Extract and normalise the registrable hostname from any URL-like string.

        Normalisation steps
        -------------------
        1. Prepend ``https://`` when no scheme is present so ``urlparse`` works.
        2. Read ``parsed.hostname`` (already lowercased, port stripped).
        3. Strip a single leading ``www.`` prefix (other sub-domains are kept).

        Examples
        --------
        ``"https://www.example.com/path?q=1"``  →  ``"example.com"``
        ``"http://api.example.com"``             →  ``"api.example.com"``
        ``"example.com"``                         →  ``"example.com"``
        ``"www.example.co.uk/page"``              →  ``"example.co.uk"``

        Raises
        ------
        InvalidWebsiteURLError
            If no valid hostname can be extracted.
        """
        url = website_url.strip()
        if not url.lower().startswith(("http://", "https://")):
            url = f"https://{url}"

        parsed = urlparse(url)
        hostname: Optional[str] = parsed.hostname  # lowercased, port stripped

        if not hostname:
            raise InvalidWebsiteURLError(
                f"Cannot extract a hostname from: {website_url!r}"
            )

        if hostname.startswith("www."):
            hostname = hostname[4:]

        if "." not in hostname:
            raise InvalidWebsiteURLError(
                f"Extracted hostname '{hostname}' is not a valid domain (no TLD)."
            )

        return hostname

    @staticmethod
    def generate_key(address: str) -> str:
        """
        Derive a domain key from its address.

        Non-alphanumeric characters are replaced by underscores, consecutive
        underscores are collapsed, and the result is uppercased.

        Examples
        --------
        ``"example.com"``     →  ``"EXAMPLE_COM"``
        ``"api.acme.co.uk"``  →  ``"API_ACME_CO_UK"``
        """
        key = re.sub(r"[^A-Za-z0-9]", "_", address).upper()
        key = re.sub(r"_+", "_", key).strip("_")
        return key

    # ------------------------------------------------------------------
    # Private helpers
    # ------------------------------------------------------------------

    def _resolve_key(self, raw_key: Optional[str], address: str) -> str:
        """Return the caller-supplied key (normalised) or auto-generate one."""
        if raw_key and raw_key.strip():
            return raw_key.strip().upper()
        return self.generate_key(address)
