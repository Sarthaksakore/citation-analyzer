"""
tests/test_storage_analysis.py

Unit/integration tests for Layer 5:
  - app/storage.py  — ORM models, upsert_researcher, save_pipeline_run,
                      get_researcher_citations
  - app/analysis.py — compute_statistics, compute_statistics_from_db

Uses an in-memory SQLite database — no disk I/O, no network calls.
"""

from __future__ import annotations

import json
import pytest

from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.storage import (
    Base,
    DBResearcher,
    DBPublication,
    DBCitation,
    get_researcher_citations,
    init_db,
    save_pipeline_run,
    upsert_researcher,
    get_all_researchers,
)
from app.analysis import compute_statistics, compute_statistics_from_db


# ---------------------------------------------------------------------------
# In-memory SQLite engine for tests
# ---------------------------------------------------------------------------

TEST_DB_URL = "sqlite:///:memory:"


@pytest.fixture(scope="function")
def db():
    """Fresh in-memory SQLite DB for every test function."""
    eng = create_engine(TEST_DB_URL, connect_args={"check_same_thread": False})
    Base.metadata.create_all(eng)
    Session = sessionmaker(bind=eng)
    session = Session()
    yield session
    session.close()
    Base.metadata.drop_all(eng)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------

@pytest.fixture
def researcher_dict():
    return {
        "display_name": "Alice Smith",
        "openalex_id": "A1111111111",
        "orcid": "0000-0001-1234-5678",
        "affiliation": "MIT Computer Science",
    }


@pytest.fixture
def publications():
    return [
        {"work_id": "W001", "title": "Deep Learning Survey", "year": 2020, "doi": "10.1000/a"},
        {"work_id": "W002", "title": "Attention Mechanisms", "year": 2021, "doi": "10.1000/b"},
    ]


def _make_citations(work_id: str, n: int = 3) -> list:
    return [
        {
            "title": f"Citing Paper {i} for {work_id}",
            "authors": ["Bob Jones", "Carol White"],
            "year": 2022 + i,
            "doi": f"10.9999/{work_id}-{i}" if i % 2 == 0 else None,
            "venue": "Nature" if i == 0 else None,
            "sources": ["openalex", "crossref"] if i == 0 else ["openalex"],
            "duplicate_count": 2 if i == 0 else 1,
            "source_record_ids": [f"uuid-{work_id}-{i}"],
            "classification": ["SELF", "EXTERNAL", "UNRESOLVED"][i % 3],
            "confidence": [1.0, 1.0, 0.4][i % 3],
            "evidence": "ORCID match" if i % 3 == 0 else "No match",
            "needs_review": i % 3 == 2,
        }
        for i in range(n)
    ]


# ---------------------------------------------------------------------------
# upsert_researcher
# ---------------------------------------------------------------------------

class TestUpsertResearcher:
    def test_creates_new_researcher(self, researcher_dict, db):
        row = upsert_researcher(researcher_dict, db)
        db.flush()
        assert row.id is not None
        assert row.name == "Alice Smith"
        assert row.openalex_id == "A1111111111"
        assert row.orcid == "0000-0001-1234-5678"
        assert row.affiliation == "MIT Computer Science"

    def test_idempotent_same_openalex_id(self, researcher_dict, db):
        r1 = upsert_researcher(researcher_dict, db)
        db.flush()
        r2 = upsert_researcher(researcher_dict, db)
        db.flush()
        assert r1.id == r2.id
        assert db.query(DBResearcher).count() == 1

    def test_updates_affiliation_on_re_run(self, researcher_dict, db):
        upsert_researcher(researcher_dict, db)
        db.flush()
        updated = dict(researcher_dict)
        updated["affiliation"] = "Harvard SEAS"
        row = upsert_researcher(updated, db)
        db.flush()
        assert row.affiliation == "Harvard SEAS"
        assert db.query(DBResearcher).count() == 1

    def test_lookup_by_orcid_when_no_openalex_id(self, researcher_dict, db):
        no_oa = dict(researcher_dict)
        no_oa.pop("openalex_id")
        r1 = upsert_researcher(no_oa, db)
        db.flush()
        r2 = upsert_researcher(no_oa, db)
        db.flush()
        assert r1.id == r2.id

    def test_display_name_fallback(self, db):
        r = upsert_researcher({"display_name": "No ID Researcher"}, db)
        db.flush()
        assert r.name == "No ID Researcher"


# ---------------------------------------------------------------------------
# save_pipeline_run
# ---------------------------------------------------------------------------

class TestSavePipelineRun:
    def test_saves_researcher_and_publications(self, researcher_dict, publications, db):
        citations_by_pub = {
            "W001": _make_citations("W001", 3),
            "W002": _make_citations("W002", 2),
        }
        save_pipeline_run(researcher_dict, publications, citations_by_pub, db)
        db.commit()

        assert db.query(DBResearcher).count() == 1
        assert db.query(DBPublication).count() == 2
        assert db.query(DBCitation).count() == 5

    def test_idempotent_second_run(self, researcher_dict, publications, db):
        """Running twice should not duplicate rows."""
        citations_by_pub = {"W001": _make_citations("W001", 2), "W002": []}
        save_pipeline_run(researcher_dict, publications, citations_by_pub, db)
        db.commit()
        save_pipeline_run(researcher_dict, publications, citations_by_pub, db)
        db.commit()

        assert db.query(DBResearcher).count() == 1
        assert db.query(DBPublication).count() == 2
        assert db.query(DBCitation).count() == 2

    def test_citation_fields_stored_correctly(self, researcher_dict, publications, db):
        cits = [
            {
                "title": "Self Citation Paper",
                "authors": ["Alice Smith", "Bob Jones"],
                "year": 2023,
                "doi": "10.1000/self",
                "venue": "ICML",
                "sources": ["openalex", "semantic_scholar"],
                "duplicate_count": 2,
                "source_record_ids": ["uuid-1", "uuid-2"],
                "classification": "SELF",
                "confidence": 1.0,
                "evidence": "ORCID match",
                "needs_review": False,
            }
        ]
        save_pipeline_run(researcher_dict, publications[:1], {"W001": cits}, db)
        db.commit()

        row = db.query(DBCitation).first()
        assert row.title == "Self Citation Paper"
        assert row.classification == "SELF"
        assert row.confidence == 1.0
        assert row.doi == "10.1000/self"
        assert json.loads(row.sources) == ["openalex", "semantic_scholar"]
        assert json.loads(row.authors) == ["Alice Smith", "Bob Jones"]
        assert row.needs_review == 0
        assert row.duplicate_count == 2

    def test_needs_review_flag_stored(self, researcher_dict, publications, db):
        cits = [
            {
                "title": "Ambiguous Paper",
                "authors": ["Alice Smith"],
                "year": 2022,
                "sources": ["crossref"],
                "classification": "UNRESOLVED",
                "confidence": 0.4,
                "needs_review": True,
            }
        ]
        save_pipeline_run(researcher_dict, publications[:1], {"W001": cits}, db)
        db.commit()
        row = db.query(DBCitation).first()
        assert row.needs_review == 1

    def test_empty_citations_still_saves_researcher(self, researcher_dict, publications, db):
        save_pipeline_run(researcher_dict, publications, {}, db)
        db.commit()
        assert db.query(DBResearcher).count() == 1
        assert db.query(DBPublication).count() == 2
        assert db.query(DBCitation).count() == 0


# ---------------------------------------------------------------------------
# get_researcher_citations
# ---------------------------------------------------------------------------

class TestGetResearcherCitations:
    def _seed(self, researcher_dict, publications, db):
        citations_by_pub = {
            "W001": _make_citations("W001", 3),
            "W002": _make_citations("W002", 1),
        }
        save_pipeline_run(researcher_dict, publications, citations_by_pub, db)
        db.commit()

    def test_returns_correct_structure(self, researcher_dict, publications, db):
        self._seed(researcher_dict, publications, db)
        result = get_researcher_citations("A1111111111", db)
        assert result["researcher"]["display_name"] == "Alice Smith"
        assert len(result["publications"]) == 2
        assert result["total"] == 4

    def test_citations_have_required_keys(self, researcher_dict, publications, db):
        self._seed(researcher_dict, publications, db)
        result = get_researcher_citations("A1111111111", db)
        required = {"title", "authors", "year", "doi", "sources", "classification",
                    "confidence", "needs_review", "retrieved_at", "cited_work_id"}
        for cit in result["citations"]:
            assert required.issubset(cit.keys())

    def test_classification_filter(self, researcher_dict, publications, db):
        self._seed(researcher_dict, publications, db)
        result = get_researcher_citations("A1111111111", db, classification_filter="SELF")
        for cit in result["citations"]:
            assert cit["classification"] == "SELF"

    def test_unknown_researcher_returns_empty(self, db):
        result = get_researcher_citations("A_DOES_NOT_EXIST", db)
        assert result["researcher"] is None
        assert result["citations"] == []
        assert result["total"] == 0

    def test_authors_deserialized_as_list(self, researcher_dict, publications, db):
        self._seed(researcher_dict, publications, db)
        result = get_researcher_citations("A1111111111", db)
        for cit in result["citations"]:
            assert isinstance(cit["authors"], list)

    def test_sources_deserialized_as_list(self, researcher_dict, publications, db):
        self._seed(researcher_dict, publications, db)
        result = get_researcher_citations("A1111111111", db)
        for cit in result["citations"]:
            assert isinstance(cit["sources"], list)

    def test_get_all_researchers(self, researcher_dict, publications, db):
        save_pipeline_run(researcher_dict, publications, {}, db)
        db.commit()
        rows = get_all_researchers(db)
        assert len(rows) == 1
        assert rows[0]["display_name"] == "Alice Smith"
        assert rows[0]["publication_count"] == 2


# ---------------------------------------------------------------------------
# compute_statistics
# ---------------------------------------------------------------------------

class TestComputeStatistics:
    def _citations(self):
        return [
            # SELF, 2 sources (multi-source), dup_count=2
            {"sources": ["openalex", "crossref"], "duplicate_count": 2,
             "classification": "SELF", "confidence": 1.0, "needs_review": False},
            # SELF, 3 sources, dup_count=3
            {"sources": ["openalex", "crossref", "semantic_scholar"], "duplicate_count": 3,
             "classification": "SELF", "confidence": 0.95, "needs_review": False},
            # EXTERNAL, openalex-only
            {"sources": ["openalex"], "duplicate_count": 1,
             "classification": "EXTERNAL", "confidence": 1.0, "needs_review": False},
            # EXTERNAL, crossref-only
            {"sources": ["crossref"], "duplicate_count": 1,
             "classification": "EXTERNAL", "confidence": 1.0, "needs_review": False},
            # UNRESOLVED, name-only
            {"sources": ["semantic_scholar"], "duplicate_count": 1,
             "classification": "UNRESOLVED", "confidence": 0.4, "needs_review": True},
        ]

    def test_total_raw(self):
        stats = compute_statistics(self._citations())
        assert stats["total_raw"] == 2 + 3 + 1 + 1 + 1  # == 8

    def test_deduplicated_count(self):
        stats = compute_statistics(self._citations())
        assert stats["deduplicated_count"] == 5

    def test_classification_counts(self):
        stats = compute_statistics(self._citations())
        assert stats["self_citation_count"] == 2
        assert stats["external_citation_count"] == 2
        assert stats["unresolved_count"] == 1

    def test_source_wise_counts(self):
        stats = compute_statistics(self._citations())
        sw = stats["source_wise_counts"]
        # openalex appears in citations 0,1,2 → 3
        assert sw["openalex"] == 3
        # crossref appears in citations 0,1,3 → 3
        assert sw["crossref"] == 3
        # semantic_scholar appears in citations 1,4 → 2
        assert sw["semantic_scholar"] == 2

    def test_source_overlap(self):
        stats = compute_statistics(self._citations())
        # citations[0] (2 sources) and citations[1] (3 sources) → multi=2
        assert stats["source_overlap"]["multi_source"] == 2
        # citations[2],[3],[4] single source → single=3
        assert stats["source_overlap"]["single_source"] == 3

    def test_citations_per_source_exclusive(self):
        stats = compute_statistics(self._citations())
        excl = stats["citations_per_source_exclusive"]
        assert excl.get("openalex", 0) == 1      # citations[2]
        assert excl.get("crossref", 0) == 1      # citations[3]
        assert excl.get("semantic_scholar", 0) == 1  # citations[4]

    def test_needs_review_count(self):
        stats = compute_statistics(self._citations())
        assert stats["needs_review_count"] == 1

    def test_avg_confidence(self):
        stats = compute_statistics(self._citations())
        # Only SELF and EXTERNAL count: [1.0, 0.95, 1.0, 1.0]
        expected = round((1.0 + 0.95 + 1.0 + 1.0) / 4, 4)
        assert stats["avg_confidence"] == expected

    def test_empty_input(self):
        stats = compute_statistics([])
        assert stats["total_raw"] == 0
        assert stats["deduplicated_count"] == 0
        assert stats["self_citation_count"] == 0
        assert stats["source_overlap"]["multi_source"] == 0

    def test_missing_sources_field_tolerated(self):
        """Citations without 'sources' key must not crash."""
        cits = [{"classification": "EXTERNAL", "confidence": 1.0, "duplicate_count": 1}]
        stats = compute_statistics(cits)
        assert stats["deduplicated_count"] == 1

    def test_missing_duplicate_count_defaults_to_1(self):
        cits = [{"classification": "SELF", "confidence": 1.0, "sources": ["openalex"]}]
        stats = compute_statistics(cits)
        assert stats["total_raw"] == 1

    def test_all_fields_present(self):
        stats = compute_statistics(self._citations())
        required = {
            "total_raw", "deduplicated_count", "self_citation_count",
            "external_citation_count", "unresolved_count", "source_wise_counts",
            "source_overlap", "citations_per_source_exclusive",
            "needs_review_count", "avg_confidence",
        }
        assert required.issubset(stats.keys())


# ---------------------------------------------------------------------------
# compute_statistics_from_db
# ---------------------------------------------------------------------------

class TestComputeStatisticsFromDb:
    def test_returns_correct_stats(self, researcher_dict, publications, db):
        cits = [
            {"title": "P1", "authors": [], "sources": ["openalex", "crossref"],
             "duplicate_count": 2, "classification": "SELF", "confidence": 1.0,
             "needs_review": False},
            {"title": "P2", "authors": [], "sources": ["crossref"],
             "duplicate_count": 1, "classification": "EXTERNAL", "confidence": 1.0,
             "needs_review": False},
        ]
        save_pipeline_run(researcher_dict, publications[:1], {"W001": cits}, db)
        db.commit()

        stats = compute_statistics_from_db("A1111111111", db)
        assert stats["deduplicated_count"] == 2
        assert stats["self_citation_count"] == 1
        assert stats["external_citation_count"] == 1
        assert stats["researcher"]["display_name"] == "Alice Smith"
        assert stats["publications_count"] == 1

    def test_unknown_id_returns_error(self, db):
        result = compute_statistics_from_db("A_NONE", db)
        assert "error" in result

    def test_all_top_level_fields_present(self, researcher_dict, publications, db):
        save_pipeline_run(researcher_dict, publications[:1], {"W001": []}, db)
        db.commit()
        stats = compute_statistics_from_db("A1111111111", db)
        assert "researcher" in stats
        assert "publications_count" in stats
        assert "source_wise_counts" in stats
