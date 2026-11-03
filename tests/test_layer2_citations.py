"""
tests/test_layer2_citations.py

Live integration tests for Layer 2 citation retrieval.
Makes real HTTP calls to OpenAlex, Crossref, and Semantic Scholar.

Run:
    .venv\\Scripts\\python -m pytest tests/test_layer2_citations.py -v -s

Strategy
--------
We cap all citation fetches with max_results_per_source=50 so the tests
finish in <60 s even for highly-cited papers. The per-source schema and
correctness tests only need a representative sample, not exhaustive data.

For the full researcher test we cap to 2 publications and 50 results
per source so the end-to-end flow completes quickly.
"""

from __future__ import annotations

import pprint
import sys
from collections import Counter

import pytest

from app.retrieval import (
    _citing_papers_openalex,
    _citing_papers_semantic_scholar,
    get_all_citations_for_researcher,
    get_citing_papers,
    get_ieee_scopus_wos_citations,
    get_researcher_publications,
    resolve_researcher,
)

# ---------------------------------------------------------------------------
# Constants — moderately-cited paper so tests stay fast with the cap
# ---------------------------------------------------------------------------
# "Citation Analysis: A Comparison of Google Scholar, Scopus, and Web of
#  Science" — Yang & Meho 2007.  ~300 citations; well-indexed across sources.
# This is also directly cited in our project synopsis literature review.
KNOWN_DOI = "10.1002/meet.1450430173"
KNOWN_TITLE = "Citation Analysis: A Comparison of Google Scholar, Scopus, and Web of Science"

# OpenAlex work ID for the above paper (verified)
KNOWN_WORK_ID = "W1980485417"

# Researcher with a modest pub count — good for full-researcher test
RESEARCHER_NAME = "Lokman Meho"

# Max records per source per test call (keeps tests < 30 s each)
MAX_PER_SOURCE = 50

# Required keys in every raw citation record
CITATION_KEYS = {
    "title", "authors", "year", "doi", "venue",
    "source", "cited_work_id", "cited_doi",
}


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _safe_print(text: str) -> None:
    """Print text replacing chars not encodable in the current console encoding."""
    encoded = text.encode(sys.stdout.encoding or "utf-8", errors="replace")
    sys.stdout.buffer.write(encoded + b"\n")
    sys.stdout.buffer.flush()


def _print_source_counts(label: str, records: list) -> None:
    counts = Counter(r.get("source", "?") for r in records)
    _safe_print(f"\n{'='*62}")
    _safe_print(f"  {label}")
    _safe_print(f"  Total records : {len(records)}")
    _safe_print(f"  By source     : {dict(counts)}")
    _safe_print(f"{'='*62}")


def _print_sample(records: list, n: int = 3) -> None:
    for rec in records[:n]:
        title = (rec.get("title") or "")[:80]
        authors = str(rec.get("authors", [])[:2])
        _safe_print(f"\n  * {title}")
        _safe_print(
            f"    authors={authors}  "
            f"year={rec.get('year')}  "
            f"doi={rec.get('doi')}  "
            f"source={rec.get('source')}"
        )


# ---------------------------------------------------------------------------
# Source 1: OpenAlex
# ---------------------------------------------------------------------------

class TestOpenAlexCitations:
    def test_returns_list(self):
        records = _citing_papers_openalex(KNOWN_WORK_ID, KNOWN_DOI, max_results=MAX_PER_SOURCE)
        _print_source_counts("OpenAlex — citing papers", records)
        _print_sample(records)
        assert isinstance(records, list)

    def test_capped_at_max_results(self):
        records = _citing_papers_openalex(KNOWN_WORK_ID, KNOWN_DOI, max_results=MAX_PER_SOURCE)
        assert len(records) <= MAX_PER_SOURCE

    def test_all_source_openalex(self):
        records = _citing_papers_openalex(KNOWN_WORK_ID, KNOWN_DOI, max_results=MAX_PER_SOURCE)
        assert all(r["source"] == "openalex" for r in records), "Non-OpenAlex record found"

    def test_schema(self):
        records = _citing_papers_openalex(KNOWN_WORK_ID, KNOWN_DOI, max_results=MAX_PER_SOURCE)
        for rec in records:
            missing = CITATION_KEYS - rec.keys()
            assert not missing, f"Missing keys: {missing}"

    def test_authors_is_list(self):
        records = _citing_papers_openalex(KNOWN_WORK_ID, KNOWN_DOI, max_results=MAX_PER_SOURCE)
        for rec in records:
            assert isinstance(rec["authors"], list)

    def test_cited_work_id_propagated(self):
        records = _citing_papers_openalex(KNOWN_WORK_ID, KNOWN_DOI, max_results=MAX_PER_SOURCE)
        for rec in records[:5]:
            assert rec["cited_work_id"] == KNOWN_WORK_ID

    def test_cited_doi_propagated(self):
        records = _citing_papers_openalex(KNOWN_WORK_ID, KNOWN_DOI, max_results=MAX_PER_SOURCE)
        for rec in records[:5]:
            assert rec["cited_doi"] == KNOWN_DOI


# ---------------------------------------------------------------------------
# Source 3: Semantic Scholar
# ---------------------------------------------------------------------------

class TestSemanticScholarCitations:
    def test_returns_list(self):
        records = _citing_papers_semantic_scholar(KNOWN_DOI, max_results=MAX_PER_SOURCE)
        _print_source_counts("Semantic Scholar — citing papers", records)
        _print_sample(records)
        assert isinstance(records, list)

    def test_capped_at_max_results(self):
        records = _citing_papers_semantic_scholar(KNOWN_DOI, max_results=MAX_PER_SOURCE)
        assert len(records) <= MAX_PER_SOURCE

    def test_all_source_s2(self):
        records = _citing_papers_semantic_scholar(KNOWN_DOI, max_results=MAX_PER_SOURCE)
        assert all(r["source"] == "semantic_scholar" for r in records)

    def test_schema(self):
        records = _citing_papers_semantic_scholar(KNOWN_DOI, max_results=MAX_PER_SOURCE)
        for rec in records:
            missing = CITATION_KEYS - rec.keys()
            assert not missing, f"Missing keys: {missing}"

    def test_invalid_doi_returns_empty(self):
        records = _citing_papers_semantic_scholar("10.99999/thisdoesnotexist-xyz")
        assert records == []

    def test_cited_doi_propagated(self):
        records = _citing_papers_semantic_scholar(KNOWN_DOI, max_results=MAX_PER_SOURCE)
        for rec in records[:5]:
            assert rec["cited_doi"] == KNOWN_DOI


# ---------------------------------------------------------------------------
# IEEE / Scopus / WoS stub
# ---------------------------------------------------------------------------

class TestIEEEScopusWoSStub:
    def test_returns_empty_list(self):
        result = get_ieee_scopus_wos_citations(cited_doi=KNOWN_DOI)
        assert result == [], "Stub must return [] until institutional keys are configured"

    def test_does_not_raise(self):
        try:
            get_ieee_scopus_wos_citations(cited_doi=KNOWN_DOI, cited_work_id=KNOWN_WORK_ID)
        except Exception as exc:
            pytest.fail(f"Stub raised unexpectedly: {exc}")


# ---------------------------------------------------------------------------
# get_citing_papers — unified entry point
# ---------------------------------------------------------------------------

class TestGetCitingPapers:
    def test_returns_list(self):
        records = get_citing_papers(
            KNOWN_WORK_ID, KNOWN_DOI, KNOWN_TITLE,
            max_results_per_source=MAX_PER_SOURCE,
        )
        _print_source_counts("get_citing_papers — all sources", records)
        _print_sample(records)
        assert isinstance(records, list)

    def test_multiple_sources_present(self):
        """At least 2 distinct sources should appear in combined results."""
        records = get_citing_papers(
            KNOWN_WORK_ID, KNOWN_DOI, KNOWN_TITLE,
            max_results_per_source=MAX_PER_SOURCE,
        )
        sources = {r["source"] for r in records}
        assert len(sources) >= 2, f"Expected ≥2 sources, got: {sources}"

    def test_schema_all_records(self):
        records = get_citing_papers(
            KNOWN_WORK_ID, KNOWN_DOI, KNOWN_TITLE,
            max_results_per_source=MAX_PER_SOURCE,
        )
        for rec in records:
            missing = CITATION_KEYS - rec.keys()
            assert not missing, f"Missing keys: {missing}"

    def test_no_doi_only_runs_openalex(self):
        """With no DOI only OpenAlex runs; must not crash and must return list."""
        records = get_citing_papers(KNOWN_WORK_ID, cited_doi=None, max_results_per_source=10)
        assert isinstance(records, list)
        sources = {r["source"] for r in records}
        assert "openalex" in sources

    def test_source_breakdown_printed(self):
        """Confirm we can compute per-source counts from the result."""
        records = get_citing_papers(
            KNOWN_WORK_ID, KNOWN_DOI, KNOWN_TITLE,
            max_results_per_source=MAX_PER_SOURCE,
        )
        counts = Counter(r["source"] for r in records)
        print(f"\n  Combined source breakdown: {dict(counts)}")
        assert sum(counts.values()) == len(records)


# ---------------------------------------------------------------------------
# get_all_citations_for_researcher — full pipeline (capped)
# ---------------------------------------------------------------------------

class TestGetAllCitationsForResearcher:
    """
    Resolves a real researcher, fetches up to 2 publications, retrieves
    citing papers (capped at MAX_PER_SOURCE per source per publication),
    and prints the per-source breakdown for manual verification.
    """

    @pytest.fixture(scope="class")
    def researcher_result(self):
        candidates = resolve_researcher(RESEARCHER_NAME)
        if not candidates:
            pytest.skip(f"resolve_researcher returned no results for '{RESEARCHER_NAME}'")
        author_id = candidates[0]["openalex_id"]
        _safe_print(f"\n  Resolved: {candidates[0]['display_name']}  ({author_id})")
        result = get_all_citations_for_researcher(
            author_id,
            max_publications=2,
            max_results_per_source=MAX_PER_SOURCE,
        )
        return result

    def test_result_has_required_keys(self, researcher_result):
        assert "publications" in researcher_result
        assert "raw_citations" in researcher_result
        assert "counts_by_source" in researcher_result
        assert "total" in researcher_result

    def test_publications_capped(self, researcher_result):
        assert len(researcher_result["publications"]) <= 2

    def test_total_matches_list_length(self, researcher_result):
        assert researcher_result["total"] == len(researcher_result["raw_citations"])

    def test_per_source_count_table(self, researcher_result):
        """Print the per-source count table for manual verification."""
        counts = researcher_result["counts_by_source"]
        total = researcher_result["total"]
        pubs = researcher_result["publications"]

        _safe_print(f"\n{'='*62}")
        _safe_print(f"  Researcher : {RESEARCHER_NAME}")
        _safe_print(f"  Publications processed : {len(pubs)}")
        for p in pubs:
            _safe_print(f"    - {p.get('title', '')[:60]}  doi={p.get('doi')}")
        _safe_print(f"  Raw citation records   : {total}")
        _safe_print(f"  By source:")
        for src, cnt in sorted(counts.items()):
            pct = (cnt / total * 100) if total else 0.0
            _safe_print(f"    {src:<25} {cnt:>5}  ({pct:.1f}%)")
        _safe_print(f"{'='*62}")

        assert isinstance(counts, dict)

    def test_citation_schema(self, researcher_result):
        for rec in researcher_result["raw_citations"][:30]:
            missing = CITATION_KEYS - rec.keys()
            assert not missing, f"Missing keys: {missing}"
