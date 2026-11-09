"""Sanity checks for contact details extracted from chat messages.

The LLM extracts whatever a visitor types. Visitors who are asked for their
details often type placeholders ("test@test.com", "1234567890", "asdf"), so
those must not land in the leads table as if they were real. Each cleaner
returns the normalised value, or None when it is missing, malformed or an
obvious placeholder. The cleaners are deliberately conservative: a value is
only rejected when it cannot be a real contact, so an unusual but possible
value is kept.
"""

from __future__ import annotations

import re

_EMAIL_RE = re.compile(r"^[A-Za-z0-9._%+'-]+@([A-Za-z0-9-]+\.)+[A-Za-z]{2,}$")

# Local parts and domains people type when they do not want to give an email.
_PLACEHOLDER_WORDS = frozenset({
    "test", "testing", "tester", "abc", "abcd", "xyz", "asdf", "asdfg", "qwerty", "qwertyuiop",
    "aaa", "aaaa", "fake", "dummy", "sample", "none", "no", "nope", "null", "na", "n/a",
    "example", "email", "mail", "user", "username", "guest", "anonymous", "anon", "nobody",
    "someone", "foo", "bar", "baz", "blah", "temp", "demo", "unknown", "notgiven", "noemail",
})
_PLACEHOLDER_DOMAINS = frozenset({
    "example.com", "example.org", "example.net", "test.com", "test.in", "abc.com", "xyz.com",
    "email.com", "mail.com", "domain.com", "company.com", "website.com", "site.com", "fake.com",
    "asdf.com", "qwerty.com", "aaa.com", "a.com", "b.com", "x.com", "mailinator.com",
    "guerrillamail.com", "10minutemail.com", "yopmail.com", "tempmail.com", "temp-mail.org",
    "sharklasers.com", "trashmail.com", "dispostable.com", "getnada.com", "throwawaymail.com",
})

# Names people type instead of a name.
_PLACEHOLDER_NAMES = frozenset(_PLACEHOLDER_WORDS | {
    "me", "you", "name", "my name", "your name", "customer", "visitor", "person", "human", "bot", "admin",
})

_INDIA_HINTS = ("india", "bharat", "in")


def _is_mash(value: str) -> bool:
    """True for single-character repeats ("aaaa") and keyboard runs ("asdfgh")."""
    v = value.lower()
    if len(set(v)) == 1:
        return True
    keyboard = "qwertyuiopasdfghjklzxcvbnm"
    return len(v) >= 4 and (v in keyboard or v in keyboard[::-1])


def clean_name(value: str | None) -> str | None:
    """Return the name, or None for an empty value or an obvious placeholder."""
    name = (value or "").strip(" \t\r\n.,;:!?\"'")
    if not name:
        return None
    lowered = name.lower()
    if lowered in _PLACEHOLDER_NAMES or _is_mash(lowered.replace(" ", "")):
        return None
    letters = [c for c in name if c.isalpha()]
    if len(letters) < 2:  # "a", "1", "-", "x."
        return None
    return name


def clean_email(value: str | None) -> str | None:
    """Return the email with a lower-cased domain, or None if malformed or a placeholder."""
    email = (value or "").strip().strip(".,;:<>()[]\"'")
    if not email or not _EMAIL_RE.match(email):
        return None
    local, _, domain = email.rpartition("@")
    domain = domain.lower()
    if domain in _PLACEHOLDER_DOMAINS:
        return None
    local_key = re.sub(r"[^a-z0-9]", "", local.lower())
    if local_key in _PLACEHOLDER_WORDS or _is_mash(local_key):
        return None
    if _is_mash(domain.split(".")[0]):
        return None
    if local_key == domain.split(".")[0] and local_key in _PLACEHOLDER_WORDS:
        return None
    return f"{local}@{domain}"


def _is_sequence(digits: str) -> bool:
    """True when every digit is one more (or one less) than the previous: 1234567890, 9876543210."""
    if len(digits) < 6:
        return False
    steps = {(int(b) - int(a)) % 10 for a, b in zip(digits, digits[1:])}
    return steps in ({1}, {9})


def _looks_indian(country: str | None, digits: str, has_plus: bool) -> bool:
    c = (country or "").strip().lower()
    if c in _INDIA_HINTS:
        return True
    # "+91 ..." is India whatever follows; a bare 12-digit 91... is the same number without the plus.
    return digits.startswith("91") and (has_plus or len(digits) == 12)


def clean_mobile(value: str | None, country: str | None = None) -> str | None:
    """Return the number as digits (with a leading + if given), or None if it cannot be a phone number.

    Rejects numbers with fewer than 7 or more than 15 digits, numbers made of a
    single repeated digit, straight ascending or descending runs, and, when the
    country is India (or the number carries +91), national numbers that do not
    have 10 digits starting with 6 to 9.
    """
    raw = (value or "").strip()
    if not raw:
        return None
    has_plus = raw.lstrip().startswith("+")
    digits = re.sub(r"\D", "", raw)
    if digits.startswith("00") and not has_plus:
        digits, has_plus = digits[2:], True
    if not 7 <= len(digits) <= 15:
        return None
    if len(set(digits)) == 1 or _is_sequence(digits):
        return None
    if len(set(digits)) <= 2 and len(digits) >= 10:  # 1212121212, 1010101010
        return None
    if _looks_indian(country, digits, has_plus):
        national = digits[2:] if digits.startswith("91") and (has_plus or len(digits) == 12) else digits
        if not (len(national) == 10 and national[0] in "6789"):
            return None
        if _is_sequence(national) or len(set(national)) == 1:
            return None
    return ("+" if has_plus else "") + digits


def clean_country(value: str | None) -> str | None:
    country = (value or "").strip(" \t\r\n.,;:!?\"'")
    if not country or country.lower() in _PLACEHOLDER_WORDS or _is_mash(country.lower()):
        return None
    if not any(c.isalpha() for c in country):
        return None
    return country


def sanitise_contact_info(info: dict | None) -> dict:
    """Return a copy of the extracted contact info with invalid or placeholder values blanked.

    Keys are the ones the extraction prompt returns: contact_name, email, mobile,
    country. Missing keys come back as empty strings so callers can treat the
    result uniformly.
    """
    info = dict(info or {})
    country = clean_country(info.get("country"))
    cleaned = {
        "contact_name": clean_name(info.get("contact_name")) or "",
        "email": clean_email(info.get("email")) or "",
        "mobile": clean_mobile(info.get("mobile"), country) or "",
        "country": country or "",
    }
    for key, value in info.items():
        cleaned.setdefault(key, value)
    return cleaned
