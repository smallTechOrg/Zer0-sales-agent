"""Placeholder and malformed contact details must not be saved as leads."""

from unittest.mock import patch

import pytest

from contact_validation import (
    clean_country,
    clean_email,
    clean_mobile,
    clean_name,
    sanitise_contact_info,
)
from conversation_processor import conversation_processor as cp


# --------------------------------------------------------------------- email

@pytest.mark.parametrize("value, expected", [
    ("priya@sweetcrumbs.in", "priya@sweetcrumbs.in"),
    ("  Rahul.Mehta@Example-Corp.co.uk ", "Rahul.Mehta@example-corp.co.uk"),
    ("sai+zer0@madhyamakist.com", "sai+zer0@madhyamakist.com"),
    ("<ops@acme.io>,", "ops@acme.io"),
])
def test_clean_email_keeps_real_addresses(value, expected):
    assert clean_email(value) == expected


@pytest.mark.parametrize("value", [
    "", None, "priya", "priya@", "@sweetcrumbs.in", "priya@sweetcrumbs", "priya @sweetcrumbs.in",
    "test@test.com", "abc@xyz.com", "a@a.com", "someone@example.com", "asdf@asdf.com",
    "qwerty@gmail.com", "aaaa@gmail.com", "throwaway@mailinator.com", "none@none.com", "fake@fake.com",
])
def test_clean_email_rejects_placeholders_and_malformed(value):
    assert clean_email(value) is None


# -------------------------------------------------------------------- mobile

@pytest.mark.parametrize("value, country, expected", [
    ("9876501234", "India", "9876501234"),
    ("+91 98765 01234", None, "+919876501234"),
    ("0091-98765-01234", "", "+919876501234"),
    ("+1 (415) 555-0132", "USA", "+14155550132"),
    ("020 7946 0958", "United Kingdom", "02079460958"),
])
def test_clean_mobile_keeps_plausible_numbers(value, country, expected):
    assert clean_mobile(value, country) == expected


@pytest.mark.parametrize("value, country", [
    ("", None), (None, None), ("12345", None), ("1234567890123456", None),
    ("1234567890", None), ("0123456789", None), ("9876543210", None), ("0000000000", None),
    ("9999999999", "India"), ("1212121212", None), ("+91 1234567890", None),
    ("12345 67890", "India"),      # Indian numbers start with 6-9
    ("98765", "India"),            # too short
    ("+91 987650123", None),       # 9 digits after +91
])
def test_clean_mobile_rejects_fake_numbers(value, country):
    assert clean_mobile(value, country) is None


# ---------------------------------------------------------------------- name

@pytest.mark.parametrize("value, expected", [
    ("Priya", "Priya"), (" Rahul Mehta. ", "Rahul Mehta"), ("Jo", "Jo"), ("O'Neil", "O'Neil"),
])
def test_clean_name_keeps_real_names(value, expected):
    assert clean_name(value) == expected


@pytest.mark.parametrize("value", ["", None, "test", "Test", "asdf", "xyz", "a", "x.", "123", "none", "N/A", "aaaa", "my name"])
def test_clean_name_rejects_placeholders(value):
    assert clean_name(value) is None


def test_clean_country():
    assert clean_country(" India ") == "India"
    assert clean_country("test") is None
    assert clean_country("") is None
    assert clean_country("123") is None


# ----------------------------------------------------------------- sanitise

def test_sanitise_blanks_only_the_bad_fields():
    out = sanitise_contact_info({"contact_name": "Priya", "email": "test@test.com", "mobile": "9876501234", "country": "India"})
    assert out == {"contact_name": "Priya", "email": "", "mobile": "9876501234", "country": "India"}


def test_sanitise_handles_missing_keys_and_extra_keys():
    out = sanitise_contact_info({"email": "ops@acme.io", "confidence": "high"})
    assert out["email"] == "ops@acme.io"
    assert out["contact_name"] == "" and out["mobile"] == "" and out["country"] == ""
    assert out["confidence"] == "high"


def test_sanitise_uses_country_to_judge_the_number():
    assert sanitise_contact_info({"mobile": "1234567890", "country": "India"})["mobile"] == ""
    assert sanitise_contact_info({"mobile": "5551234567", "country": "India"})["mobile"] == ""
    assert sanitise_contact_info({"mobile": "5551234567", "country": "USA"})["mobile"] == "5551234567"


# --------------------------------------------------------- processor wiring

def test_processor_does_not_save_placeholder_contact_details():
    """A message with only fake details is treated as no contact info at all."""
    fake = {"contact_name": "test", "email": "test@test.com", "mobile": "1234567890", "country": ""}
    with patch.object(cp, "_update_session_request_type"), \
         patch.object(cp, "_detect_info_with_llm", return_value=fake), \
         patch.object(cp, "_save_info_to_database") as save:
        cp.process_conversation("I'm test, test@test.com, 1234567890", "s1", cp.agent_type.SALES, "ZERO")
    save.assert_not_called()


def test_processor_saves_the_real_fields_and_drops_the_fake_ones():
    mixed = {"contact_name": "Priya", "email": "abc@xyz.com", "mobile": "+91 98765 01234", "country": "India"}
    with patch.object(cp, "_update_session_request_type"), \
         patch.object(cp, "_detect_info_with_llm", return_value=mixed), \
         patch.object(cp, "_save_info_to_database") as save:
        cp.process_conversation("Priya, abc@xyz.com, +91 98765 01234, India", "s2", cp.agent_type.SALES, "ZERO")
    save.assert_called_once()
    saved = save.call_args.args[1]
    assert saved["contact_name"] == "Priya"
    assert saved["email"] == ""
    assert saved["mobile"] == "+919876501234"
    assert saved["country"] == "India"


def test_processor_leaves_generic_name_detection_alone():
    generic = {"name_detected": True, "contact_name": "Priya", "confidence": "high"}
    with patch.object(cp, "_update_session_request_type"), \
         patch.object(cp, "_detect_info_with_llm", return_value=generic), \
         patch.object(cp, "_save_info_to_database") as save:
        cp.process_conversation("I'm Priya", "s3", cp.agent_type.GENERIC, "ZERO")
    save.assert_called_once()
    assert save.call_args.args[1] is generic
