"""
app/security.py — Payload sanitisation, rate-limiting, and integrity checks.

Per the project synopsis Safety & Cybersecurity Considerations:

  sanitize_input(text)          — strip HTML/XSS (alias for sanitize_text)
  sanitize_text(text)           — strip HTML/XSS using bleach
  sanitize_doi(doi)             — validate and normalise a DOI
  sanitize_orcid(orcid)         — validate an ORCID identifier

  compute_record_checksum(data) — SHA-256 of a serialised result set
  verify_record_checksum(data, expected_checksum)  — tamper-detection
  sign_summary(summary_dict)    — attach checksum + timestamp to a result dict
  verify_summary(summary_dict)  — raise IntegrityError if checksum fails

  limiter                       — shared SlowAPI Limiter (import into main.py)
  CITATION_SEARCH_RATE_LIMIT    — per-route string e.g. "20/minute"

Design notes
------------
- bleach strips ALL HTML tags & attributes by default. Correct for
  plain-text scholarly metadata (titles, names, affiliations).
- SQL injection is mitigated by SQLAlchemy parameterised queries; bleach
  covers XSS for anything rendered in a browser.
- HMAC-SHA256 is used for the integrity checksum so a server-side secret
  prevents forged checksums (pure SHA-256 without a key would be forgeable).
- API keys / secrets are loaded exclusively from .env — never hardcoded.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import os
import re
import time
from typing import Any, Dict, Optional

import bleach
from dotenv import load_dotenv
from slowapi import Limiter
from slowapi.util import get_remote_address

load_dotenv()


# ---------------------------------------------------------------------------
# Configuration — loaded from environment, never hardcoded
# ---------------------------------------------------------------------------

# Secret used for HMAC-SHA256 integrity signing of result summaries
_INTEGRITY_SECRET: bytes = os.getenv(
    "INTEGRITY_SECRET", "change-me-to-a-random-secret"
).encode()

# Per-minute rate limit for the citation search endpoint
CITATION_SEARCH_RATE_LIMIT: str = os.getenv("RATE_LIMIT_PER_MINUTE", "20") + "/minute"

# General default limit applied to every route unless overridden
_DEFAULT_RATE_LIMIT: str = os.getenv("RATE_LIMIT_DEFAULT", "60") + "/minute"


# ---------------------------------------------------------------------------
# Rate limiter — one shared instance, imported by main.py
# ---------------------------------------------------------------------------

limiter = Limiter(
    key_func=get_remote_address,
    default_limits=[_DEFAULT_RATE_LIMIT],
)


# ---------------------------------------------------------------------------
# Bleach configuration — strip ALL HTML
# ---------------------------------------------------------------------------

_ALLOWED_TAGS: list = []        # no HTML tags permitted at all
_ALLOWED_ATTRS: dict = {}       # no attributes permitted


# ---------------------------------------------------------------------------
# Input sanitisation
# ---------------------------------------------------------------------------


def sanitize_input(text: Optional[str], max_length: int = 2000) -> Optional[str]:
    """
    Alias for ``sanitize_text`` — the name specified in the project synopsis.

    Strip/escape HTML and XSS vectors from any scraped or user-submitted
    string before it is stored or displayed.

    Parameters
    ----------
    text:
        Raw input string.
    max_length:
        Hard ceiling on the returned string length (default 2000).

    Returns
    -------
    Sanitised string, or None if the input is None or empty after cleaning.

    Examples
    --------
    >>> sanitize_input("<script>alert('xss')</script>Hello")
    'Hello'
    >>> sanitize_input("  Alice Smith  ")
    'Alice Smith'
    """
    return sanitize_text(text, max_length=max_length)


def sanitize_text(value: Optional[str], max_length: int = 2000) -> Optional[str]:
    """
    Strip HTML/XSS vectors from a free-text string and truncate.

    Uses bleach with an empty tag allowlist (all HTML stripped, not escaped),
    which is the correct policy for scholarly metadata that should contain
    only plain text.

    Parameters
    ----------
    value:
        Raw input string (e.g. title, author name, affiliation).
    max_length:
        Maximum allowed length after sanitisation (default 2000 chars).

    Returns
    -------
    Sanitised string, or None if input is None/empty after cleaning.
    """
    if value is None:
        return None
    cleaned = bleach.clean(value, tags=_ALLOWED_TAGS, attributes=_ALLOWED_ATTRS, strip=True)
    cleaned = cleaned.strip()
    if not cleaned:
        return None
    return cleaned[:max_length]


def sanitize_author_list(authors: list) -> list:
    """
    Sanitise every entry in an author list.
    Drops any entry that is None or empty after sanitisation.
    """
    cleaned = []
    for name in authors:
        s = sanitize_text(str(name) if name is not None else "")
        if s:
            cleaned.append(s)
    return cleaned


# ---------------------------------------------------------------------------
# DOI validation & normalisation
# ---------------------------------------------------------------------------

_DOI_RE = re.compile(r"^10\.\d{4,9}/\S+$", re.IGNORECASE)


def sanitize_doi(doi: Optional[str]) -> Optional[str]:
    """
    Validate and normalise a DOI string.

    - Strips leading URL prefixes (https://doi.org/, http://dx.doi.org/)
    - Lowercases
    - Returns None if the result does not match the DOI pattern

    Examples
    --------
    >>> sanitize_doi("https://doi.org/10.1000/xyz123")
    '10.1000/xyz123'
    >>> sanitize_doi("NOT-A-DOI")
    None
    """
    if not doi:
        return None
    doi = doi.strip()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi, flags=re.IGNORECASE)
    doi = doi.lower()
    return doi if _DOI_RE.match(doi) else None


# ---------------------------------------------------------------------------
# ORCID validation
# ---------------------------------------------------------------------------

_ORCID_RE = re.compile(r"^\d{4}-\d{4}-\d{4}-\d{3}[\dX]$", re.IGNORECASE)


def sanitize_orcid(orcid: Optional[str]) -> Optional[str]:
    """
    Validate an ORCID identifier.

    Strips the URL prefix and validates the 0000-0000-0000-000X pattern.
    Returns the bare uppercased ORCID or None if invalid.

    Examples
    --------
    >>> sanitize_orcid("https://orcid.org/0000-0002-1825-0097")
    '0000-0002-1825-0097'
    >>> sanitize_orcid("not-an-orcid")
    None
    """
    if not orcid:
        return None
    orcid = orcid.strip()
    orcid = re.sub(r"^https?://orcid\.org/", "", orcid, flags=re.IGNORECASE)
    return orcid.upper() if _ORCID_RE.match(orcid) else None


# ---------------------------------------------------------------------------
# Integrity / checksum
# ---------------------------------------------------------------------------


class IntegrityError(ValueError):
    """Raised when a stored summary's checksum does not match its content."""


def compute_record_checksum(data: Any) -> str:
    """
    Compute an HMAC-SHA256 checksum of a result set.

    The data is serialised to a canonical JSON string (keys sorted,
    no extra whitespace) before hashing, so the checksum is stable
    regardless of dict-insertion order.

    Parameters
    ----------
    data:
        Any JSON-serialisable object (dict, list, int, etc.).

    Returns
    -------
    Hex-encoded HMAC-SHA256 digest string (64 hex chars).

    Notes
    -----
    - A server-side secret (``INTEGRITY_SECRET`` from .env) is used as the
      HMAC key, so an attacker cannot forge a matching checksum without the
      secret.
    - For pure tamper-detection without a secret (e.g. development), set
      ``INTEGRITY_SECRET=dev-insecure-key`` in .env.
    """
    canonical = json.dumps(data, sort_keys=True, separators=(",", ":"), default=str)
    return hmac.new(_INTEGRITY_SECRET, canonical.encode(), hashlib.sha256).hexdigest()


def verify_record_checksum(data: Any, expected_checksum: str) -> bool:
    """
    Verify that ``data`` matches the previously computed ``expected_checksum``.

    Uses ``hmac.compare_digest`` to prevent timing-based attacks.

    Returns
    -------
    True if the checksum matches, False otherwise.
    """
    actual = compute_record_checksum(data)
    return hmac.compare_digest(actual, expected_checksum)


def sign_summary(summary: Dict[str, Any]) -> Dict[str, Any]:
    """
    Attach a checksum and generation timestamp to a statistics/summary dict.

    The checksum covers the entire summary content (excluding the
    ``_integrity`` wrapper itself), making silent post-generation edits
    detectable.

    Parameters
    ----------
    summary:
        The statistics/citation-summary dict to sign
        (e.g. output of ``compute_statistics()``).

    Returns
    -------
    A new dict with the original summary plus an ``_integrity`` sub-dict::

        {
          ...original summary fields...,
          "_integrity": {
              "checksum":   "<64-char hex HMAC-SHA256>",
              "generated_at": <unix timestamp float>,
              "algorithm":  "hmac-sha256"
          }
        }
    """
    signed = dict(summary)
    # Remove any existing integrity block before computing checksum
    signed.pop("_integrity", None)
    checksum = compute_record_checksum(signed)
    signed["_integrity"] = {
        "checksum": checksum,
        "generated_at": time.time(),
        "algorithm": "hmac-sha256",
    }
    return signed


def verify_summary(summary: Dict[str, Any]) -> bool:
    """
    Verify the integrity of a signed summary dict.

    Parameters
    ----------
    summary:
        A dict previously returned by ``sign_summary()``.

    Returns
    -------
    True if the checksum is valid.

    Raises
    ------
    IntegrityError
        If the ``_integrity`` block is missing or the checksum does not match.
    """
    if "_integrity" not in summary:
        raise IntegrityError("Summary is missing the '_integrity' block — cannot verify.")

    stored_checksum = summary["_integrity"].get("checksum", "")
    # Reconstruct the payload that was signed (remove the _integrity wrapper)
    payload = {k: v for k, v in summary.items() if k != "_integrity"}

    if not verify_record_checksum(payload, stored_checksum):
        raise IntegrityError(
            "Integrity check FAILED — the summary has been altered since generation."
        )
    return True
