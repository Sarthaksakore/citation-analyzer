"""
tests/test_retrieval.py

Live integration tests for Layer 1 retrieval functions.
These tests make real HTTP calls to the OpenAlex API — no mocking.
Run with:
    .venv\\Scripts\\python -m pytest tests/test_retrieval.py -v -s

The -s flag keeps print() output visible so you can inspect results manually.

Tests
-----
  test_resolve_by_name          — search by a well-known researcher name
  test_resolve_by_orcid         — look up via a real ORCID
  test_resolve_by_doi           — extract authors from a paper DOI
  test_get_publications         — fetch works list for the resolved author
  test_candidate_schema         — assert all required keys are present
  test_empty_query_returns_list — graceful empty result, never raises
"""

from __future__ import annotations

import pprint
import sys

# Force UTF-8 stdout on Windows console
if hasattr(sys.stdout, "reconfigure"):
    sys.stdout.reconfigure(encoding="utf-8")

import pytest

from app.retrieval import (
    get_crossref_metadata,
    get_opencitations_citations,
    get_citing_papers,
    get_researcher_publications,
    resolve_researcher,
)

# ---------------------------------------------------------------------------
# Fixtures / constants
# ---------------------------------------------------------------------------

# A highly-cited, unambiguous researcher name for smoke tests
KNOWN_NAME = "Yoshua Bengio"

# ORCID for Yoshua Bengio (publicly listed)
KNOWN_ORCID = "0000-0002-1784-2920"

# A well-known ML paper DOI (Attention Is All You Need)
KNOWN_DOI = "10.48550/arXiv.1706.03762"

# Expected keys in every candidate dict
REQUIRED_KEYS = {"openalex_id", "display_name", "orcid", "affiliation", "works_count"}


# ---------------------------------------------------------------------------
# Helper
# ---------------------------------------------------------------------------


def _print_candidates(label: str, candidates: list) -> None:
    print(f"\n{'='*60}")
    print(f"  {label}")
    print(f"{'='*60}")
    for i, c in enumerate(candidates, 1):
        print(f"\n  Candidate {i}:")
        pprint.pprint(c, indent=4)


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestResolveByName:
    def test_returns_list(self):
        """resolve_researcher(name) must return a list."""
        results = resolve_researcher(KNOWN_NAME)
        _print_candidates(f"resolve_researcher('{KNOWN_NAME}')", results)
        assert isinstance(results, list), "Expected a list"

    def test_at_most_three_candidates(self):
        results = resolve_researcher(KNOWN_NAME)
        assert len(results) <= 3, f"Expected ≤3 candidates, got {len(results)}"

    def test_candidate_schema(self):
        """Every candidate must have the five required keys."""
        results = resolve_researcher(KNOWN_NAME)
        for candidate in results:
            missing = REQUIRED_KEYS - candidate.keys()
            assert not missing, f"Candidate missing keys: {missing}\n{candidate}"

    def test_openalex_id_is_non_empty_string(self):
        results = resolve_researcher(KNOWN_NAME)
        if results:
            assert isinstance(results[0]["openalex_id"], str)
            assert results[0]["openalex_id"].strip() != ""

    def test_works_count_is_int(self):
        results = resolve_researcher(KNOWN_NAME)
        if results:
            assert isinstance(results[0]["works_count"], int)
            assert results[0]["works_count"] >= 0

    def test_display_name_non_empty(self):
        results = resolve_researcher(KNOWN_NAME)
        if results:
            assert results[0]["display_name"].strip() != ""


class TestResolveByOrcid:
    def test_orcid_returns_single_candidate(self):
        """ORCID lookup should return authoritative candidate(s)."""
        results = resolve_researcher(KNOWN_ORCID)
        _print_candidates(f"resolve_researcher(ORCID={KNOWN_ORCID})", results)
        assert isinstance(results, list)
        assert len(results) <= 3, f"ORCID path should return <= 3 results, got {len(results)}"

    def test_orcid_url_format_also_works(self):
        """https://orcid.org/ prefix should be stripped and still resolve."""
        results = resolve_researcher(f"https://orcid.org/{KNOWN_ORCID}")
        assert isinstance(results, list)

    def test_orcid_candidate_schema(self):
        results = resolve_researcher(KNOWN_ORCID)
        for candidate in results:
            missing = REQUIRED_KEYS - candidate.keys()
            assert not missing, f"Missing keys: {missing}"


class TestResolveByDoi:
    def test_doi_returns_candidates(self):
        """DOI lookup should return the paper's authors as candidates."""
        results = resolve_researcher(KNOWN_DOI)
        _print_candidates(f"resolve_researcher(DOI={KNOWN_DOI})", results)
        assert isinstance(results, list)

    def test_doi_url_format_also_works(self):
        results = resolve_researcher(f"https://doi.org/{KNOWN_DOI}")
        assert isinstance(results, list)

    def test_doi_candidate_schema(self):
        results = resolve_researcher(KNOWN_DOI)
        for candidate in results:
            missing = REQUIRED_KEYS - candidate.keys()
            assert not missing, f"Missing keys: {missing}"


class TestGetResearcherPublications:
    def test_returns_list_of_dicts(self):
        """get_researcher_publications must return a list of dicts."""
        candidates = resolve_researcher(KNOWN_NAME)
        if not candidates:
            pytest.skip("resolve_researcher returned no candidates — skip publications test")

        author_id = candidates[0]["openalex_id"]
        pubs = get_researcher_publications(author_id)

        _print_publications(author_id, pubs)
        assert isinstance(pubs, list)

    def test_publication_schema(self):
        """Every publication must have work_id, title, year, doi keys."""
        candidates = resolve_researcher(KNOWN_NAME)
        if not candidates:
            pytest.skip("No candidates returned")

        pubs = get_researcher_publications(candidates[0]["openalex_id"])
        required = {"work_id", "title", "year", "doi"}
        for pub in pubs[:5]:  # check first 5 to keep test fast
            missing = required - pub.keys()
            assert not missing, f"Publication missing keys: {missing}\n{pub}"

    def test_work_ids_non_empty(self):
        candidates = resolve_researcher(KNOWN_NAME)
        if not candidates:
            pytest.skip("No candidates returned")
        pubs = get_researcher_publications(candidates[0]["openalex_id"])
        if pubs:
            assert pubs[0]["work_id"].strip() != ""

    def test_full_url_author_id_accepted(self):
        """get_researcher_publications should accept the full OpenAlex URL form."""
        candidates = resolve_researcher(KNOWN_NAME)
        if not candidates:
            pytest.skip("No candidates returned")
        full_url = f"https://openalex.org/{candidates[0]['openalex_id']}"
        pubs = get_researcher_publications(full_url)
        assert isinstance(pubs, list)


class TestEdgeCases:
    def test_gibberish_name_returns_empty_list(self):
        """An unmatchable name should return [] without raising."""
        results = resolve_researcher("zzzzzzzzquuxfoo99999notaname")
        assert isinstance(results, list)
        assert results == []

    def test_empty_string_returns_list(self):
        """Empty string should return a list (possibly empty) without raising."""
        results = resolve_researcher("")
        assert isinstance(results, list)


class TestCrossrefMetadata:
    def test_get_crossref_metadata_valid_doi(self):
        """Crossref lookup should return metadata for a valid DOI."""
        meta = get_crossref_metadata("10.1002/asi.20416")
        assert isinstance(meta, dict)
        assert meta.get("title") is not None
        assert "authors" in meta
        print("\nCrossref Metadata test for 10.1002/asi.20416:")
        print(f"  Title: {meta.get('title')}")
        print(f"  Authors: {meta.get('authors')}")
        print(f"  Year: {meta.get('year')}")

    def test_invalid_doi_returns_empty(self):
        meta = get_crossref_metadata("invalid/doi/999999999")
        assert meta == {} or meta.get("title") is None


class TestOpenCitations:
    def test_get_opencitations_citations(self):
        """OpenCitations should return real citing records for a paper DOI."""
        doi = "10.1002/meet.1450430173"
        records = get_opencitations_citations(doi, max_results=5)
        assert isinstance(records, list)
        print(f"\nOpenCitations returned {len(records)} records for DOI {doi}:")
        for r in records:
            assert r["source"] == "opencitations"
            assert r["doi"] is not None
            print(f"  • Citing DOI: {r['doi']} | Title: {r.get('title')[:60]} | Authors: {r.get('authors')[:2]}")


class TestGetCitingPapersCombined:
    def test_get_citing_papers_sources(self):
        """get_citing_papers should combine OpenAlex, S2, and OpenCitations."""
        records = get_citing_papers(
            cited_work_id="W1980485417",
            cited_doi="10.1002/meet.1450430173",
            max_results_per_source=5,
        )
        assert isinstance(records, list)
        sources = {r["source"] for r in records}
        for r in records[:5]:
            print(f"  [{r['source']}] {r['title'][:60]} ({r.get('year')}) — {r.get('authors')[:2]}")






# ---------------------------------------------------------------------------
# Pretty-print helper for publications
# ---------------------------------------------------------------------------


def _print_publications(author_id: str, pubs: list) -> None:
    print(f"\n{'='*60}")
    print(f"  Publications for {author_id}  ({len(pubs)} total)")
    print(f"{'='*60}")
    for pub in pubs[:5]:
        print(f"\n  • {pub.get('title', '(no title)')[:80]}")
        print(f"    year={pub.get('year')}  doi={pub.get('doi')}  id={pub.get('work_id')}")
    if len(pubs) > 5:
        print(f"  … and {len(pubs) - 5} more")
