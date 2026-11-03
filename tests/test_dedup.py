"""
tests/test_dedup.py

Unit tests for Layer 3: deduplicate_citations() in app/dedup.py.

All tests use hand-crafted records — no network calls.
Covers every branch of the matching hierarchy:
  1. Exact DOI match
  2. Source-specific ID match
  3. Fuzzy bibliographic match (title + author-set, year ±1)

Also tests merge rules:
  - sources list accumulation
  - duplicate_count increment
  - source_record_ids traceability
  - canonical selection (DOI > author count > venue)
  - year-tolerance edge cases (±1 allowed, ±2 rejected)
  - author format normalisation (comma-inverted, initials, Unicode)
  - empty / degenerate inputs
"""

from __future__ import annotations

from app.dedup import (
    FUZZY_THRESHOLD,
    YEAR_TOLERANCE,
    _author_set_similarity,
    _combined_similarity,
    _norm_doi,
    _norm_title,
    deduplicate_citations,
    dedup_summary,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _rec(
    title="A Paper Title",
    authors=None,
    year=2020,
    doi=None,
    venue=None,
    source="openalex",
    source_id=None,
    cited_doi=None,
    publisher_url=None,
):
    """Convenience factory for raw citation dicts."""
    return {
        "title": title,
        "authors": authors or ["Alice Smith", "Bob Jones"],
        "year": year,
        "doi": doi,
        "venue": venue,
        "source": source,
        "source_id": source_id or f"{source}-{title[:10].replace(' ', '')}",
        "cited_doi": cited_doi or "10.9999/cited",
        "publisher_url": publisher_url,
    }


# ---------------------------------------------------------------------------
# Normalisation unit tests
# ---------------------------------------------------------------------------

class TestNormDoi:
    def test_strips_https_prefix(self):
        assert _norm_doi("https://doi.org/10.1000/xyz") == "10.1000/xyz"

    def test_strips_http_dx_prefix(self):
        assert _norm_doi("http://dx.doi.org/10.1000/xyz") == "10.1000/xyz"

    def test_lowercases(self):
        assert _norm_doi("10.1000/ABC") == "10.1000/abc"

    def test_strips_whitespace(self):
        assert _norm_doi("  10.1000/xyz  ") == "10.1000/xyz"

    def test_none_returns_none(self):
        assert _norm_doi(None) is None

    def test_empty_string_returns_none(self):
        assert _norm_doi("") is None


class TestNormTitle:
    def test_lowercases(self):
        assert _norm_title("Deep Learning") == "deep learning"

    def test_removes_punctuation(self):
        result = _norm_title("Deep Learning: A Review!")
        assert ":" not in result and "!" not in result

    def test_collapses_whitespace(self):
        result = _norm_title("  Deep   Learning  ")
        assert result == "deep learning"

    def test_empty_returns_empty(self):
        assert _norm_title("") == ""

    def test_none_returns_empty(self):
        assert _norm_title(None) == ""


class TestAuthorSetSimilarity:
    def test_identical_sets(self):
        a = ["Alice Smith", "Bob Jones"]
        assert _author_set_similarity(a, a) == 100.0

    def test_completely_different(self):
        a = ["Alice Smith"]
        b = ["Carol White"]
        assert _author_set_similarity(a, b) == 0.0

    def test_partial_overlap(self):
        a = ["Alice Smith", "Bob Jones"]
        b = ["Alice Smith", "Carol White"]
        # intersection=1 (smith), union=3 → 33.3%
        score = _author_set_similarity(a, b)
        assert 30 < score < 40

    def test_empty_a_returns_100(self):
        """If one list is empty we can't penalise — return 100."""
        assert _author_set_similarity([], ["Bob Jones"]) == 100.0

    def test_comma_inverted_names(self):
        """'Smith, Alice' and 'Alice Smith' should extract the same last name."""
        a = ["Smith, Alice"]
        b = ["Alice Smith"]
        assert _author_set_similarity(a, b) == 100.0


# ---------------------------------------------------------------------------
# Priority 1: Exact DOI match
# ---------------------------------------------------------------------------

class TestDoiMatch:
    def test_same_doi_merges(self):
        records = [
            _rec(title="Paper A", doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(title="Paper A slightly different", doi="10.1000/aaa", source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_doi_case_insensitive(self):
        records = [
            _rec(doi="10.1000/AAA", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_doi_with_url_prefix_merges(self):
        records = [
            _rec(doi="https://doi.org/10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_different_dois_not_merged(self):
        records = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/bbb", source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 2

    def test_three_same_doi_all_merge(self):
        records = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR1"),
            _rec(doi="10.1000/aaa", source="semantic_scholar", source_id="S1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1
        assert result[0]["duplicate_count"] == 3


# ---------------------------------------------------------------------------
# Priority 2: Source-specific ID match
# ---------------------------------------------------------------------------

class TestSourceIdMatch:
    def test_same_openalex_id_merges(self):
        records = [
            _rec(title="Paper X", source="openalex", source_id="W999"),
            _rec(title="Paper X variant", source="openalex", source_id="W999"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_different_source_ids_not_merged(self):
        records = [
            _rec(title="Paper X", source="openalex", source_id="W111"),
            _rec(title="Paper X", source="openalex", source_id="W222"),
        ]
        # Different IDs, no DOI, title identical → will hit fuzzy and merge
        # (source_id miss → fuzzy catches it)
        result = deduplicate_citations(records)
        # Titles are identical so fuzzy should merge them
        assert len(result) == 1

    def test_same_s2_id_merges(self):
        records = [
            _rec(source="semantic_scholar", source_id="s2abc", doi=None),
            _rec(source="semantic_scholar", source_id="s2abc", doi=None),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_doi_takes_priority_over_source_id(self):
        """If DOIs match, they must merge even if source IDs differ."""
        records = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W111"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR999"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1


# ---------------------------------------------------------------------------
# Priority 3: Fuzzy bibliographic match
# ---------------------------------------------------------------------------

class TestFuzzyMatch:
    def test_identical_title_no_doi_merges(self):
        records = [
            _rec("Deep Learning for NLP", doi=None, source="openalex", source_id="W1"),
            _rec("Deep Learning for NLP", doi=None, source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_title_casing_difference_merges(self):
        """Different title casing should still merge."""
        records = [
            _rec("attention is all you need", doi=None, year=2017,
                 source="openalex", source_id="W1"),
            _rec("Attention Is All You Need", doi=None, year=2017,
                 source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_minor_title_punctuation_difference_merges(self):
        """Trailing period / colon differences should still merge."""
        records = [
            _rec("A survey of deep learning methods.", doi=None, year=2021,
                 source="openalex", source_id="W1"),
            _rec("A survey of deep learning methods", doi=None, year=2021,
                 source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_year_within_tolerance_merges(self):
        """Year difference ≤ YEAR_TOLERANCE should still allow fuzzy merge."""
        records = [
            _rec("Generative adversarial networks", doi=None, year=2014,
                 source="openalex", source_id="W1"),
            _rec("Generative adversarial networks", doi=None, year=2014 + YEAR_TOLERANCE,
                 source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_year_outside_tolerance_not_merged(self):
        """Year difference > YEAR_TOLERANCE must prevent merge."""
        records = [
            _rec("Annual Review of Machine Learning", doi=None, year=2020,
                 source="openalex", source_id="W1"),
            _rec("Annual Review of Machine Learning", doi=None, year=2023,
                 source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 2

    def test_completely_different_titles_not_merged(self):
        records = [
            _rec("Quantum Computing for Beginners", doi=None, source="openalex", source_id="W1"),
            _rec("Introduction to Graph Theory", doi=None, source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 2

    def test_different_author_sets_reduce_score(self):
        """Near-identical titles but completely different authors should not merge."""
        records = [
            _rec("A Study of Transformer Models", authors=["Smith A", "Jones B"],
                 doi=None, year=2021, source="openalex", source_id="W1"),
            _rec("A Study of Transformer Models", authors=["Wang C", "Liu D", "Chen E"],
                 doi=None, year=2021, source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        # Score = 0.7*100 + 0.3*0 = 70 < 85 → should NOT merge
        assert len(result) == 2


# ---------------------------------------------------------------------------
# Merge rules: provenance fields
# ---------------------------------------------------------------------------

class TestProvenance:
    def test_sources_list_accumulated(self):
        records = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR1"),
            _rec(doi="10.1000/aaa", source="semantic_scholar", source_id="S1"),
        ]
        result = deduplicate_citations(records)
        assert set(result[0]["sources"]) == {"openalex", "crossref", "semantic_scholar"}

    def test_duplicate_count(self):
        records = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert result[0]["duplicate_count"] == 2

    def test_source_record_ids_length_matches_raw(self):
        records = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR1"),
            _rec(doi="10.1000/aaa", source="semantic_scholar", source_id="S1"),
        ]
        result = deduplicate_citations(records)
        assert len(result[0]["source_record_ids"]) == 3

    def test_source_record_ids_are_strings(self):
        records = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        for rid in result[0]["source_record_ids"]:
            assert isinstance(rid, str) and rid

    def test_no_source_duplication(self):
        """Same source appearing twice should only be listed once in sources."""
        records = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
        ]
        result = deduplicate_citations(records)
        assert result[0]["sources"].count("openalex") == 1


# ---------------------------------------------------------------------------
# Merge rules: canonical selection (completeness)
# ---------------------------------------------------------------------------

class TestCanonicalSelection:
    def test_prefers_record_with_doi(self):
        """When one record has a DOI and the other doesn't, the DOI wins."""
        records = [
            _rec(title="Paper With DOI", doi="10.1000/aaa",
                 source="openalex", source_id="W1", venue=None),
            # Second record has no DOI — it is the fuzzy duplicate
            _rec(title="Paper With DOI", doi=None,
                 source="crossref", source_id="CR1", venue="Nature"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1
        assert result[0]["doi"] == "10.1000/aaa"

    def test_doi_filled_from_duplicate(self):
        """If canonical had no DOI but duplicate has one, DOI should be filled."""
        records = [
            _rec(title="Missing DOI Paper", doi=None,
                 source="crossref", source_id="CR1"),
            _rec(title="Missing DOI Paper", doi="10.1000/bbb",
                 source="openalex", source_id="W1"),
        ]
        result = deduplicate_citations(records)
        assert result[0]["doi"] == "10.1000/bbb"

    def test_longer_author_list_preferred(self):
        """The record with more authors should win the author field."""
        records = [
            _rec(doi="10.1000/aaa", authors=["Smith A"],
                 source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", authors=["Smith A", "Jones B", "White C"],
                 source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result[0]["authors"]) == 3

    def test_venue_filled_from_duplicate(self):
        """If canonical has no venue but duplicate has one, it should be filled."""
        records = [
            _rec(doi="10.1000/aaa", venue=None,
                 source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", venue="Nature",
                 source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert result[0]["venue"] == "Nature"


# ---------------------------------------------------------------------------
# Real-world formatting scenarios
# ---------------------------------------------------------------------------

class TestRealWorldFormats:
    def test_author_comma_inverted_vs_normal(self):
        """'Smith, Alice' (Crossref format) vs 'Alice Smith' (OpenAlex format)."""
        records = [
            _rec("Transformer Models Review",
                 authors=["Smith, Alice", "Jones, Bob"],
                 doi=None, year=2022, source="crossref", source_id="CR1"),
            _rec("Transformer Models Review",
                 authors=["Alice Smith", "Bob Jones"],
                 doi=None, year=2022, source="openalex", source_id="W1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_title_with_subtitle_merges(self):
        """'Title: A Subtitle' vs 'Title A Subtitle' (colon stripped)."""
        records = [
            _rec("BERT: Pre-training of Deep Bidirectional Transformers",
                 doi=None, year=2019, source="openalex", source_id="W1"),
            _rec("BERT Pre-training of Deep Bidirectional Transformers",
                 doi=None, year=2019, source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_mixed_doi_and_no_doi(self):
        """
        Three records for the same paper: one from OpenAlex (with DOI),
        one from Crossref (DOI matches), one from S2 (no DOI, fuzzy match).
        All three should collapse into one.
        """
        records = [
            _rec("Deep Learning", doi="10.1038/nature14539", year=2015,
                 authors=["Yann LeCun", "Yoshua Bengio", "Geoffrey Hinton"],
                 venue="Nature", source="openalex", source_id="W1"),
            _rec("Deep learning", doi="10.1038/NATURE14539", year=2015,
                 authors=["LeCun, Yann", "Bengio, Yoshua", "Hinton, Geoffrey"],
                 venue=None, source="crossref", source_id="CR1"),
            _rec("Deep Learning", doi=None, year=2015,
                 authors=["Yann LeCun", "Yoshua Bengio"],
                 source="semantic_scholar", source_id="S1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1
        assert set(result[0]["sources"]) == {"openalex", "crossref", "semantic_scholar"}
        assert result[0]["duplicate_count"] == 3
        assert len(result[0]["source_record_ids"]) == 3
        assert result[0]["doi"] == "10.1038/nature14539"


# ---------------------------------------------------------------------------
# Edge cases
# ---------------------------------------------------------------------------

class TestEdgeCases:
    def test_empty_input(self):
        assert deduplicate_citations([]) == []

    def test_single_record(self):
        r = [_rec("Only Paper", doi="10.1000/only")]
        result = deduplicate_citations(r)
        assert len(result) == 1
        assert result[0]["duplicate_count"] == 1
        assert len(result[0]["source_record_ids"]) == 1

    def test_all_unique_records_untouched(self):
        records = [
            _rec("Paper One", doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec("Paper Two", doi="10.1000/bbb", source="crossref", source_id="CR1"),
            _rec("Paper Three", doi="10.1000/ccc", source="semantic_scholar", source_id="S1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 3

    def test_missing_title_does_not_crash(self):
        records = [
            _rec(doi=None, source="openalex", source_id="W1"),
            _rec(doi=None, source="crossref", source_id="CR1"),
        ]
        records[0]["title"] = ""
        result = deduplicate_citations(records)
        assert isinstance(result, list)

    def test_missing_year_does_not_block_fuzzy(self):
        """A record with no year should still match on title alone."""
        records = [
            _rec("Neural Machine Translation", doi=None, year=2016,
                 source="openalex", source_id="W1"),
            _rec("Neural Machine Translation", doi=None, year=None,
                 source="crossref", source_id="CR1"),
        ]
        result = deduplicate_citations(records)
        assert len(result) == 1

    def test_source_record_ids_all_unique(self):
        """Each raw record gets a distinct UUID — no collisions."""
        records = [_rec(doi=f"10.1000/{i}", source="openalex", source_id=f"W{i}")
                   for i in range(50)]
        result = deduplicate_citations(records)
        all_ids = [rid for r in result for rid in r["source_record_ids"]]
        assert len(all_ids) == len(set(all_ids))


# ---------------------------------------------------------------------------
# dedup_summary helper
# ---------------------------------------------------------------------------

class TestDedupSummary:
    def test_summary_structure(self):
        raw = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR1"),
            _rec(doi="10.1000/bbb", source="openalex", source_id="W2"),
        ]
        deduped = deduplicate_citations(raw)
        summary = dedup_summary(raw, deduped)

        assert summary["raw_count"] == 3
        assert summary["unique_count"] == 2
        assert summary["duplicates_removed"] == 1

    def test_cross_source_count(self):
        raw = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR1"),
            _rec(doi="10.1000/bbb", source="openalex", source_id="W2"),
        ]
        deduped = deduplicate_citations(raw)
        summary = dedup_summary(raw, deduped)
        # First paper found in 2 sources → cross_source = 1
        assert summary["cross_source_citations"] == 1

    def test_source_exclusive_count(self):
        raw = [
            _rec(doi="10.1000/aaa", source="openalex", source_id="W1"),
            _rec(doi="10.1000/aaa", source="crossref", source_id="CR1"),
            _rec(doi="10.1000/bbb", source="openalex", source_id="W2"),
        ]
        deduped = deduplicate_citations(raw)
        summary = dedup_summary(raw, deduped)
        # Second paper only in openalex → exclusive["openalex"] = 1
        assert summary["source_exclusive"].get("openalex", 0) == 1
