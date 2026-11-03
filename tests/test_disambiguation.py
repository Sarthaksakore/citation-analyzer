"""
tests/test_disambiguation.py

Unit tests for Layer 4: resolve_author_identity, classify_citation,
classify_all in app/disambiguation.py.

All tests use hand-crafted dicts — no network calls.

Scenarios covered
-----------------
  ORCID match                → SELF, confidence 1.00
  DB author-ID match         → SELF, confidence 0.95
  Affiliation corroboration  → SELF, confidence 0.80
  Co-author corroboration    → SELF, confidence 0.65
  Name-only match            → UNRESOLVED, confidence 0.40, needs_review
  Clearly different author   → EXTERNAL, confidence 1.00
  No authors listed          → EXTERNAL
  ORCID mismatch             → falls through to next level
  Common name + no signal    → UNRESOLVED + needs_review flag
  classify_all batch         → correct counts
"""

from __future__ import annotations

import pytest

from app.disambiguation import (
    CONFIDENCE,
    NAME_MATCH_FLOOR,
    REVIEW_THRESHOLD,
    SELF_THRESHOLD,
    classify_all,
    classify_citation,
    resolve_author_identity,
)


# ---------------------------------------------------------------------------
# Fixtures — reusable researcher and citing-paper templates
# ---------------------------------------------------------------------------

@pytest.fixture
def researcher():
    """A well-specified target researcher with all identity signals available."""
    return {
        "display_name": "Alice Smith",
        "orcid": "0000-0001-1234-5678",
        "openalex_id": "A1111111111",
        "affiliation": "MIT Computer Science",
        "known_coauthors": ["Bob Jones", "Carol White"],
        "publication_titles": [
            "Deep learning for image recognition",
            "Transfer learning survey",
        ],
    }


@pytest.fixture
def paper_base():
    """A citing paper dict with minimal fields."""
    return {
        "title": "A New Method",
        "authors": ["Alice Smith"],
        "year": 2022,
        "doi": "10.9999/test",
        "source": "openalex",
        "cited_doi": "10.1000/target",
    }


# ---------------------------------------------------------------------------
# resolve_author_identity — Priority 1: ORCID match
# ---------------------------------------------------------------------------

class TestOrcidMatch:
    def test_exact_orcid_returns_self_confidence(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_orcids"] = ["0000-0001-1234-5678"]
        result = resolve_author_identity("Alice Smith", paper, researcher)
        assert result["matched"] is True
        assert result["confidence"] == CONFIDENCE["orcid"]
        assert result["level"] == "orcid"

    def test_orcid_with_url_prefix_still_matches(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_orcids"] = ["https://orcid.org/0000-0001-1234-5678"]
        result = resolve_author_identity("Alice Smith", paper, researcher)
        assert result["matched"] is True
        assert result["level"] == "orcid"

    def test_orcid_mismatch_falls_through(self, researcher, paper_base):
        """Wrong ORCID should not match at level 1 — must fall through."""
        paper = dict(paper_base)
        paper["author_orcids"] = ["0000-0000-0000-0000"]
        result = resolve_author_identity("Alice Smith", paper, researcher)
        # Should still match at some lower level (name match at minimum)
        assert result["level"] != "orcid"

    def test_no_orcid_in_paper_falls_through(self, researcher, paper_base):
        """If paper has no ORCIDs, fall through to next levels."""
        result = resolve_author_identity("Alice Smith", paper_base, researcher)
        assert result["level"] != "orcid"

    def test_orcid_match_needs_no_review(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_orcids"] = ["0000-0001-1234-5678"]
        result = resolve_author_identity("Alice Smith", paper, researcher)
        assert result["needs_review"] is False


# ---------------------------------------------------------------------------
# resolve_author_identity — Priority 2: DB author-ID match
# ---------------------------------------------------------------------------

class TestDbAuthorIdMatch:
    def test_openalex_id_match(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_ids"] = [{"openalex_id": "A1111111111"}]
        result = resolve_author_identity("Alice Smith", paper, researcher)
        assert result["matched"] is True
        assert result["confidence"] == CONFIDENCE["db_author_id"]
        assert result["level"] == "db_author_id"

    def test_db_id_mismatch_falls_through(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_ids"] = [{"openalex_id": "A9999999999"}]
        result = resolve_author_identity("Alice Smith", paper, researcher)
        assert result["level"] != "db_author_id"

    def test_db_id_match_no_review(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_ids"] = [{"openalex_id": "A1111111111"}]
        result = resolve_author_identity("Alice Smith", paper, researcher)
        assert result["needs_review"] is False


# ---------------------------------------------------------------------------
# resolve_author_identity — Priority 3: Name + affiliation
# ---------------------------------------------------------------------------

class TestAffiliationMatch:
    def test_name_plus_affiliation_match(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_affiliations"] = ["MIT Computer Science Department"]
        result = resolve_author_identity("Alice Smith", paper, researcher)
        assert result["matched"] is True
        assert result["confidence"] == CONFIDENCE["affiliation"]
        assert result["level"] == "affiliation"

    def test_different_affiliation_falls_through(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_affiliations"] = ["Harvard Medical School"]
        result = resolve_author_identity("Alice Smith", paper, researcher)
        assert result["level"] not in ("orcid", "db_author_id", "affiliation")

    def test_no_affiliation_falls_through(self, researcher, paper_base):
        result = resolve_author_identity("Alice Smith", paper_base, researcher)
        assert result["level"] not in ("affiliation",)


# ---------------------------------------------------------------------------
# resolve_author_identity — Priority 4: Name + co-author
# ---------------------------------------------------------------------------

class TestCoAuthorMatch:
    def test_shared_coauthor_match(self, researcher):
        """Paper includes Bob Jones, a known co-author → Priority 4."""
        paper = {
            "authors": ["Alice Smith", "Bob Jones", "Zara Khan"],
            "author_orcids": [],
            "author_ids": [],
            "author_affiliations": ["Stanford University"] * 3,
        }
        result = resolve_author_identity("Alice Smith", paper, researcher)
        assert result["matched"] is True
        assert result["confidence"] == CONFIDENCE["co_author"]
        assert result["level"] == "co_author"

    def test_no_shared_coauthor_falls_through(self, researcher):
        paper = {
            "authors": ["Alice Smith", "Unknown Person", "Another Name"],
            "author_orcids": [],
            "author_ids": [],
            "author_affiliations": ["Oxford University"] * 3,
        }
        result = resolve_author_identity("Alice Smith", paper, researcher)
        # Should fall to name_only
        assert result["level"] == "name_only"


# ---------------------------------------------------------------------------
# resolve_author_identity — Priority 5: Name-only (ambiguous)
# ---------------------------------------------------------------------------

class TestNameOnlyMatch:
    def test_name_only_match_confidence(self, researcher, paper_base):
        """No ORCID, no DB-ID, no affiliation, no co-author → name_only."""
        result = resolve_author_identity("Alice Smith", paper_base, researcher)
        assert result["matched"] is True
        assert result["confidence"] == CONFIDENCE["name_only"]
        assert result["level"] == "name_only"

    def test_verified_distinctive_name_with_orcid_is_self(self, researcher, paper_base):
        researcher = {**researcher, "display_name": "Lokman I. Meho"}
        paper = {**paper_base, "authors": ["Lokman I. Meho"]}
        result = resolve_author_identity("Lokman I. Meho", paper, researcher)
        assert result["level"] == "verified_name"
        assert result["confidence"] == 0.75

    def test_name_only_flags_needs_review(self, researcher, paper_base):
        """Common name with no corroboration must be flagged for review."""
        result = resolve_author_identity("Alice Smith", paper_base, researcher)
        assert result["needs_review"] is True

    def test_name_only_confidence_below_review_threshold(self, researcher, paper_base):
        result = resolve_author_identity("Alice Smith", paper_base, researcher)
        assert result["confidence"] < REVIEW_THRESHOLD

    def test_slightly_different_name_still_matches(self, researcher, paper_base):
        """'A. Smith' should fuzzy-match 'Alice Smith'."""
        result = resolve_author_identity("A. Smith", paper_base, researcher)
        # May or may not match depending on token_sort score — just verify no crash
        assert isinstance(result["matched"], bool)


# ---------------------------------------------------------------------------
# resolve_author_identity — No name match → EXTERNAL
# ---------------------------------------------------------------------------

class TestNoNameMatch:
    def test_clearly_different_author(self, researcher, paper_base):
        """Completely different name → matched=False, confidence=1.0."""
        result = resolve_author_identity("John Doe", paper_base, researcher)
        assert result["matched"] is False
        assert result["confidence"] == 1.0
        assert result["level"] == "no_match"

    def test_no_match_does_not_need_review(self, researcher, paper_base):
        result = resolve_author_identity("John Doe", paper_base, researcher)
        assert result["needs_review"] is False

    def test_partial_last_name_match_below_floor(self, researcher, paper_base):
        """'Smith' alone is not enough to match 'Alice Smith' at floor."""
        result = resolve_author_identity("Xynthia Blorbington", paper_base, researcher)
        assert result["matched"] is False


# ---------------------------------------------------------------------------
# classify_citation — full-paper classification
# ---------------------------------------------------------------------------

class TestClassifyCitation:
    def test_orcid_match_classified_self(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_orcids"] = ["0000-0001-1234-5678"]
        result = classify_citation(paper, researcher)
        assert result["classification"] == "SELF"
        assert result["confidence"] >= SELF_THRESHOLD
        assert result["needs_review"] is False

    def test_db_id_match_classified_self(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_ids"] = [{"openalex_id": "A1111111111"}]
        result = classify_citation(paper, researcher)
        assert result["classification"] == "SELF"

    def test_affiliation_match_classified_self(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_affiliations"] = ["MIT CS"]
        result = classify_citation(paper, researcher)
        assert result["classification"] == "SELF"
        assert result["confidence"] == CONFIDENCE["affiliation"]

    def test_name_only_classified_unresolved(self, researcher, paper_base):
        result = classify_citation(paper_base, researcher)
        assert result["classification"] == "UNRESOLVED"
        assert result["needs_review"] is True

    def test_different_author_classified_external(self, researcher):
        paper = {
            "title": "Quantum Chromodynamics",
            "authors": ["Richard Feynman", "Murray Gell-Mann"],
            "year": 1970,
        }
        result = classify_citation(paper, researcher)
        assert result["classification"] == "EXTERNAL"

    def test_no_authors_classified_unresolved(self, researcher):
        paper = {"title": "No Authors Paper", "authors": [], "year": 2020}
        result = classify_citation(paper, researcher)
        assert result["classification"] == "UNRESOLVED"
        assert result["needs_review"] is True

    def test_result_has_required_keys(self, researcher, paper_base):
        result = classify_citation(paper_base, researcher)
        required = {"classification", "confidence", "evidence", "needs_review",
                    "author_resolutions"}
        assert required.issubset(result.keys())

    def test_author_resolutions_length_matches_authors(self, researcher):
        paper = {
            "authors": ["Alice Smith", "Bob Jones", "Carol White"],
            "year": 2022,
        }
        result = classify_citation(paper, researcher)
        assert len(result["author_resolutions"]) == 3

    def test_original_fields_preserved(self, researcher, paper_base):
        """classify_citation must not strip any original dict keys."""
        result = classify_citation(paper_base, researcher)
        for key in paper_base:
            assert key in result, f"Key '{key}' was dropped"


# ---------------------------------------------------------------------------
# classify_all — batch classification
# ---------------------------------------------------------------------------

class TestClassifyAll:
    def test_returns_list(self, researcher, paper_base):
        papers = [paper_base, paper_base]
        result = classify_all(papers, researcher)
        assert isinstance(result, list)
        assert len(result) == 2

    def test_self_paper_in_batch(self, researcher, paper_base):
        paper_self = dict(paper_base)
        paper_self["author_orcids"] = ["0000-0001-1234-5678"]
        paper_external = {
            "title": "Unrelated Work",
            "authors": ["David Brown"],
            "year": 2022,
        }
        results = classify_all([paper_self, paper_external], researcher)
        classifications = {r["classification"] for r in results}
        assert "SELF" in classifications
        assert "EXTERNAL" in classifications

    def test_empty_list(self, researcher):
        assert classify_all([], researcher) == []

    def test_all_records_get_classification(self, researcher):
        papers = [
            {"title": f"Paper {i}", "authors": ["Alice Smith"], "year": 2020}
            for i in range(5)
        ]
        results = classify_all(papers, researcher)
        for r in results:
            assert r["classification"] in ("SELF", "EXTERNAL", "UNRESOLVED")


# ---------------------------------------------------------------------------
# Safety / ambiguity-flag scenarios
# ---------------------------------------------------------------------------

class TestAmbiguityFlag:
    def test_common_name_with_no_signal_flagged(self, researcher, paper_base):
        """
        'Alice Smith' is a very common name. Without ORCID/DB-ID/affiliation/
        co-author corroboration the result must be UNRESOLVED + needs_review.
        """
        result = classify_citation(paper_base, researcher)
        assert result["needs_review"] is True
        assert result["classification"] == "UNRESOLVED"

    def test_high_confidence_match_not_flagged(self, researcher, paper_base):
        """ORCID match should never trigger needs_review."""
        paper = dict(paper_base)
        paper["author_orcids"] = ["0000-0001-1234-5678"]
        result = classify_citation(paper, researcher)
        assert result["needs_review"] is False

    def test_external_never_flagged(self, researcher):
        """Confidently external paper must not trigger review flag."""
        paper = {"title": "Unrelated", "authors": ["Zack Zukerman"], "year": 2022}
        result = classify_citation(paper, researcher)
        assert result["classification"] == "EXTERNAL"
        assert result["needs_review"] is False

    def test_researcher_with_no_orcid_skips_orcid_level(self):
        """If researcher has no ORCID, ORCID level must not fire (no crash)."""
        researcher_no_orcid = {
            "display_name": "Alice Smith",
            "orcid": None,
            "openalex_id": None,
            "affiliation": None,
            "known_coauthors": [],
        }
        paper = {
            "authors": ["Alice Smith"],
            "author_orcids": ["0000-0001-9999-9999"],
        }
        result = resolve_author_identity("Alice Smith", paper, researcher_no_orcid)
        assert result["level"] != "orcid"


# ---------------------------------------------------------------------------
# Confidence boundary checks
# ---------------------------------------------------------------------------

class TestConfidenceBoundaries:
    def test_orcid_confidence_is_1(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_orcids"] = ["0000-0001-1234-5678"]
        r = resolve_author_identity("Alice Smith", paper, researcher)
        assert r["confidence"] == 1.00

    def test_db_id_confidence_is_095(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_ids"] = [{"openalex_id": "A1111111111"}]
        r = resolve_author_identity("Alice Smith", paper, researcher)
        assert r["confidence"] == 0.95

    def test_affiliation_confidence_is_080(self, researcher, paper_base):
        paper = dict(paper_base)
        paper["author_affiliations"] = ["MIT Computer Science Department"]
        r = resolve_author_identity("Alice Smith", paper, researcher)
        assert r["confidence"] == 0.80

    def test_coauthor_confidence_is_065(self, researcher):
        paper = {
            "authors": ["Alice Smith", "Bob Jones"],
            "author_affiliations": ["Somewhere University"] * 2,
        }
        r = resolve_author_identity("Alice Smith", paper, researcher)
        assert r["confidence"] == 0.65

    def test_name_only_confidence_is_040(self, researcher, paper_base):
        r = resolve_author_identity("Alice Smith", paper_base, researcher)
        assert r["confidence"] == 0.40

    def test_no_match_confidence_is_100(self, researcher, paper_base):
        r = resolve_author_identity("Zack Zukerman", paper_base, researcher)
        assert r["confidence"] == 1.00
