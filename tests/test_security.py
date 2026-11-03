"""
tests/test_security.py

Unit tests for app/security.py:
  - sanitize_input / sanitize_text     — XSS stripping
  - sanitize_doi                        — DOI validation
  - sanitize_orcid                      — ORCID validation
  - sanitize_author_list                — list sanitisation
  - compute_record_checksum             — HMAC-SHA256 determinism
  - verify_record_checksum              — tamper detection
  - sign_summary / verify_summary       — round-trip integrity
  - IntegrityError                      — raised on tamper

No network calls. No file I/O.
"""

from __future__ import annotations

import copy

import pytest

from app.security import (
    IntegrityError,
    compute_record_checksum,
    sanitize_author_list,
    sanitize_doi,
    sanitize_input,
    sanitize_orcid,
    sanitize_text,
    sign_summary,
    verify_record_checksum,
    verify_summary,
)


# ---------------------------------------------------------------------------
# sanitize_input / sanitize_text (they are the same function)
# ---------------------------------------------------------------------------

class TestSanitizeInput:
    def test_strips_script_tag(self):
        result = sanitize_input("<script>alert('xss')</script>Hello")
        assert "<script>" not in result
        assert "Hello" in result

    def test_strips_img_onerror(self):
        result = sanitize_input('<img src=x onerror=alert(1)>title')
        assert "<img" not in result
        assert "onerror" not in result

    def test_strips_all_html_tags(self):
        result = sanitize_input("<b>Bold</b> and <i>italic</i>")
        assert "<b>" not in result
        assert "<i>" not in result
        assert "Bold" in result
        assert "italic" in result

    def test_strips_leading_trailing_whitespace(self):
        result = sanitize_input("  Alice Smith  ")
        assert result == "Alice Smith"

    def test_none_returns_none(self):
        assert sanitize_input(None) is None

    def test_empty_string_returns_none(self):
        assert sanitize_input("") is None

    def test_whitespace_only_returns_none(self):
        assert sanitize_input("   ") is None

    def test_max_length_truncation(self):
        long_text = "A" * 3000
        result = sanitize_input(long_text, max_length=100)
        assert len(result) == 100

    def test_normal_text_unchanged(self):
        assert sanitize_input("Deep Learning: A Review") == "Deep Learning: A Review"

    def test_sanitize_text_alias(self):
        """sanitize_text and sanitize_input must be functionally identical."""
        raw = "<b>Test</b>"
        assert sanitize_text(raw) == sanitize_input(raw)

    def test_unicode_preserved(self):
        result = sanitize_input("Lüís Madeira")
        assert result == "Lüís Madeira"

    def test_javascript_uri_stripped(self):
        result = sanitize_input('<a href="javascript:void(0)">click</a>')
        assert "javascript:" not in result


# ---------------------------------------------------------------------------
# sanitize_doi
# ---------------------------------------------------------------------------

class TestSanitizeDoi:
    def test_valid_doi(self):
        assert sanitize_doi("10.1000/xyz123") == "10.1000/xyz123"

    def test_strips_https_prefix(self):
        assert sanitize_doi("https://doi.org/10.1000/xyz") == "10.1000/xyz"

    def test_strips_http_dx_prefix(self):
        assert sanitize_doi("http://dx.doi.org/10.1000/xyz") == "10.1000/xyz"

    def test_lowercases(self):
        assert sanitize_doi("10.1000/XYZ") == "10.1000/xyz"

    def test_invalid_doi_returns_none(self):
        assert sanitize_doi("NOT-A-DOI") is None

    def test_none_returns_none(self):
        assert sanitize_doi(None) is None

    def test_empty_returns_none(self):
        assert sanitize_doi("") is None

    def test_doi_without_slash_returns_none(self):
        assert sanitize_doi("10.1000") is None

    def test_real_doi(self):
        result = sanitize_doi("10.1038/nature14539")
        assert result == "10.1038/nature14539"


# ---------------------------------------------------------------------------
# sanitize_orcid
# ---------------------------------------------------------------------------

class TestSanitizeOrcid:
    def test_valid_orcid(self):
        assert sanitize_orcid("0000-0002-1825-0097") == "0000-0002-1825-0097"

    def test_strips_url_prefix(self):
        assert sanitize_orcid("https://orcid.org/0000-0002-1825-0097") == "0000-0002-1825-0097"

    def test_last_char_x_accepted(self):
        result = sanitize_orcid("0000-0002-1825-009X")
        assert result == "0000-0002-1825-009X"

    def test_uppercased(self):
        result = sanitize_orcid("0000-0002-1825-009x")
        assert result == "0000-0002-1825-009X"

    def test_invalid_orcid_returns_none(self):
        assert sanitize_orcid("not-an-orcid") is None

    def test_none_returns_none(self):
        assert sanitize_orcid(None) is None

    def test_wrong_format_returns_none(self):
        assert sanitize_orcid("0000-0000-0000") is None  # only 3 groups


# ---------------------------------------------------------------------------
# sanitize_author_list
# ---------------------------------------------------------------------------

class TestSanitizeAuthorList:
    def test_strips_html_in_names(self):
        result = sanitize_author_list(["<b>Alice Smith</b>", "Bob Jones"])
        assert result == ["Alice Smith", "Bob Jones"]

    def test_drops_empty_names(self):
        result = sanitize_author_list(["Alice", "", "   ", "Bob"])
        assert result == ["Alice", "Bob"]

    def test_none_entries_dropped(self):
        result = sanitize_author_list(["Alice", None, "Bob"])
        assert result == ["Alice", "Bob"]

    def test_empty_list(self):
        assert sanitize_author_list([]) == []


# ---------------------------------------------------------------------------
# compute_record_checksum
# ---------------------------------------------------------------------------

class TestComputeRecordChecksum:
    def test_deterministic(self):
        data = {"a": 1, "b": [1, 2, 3]}
        assert compute_record_checksum(data) == compute_record_checksum(data)

    def test_key_order_independent(self):
        d1 = {"a": 1, "b": 2}
        d2 = {"b": 2, "a": 1}
        assert compute_record_checksum(d1) == compute_record_checksum(d2)

    def test_different_data_different_checksum(self):
        d1 = {"count": 10}
        d2 = {"count": 11}
        assert compute_record_checksum(d1) != compute_record_checksum(d2)

    def test_returns_hex_string(self):
        result = compute_record_checksum({"x": 1})
        assert isinstance(result, str)
        assert len(result) == 64  # SHA-256 → 32 bytes → 64 hex chars
        int(result, 16)  # must be valid hex

    def test_list_input(self):
        data = [1, 2, 3]
        c1 = compute_record_checksum(data)
        c2 = compute_record_checksum(data)
        assert c1 == c2

    def test_nested_dict(self):
        data = {"outer": {"inner": [1, 2]}}
        c = compute_record_checksum(data)
        assert len(c) == 64


# ---------------------------------------------------------------------------
# verify_record_checksum
# ---------------------------------------------------------------------------

class TestVerifyRecordChecksum:
    def test_correct_checksum_returns_true(self):
        data = {"total": 100, "self": 5}
        checksum = compute_record_checksum(data)
        assert verify_record_checksum(data, checksum) is True

    def test_tampered_data_returns_false(self):
        data = {"total": 100}
        checksum = compute_record_checksum(data)
        tampered = {"total": 999}
        assert verify_record_checksum(tampered, checksum) is False

    def test_wrong_checksum_returns_false(self):
        data = {"total": 100}
        assert verify_record_checksum(data, "a" * 64) is False

    def test_empty_string_checksum_returns_false(self):
        data = {"x": 1}
        assert verify_record_checksum(data, "") is False


# ---------------------------------------------------------------------------
# sign_summary / verify_summary
# ---------------------------------------------------------------------------

class TestSignAndVerify:
    def _base_summary(self):
        return {
            "total_raw": 100,
            "deduplicated_count": 80,
            "self_citation_count": 5,
            "external_citation_count": 70,
            "unresolved_count": 5,
        }

    def test_sign_adds_integrity_block(self):
        signed = sign_summary(self._base_summary())
        assert "_integrity" in signed
        assert "checksum" in signed["_integrity"]
        assert "generated_at" in signed["_integrity"]
        assert signed["_integrity"]["algorithm"] == "hmac-sha256"

    def test_signed_verifies_true(self):
        signed = sign_summary(self._base_summary())
        assert verify_summary(signed) is True

    def test_tampered_field_raises_integrity_error(self):
        signed = sign_summary(self._base_summary())
        signed["self_citation_count"] = 9999  # tamper
        with pytest.raises(IntegrityError):
            verify_summary(signed)

    def test_tampered_checksum_raises_integrity_error(self):
        signed = sign_summary(self._base_summary())
        signed["_integrity"]["checksum"] = "0" * 64  # forge
        with pytest.raises(IntegrityError):
            verify_summary(signed)

    def test_missing_integrity_block_raises(self):
        with pytest.raises(IntegrityError):
            verify_summary(self._base_summary())  # unsigned

    def test_added_field_raises_integrity_error(self):
        signed = sign_summary(self._base_summary())
        signed["extra_field"] = "injected"  # add field post-signing
        with pytest.raises(IntegrityError):
            verify_summary(signed)

    def test_re_signing_existing_summary_works(self):
        """sign_summary on an already-signed dict must strip old _integrity first."""
        signed = sign_summary(self._base_summary())
        re_signed = sign_summary(signed)
        assert verify_summary(re_signed) is True

    def test_original_fields_preserved(self):
        summary = self._base_summary()
        signed = sign_summary(summary)
        for key in summary:
            assert key in signed

    def test_checksum_is_64_hex_chars(self):
        signed = sign_summary(self._base_summary())
        checksum = signed["_integrity"]["checksum"]
        assert len(checksum) == 64
        int(checksum, 16)  # valid hex
