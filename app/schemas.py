"""
app/schemas.py — Pydantic request/response models for Citation Analyzer.

These schemas are the contract between the API layer and its consumers
(frontend, evaluation scripts, other services).  All data entering or
leaving the API must pass through one of these models.
"""

from __future__ import annotations

from enum import Enum
from typing import List, Optional

from pydantic import BaseModel, Field, HttpUrl


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class CitationClassification(str, Enum):
    """Layer-4 classification for each citing paper."""

    SELF = "SELF"
    EXTERNAL = "EXTERNAL"
    UNRESOLVED = "UNRESOLVED"


class SourceDatabase(str, Enum):
    """Supported scholarly data sources."""

    OPENALEX = "openalex"
    SEMANTIC_SCHOLAR = "semantic_scholar"
    OPENCITATIONS = "opencitations"
    CROSSREF = "crossref"
    GOOGLE_SCHOLAR = "google_scholar"
    IEEE = "ieee"
    SCOPUS = "scopus"
    WOS = "wos"


# ---------------------------------------------------------------------------
# Researcher / publication identification (Layer 1)
# ---------------------------------------------------------------------------


class ResearcherLookupRequest(BaseModel):
    """Input for resolving a researcher identity (Layer 1)."""

    name: Optional[str] = Field(None, description="Full name of the researcher")
    orcid: Optional[str] = Field(None, description="ORCID identifier (e.g. 0000-0002-1825-0097)")
    doi: Optional[str] = Field(None, description="DOI of a known publication")
    affiliation: Optional[str] = Field(None, description="Institution or department")

    model_config = {"json_schema_extra": {"example": {"name": "Vedant Shelke", "orcid": "0000-0000-0000-0000"}}}


class PublicationRecord(BaseModel):
    """A single publication belonging to the target researcher."""

    title: str
    doi: Optional[str] = None
    year: Optional[int] = None
    venue: Optional[str] = None
    openalex_id: Optional[str] = None
    semantic_scholar_id: Optional[str] = None
    authors: List[str] = Field(default_factory=list)


class ResearcherProfile(BaseModel):
    """Resolved researcher identity + their publication list."""

    display_name: str
    orcid: Optional[str] = None
    openalex_author_id: Optional[str] = None
    affiliation: Optional[str] = None
    publications: List[PublicationRecord] = Field(default_factory=list)


# ---------------------------------------------------------------------------
# Raw citation retrieval (Layer 2)
# ---------------------------------------------------------------------------


class RawCitationRecord(BaseModel):
    """A single citing paper as returned by one source database."""

    title: str
    authors: List[str] = Field(default_factory=list)
    doi: Optional[str] = None
    year: Optional[int] = None
    venue: Optional[str] = None
    source_db: SourceDatabase
    source_db_id: Optional[str] = None  # e.g. OpenAlex work ID
    publisher_url: Optional[str] = None
    cited_doi: Optional[str] = None  # DOI of the paper being cited


# ---------------------------------------------------------------------------
# Deduplication (Layer 3)
# ---------------------------------------------------------------------------


class UniqueCitation(BaseModel):
    """
    A deduplicated citing paper that may have been found in multiple
    source databases.  The ``sources`` list retains provenance.
    """

    title: str
    authors: List[str] = Field(default_factory=list)
    doi: Optional[str] = None
    year: Optional[int] = None
    venue: Optional[str] = None
    sources: List[SourceDatabase] = Field(default_factory=list)
    source_db_ids: dict = Field(default_factory=dict)  # {source_db: id}
    publisher_url: Optional[str] = None
    cited_doi: Optional[str] = None

    # Classification (populated in Layer 4)
    classification: CitationClassification = CitationClassification.UNRESOLVED
    classification_confidence: float = 0.0
    classification_evidence: dict = Field(default_factory=dict)


# ---------------------------------------------------------------------------
# Disambiguation / self-citation (Layer 4)
# ---------------------------------------------------------------------------


class ClassifiedCitation(BaseModel):
    """A unique citation after author-disambiguation classification."""

    citation: UniqueCitation
    classification: CitationClassification
    confidence: float = Field(ge=0.0, le=1.0, description="Confidence in the classification [0, 1]")
    evidence: dict = Field(default_factory=dict, description="Evidence signals used for classification")


# ---------------------------------------------------------------------------
# Analysis (Layer 5)
# ---------------------------------------------------------------------------


class SourceStats(BaseModel):
    """Per-source citation statistics."""

    source: SourceDatabase
    raw_count: int
    unique_count: int


class CitationSummary(BaseModel):
    """Aggregated statistics for a researcher's citation profile."""

    researcher: ResearcherProfile
    total_raw_citations: int
    total_unique_citations: int
    self_citations: int
    external_citations: int
    unresolved_citations: int
    source_stats: List[SourceStats] = Field(default_factory=list)
    cross_source_citations: int = Field(0, description="Citations found in ≥2 sources")
    source_exclusive_citations: dict = Field(
        default_factory=dict,
        description="Source → count of citations found only in that source",
    )


# ---------------------------------------------------------------------------
# Evaluation (Layer 6)
# ---------------------------------------------------------------------------


class EvaluationResult(BaseModel):
    """Precision / Recall / F1 / Accuracy for a pipeline component."""

    component: str  # e.g. "deduplication", "self_citation", "researcher_id"
    precision: float
    recall: float
    f1_score: float
    accuracy: float
    support: int  # number of samples in gold set
