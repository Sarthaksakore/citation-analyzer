"""
app/analysis.py — Layer 5: Citation statistics & source-overlap analysis.

Public API (dict-based — works with plain list[dict] from Layers 2-4)
----------------------------------------------------------------------
  compute_statistics(citations)
      Returns a statistics dict from a flat list of classified citation dicts.

  compute_statistics_from_db(openalex_id, db)
      Re-computes statistics from stored results for a researcher without
      calling any external APIs.

Legacy schema-based API (kept for backward compatibility)
---------------------------------------------------------
  compute_citation_summary(researcher, raw_records, classified_citations)
"""

from __future__ import annotations

import logging
from collections import Counter, defaultdict
from typing import Any, Dict, List, Optional

from app.schemas import (
    CitationClassification,
    CitationSummary,
    ClassifiedCitation,
    RawCitationRecord,
    ResearcherProfile,
    SourceDatabase,
    SourceStats,
)

logger = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Primary public API — operates on plain list[dict]
# ---------------------------------------------------------------------------


def compute_statistics(citations: List[Dict[str, Any]]) -> Dict[str, Any]:
    """
    Compute citation statistics from a flat list of classified citation dicts.

    Intended to receive the output of ``classify_all()`` (Layer 4), which
    extends each dict with ``classification``, ``confidence``, ``sources``,
    and ``duplicate_count``.

    Parameters
    ----------
    citations:
        List of citation dicts.  Each dict should have at minimum:
          - ``sources``        : list[str]  (from deduplicate_citations)
          - ``duplicate_count``: int
          - ``classification`` : "SELF" | "EXTERNAL" | "UNRESOLVED"
          - ``confidence``     : float

    Returns
    -------
    dict with keys:
        total_raw               : int — sum of all duplicate_count values
        deduplicated_count      : int — len(citations) after deduplication
        self_citation_count     : int
        external_citation_count : int
        unresolved_count        : int
        source_wise_counts      : dict[str, int] — how many unique citations
                                  each source contributed to (a citation
                                  counts for every source in its 'sources' list)
        source_overlap          : dict with keys
                                    multi_source   : int (found in ≥2 sources)
                                    single_source  : int (found in exactly 1)
        citations_per_source_exclusive : dict[str, int] — unique citations
                                         only found in that one source
        needs_review_count      : int
        avg_confidence          : float
    """
    total_raw = sum(int(c.get("duplicate_count", 1)) for c in citations)
    deduplicated_count = len(citations)

    # Classification counts
    classification_counter: Counter = Counter(
        (c.get("classification") or "UNRESOLVED") for c in citations
    )
    self_count = classification_counter.get("SELF", 0)
    external_count = classification_counter.get("EXTERNAL", 0)
    unresolved_count = classification_counter.get("UNRESOLVED", 0)

    # Per-source unique citation counts (a citation with sources=["openalex","crossref"]
    # adds 1 to both openalex and crossref)
    source_wise: Counter = Counter()
    exclusive: Counter = Counter()
    multi_source_count = 0
    single_source_count = 0

    for cit in citations:
        srcs = [s for s in (cit.get("sources") or []) if s]
        n = len(srcs)

        for src in srcs:
            source_wise[src] += 1

        if n >= 2:
            multi_source_count += 1
        elif n == 1:
            single_source_count += 1
            exclusive[srcs[0]] += 1

    # needs_review count
    needs_review_count = sum(
        1 for c in citations if c.get("needs_review")
    )

    # Average confidence (over classified citations only)
    confidences = [
        c.get("confidence", 0.0) for c in citations
        if c.get("classification") in ("SELF", "EXTERNAL")
    ]
    avg_confidence = (sum(confidences) / len(confidences)) if confidences else 0.0

    stats = {
        "total_raw": total_raw,
        "deduplicated_count": deduplicated_count,
        "self_citation_count": self_count,
        "external_citation_count": external_count,
        "unresolved_count": unresolved_count,
        "source_wise_counts": dict(source_wise),
        "source_overlap": {
            "multi_source": multi_source_count,
            "single_source": single_source_count,
        },
        "citations_per_source_exclusive": dict(exclusive),
        "needs_review_count": needs_review_count,
        "avg_confidence": round(avg_confidence, 4),
    }

    logger.info(
        "compute_statistics: raw=%d deduped=%d SELF=%d EXTERNAL=%d UNRESOLVED=%d "
        "multi_source=%d needs_review=%d",
        total_raw, deduplicated_count,
        self_count, external_count, unresolved_count,
        multi_source_count, needs_review_count,
    )
    return stats


# ---------------------------------------------------------------------------
# DB-backed re-query (no API calls)
# ---------------------------------------------------------------------------


def compute_statistics_from_db(
    openalex_id: str,
    db: Any,  # Session — typed as Any to avoid circular import
) -> Dict[str, Any]:
    """
    Re-compute statistics for a researcher from stored DB results.

    No external API calls are made.  Uses ``get_researcher_citations()``
    from ``storage.py`` to load the stored classified citations.

    Parameters
    ----------
    openalex_id:
        The researcher's OpenAlex author ID.
    db:
        Active SQLAlchemy session.

    Returns
    -------
    dict — same structure as ``compute_statistics()`` plus:
        researcher : dict — researcher metadata from DB
        publications_count : int
    """
    # Import here to avoid circular imports between storage ↔ analysis
    from app.storage import get_researcher_citations

    stored = get_researcher_citations(openalex_id, db)
    if stored["researcher"] is None:
        return {
            "error": f"No stored data for researcher {openalex_id}",
            "openalex_id": openalex_id,
        }

    stats = compute_statistics(stored["citations"])
    stats["researcher"] = stored["researcher"]
    stats["publications_count"] = len(stored["publications"])
    stats["openalex_author_id"] = openalex_id
    stats["researcher_name"] = (stored["researcher"] or {}).get("display_name")
    stats["publications_processed"] = len(stored["publications"])
    stats["raw_source_counts"] = stats.get("source_wise_counts", {})

    logger.info(
        "compute_statistics_from_db: %s → %d citations analysed from DB",
        openalex_id, stored["total"],
    )
    return stats


# ---------------------------------------------------------------------------
# Legacy schema-based API (kept for backward compatibility)
# ---------------------------------------------------------------------------


def compute_citation_summary(
    researcher: ResearcherProfile,
    raw_records: List[RawCitationRecord],
    classified_citations: List[ClassifiedCitation],
) -> CitationSummary:
    """
    Compute a full CitationSummary from Pydantic schema objects.
    Legacy API — kept for existing tests and schema-based callers.

    Parameters
    ----------
    researcher:
        Resolved researcher profile (Layer 1).
    raw_records:
        All raw citation records before deduplication (Layer 2).
    classified_citations:
        Deduplicated and classified citations (Layers 3+4).

    Returns
    -------
    CitationSummary
    """
    # Raw counts per source
    raw_counts: Counter = Counter(r.source_db for r in raw_records)

    # Unique counts per source
    unique_per_source: Dict[SourceDatabase, int] = defaultdict(int)
    for cc in classified_citations:
        for src in cc.citation.sources:
            unique_per_source[src] += 1

    source_stats: List[SourceStats] = []
    for src in SourceDatabase:
        if raw_counts[src] > 0 or unique_per_source[src] > 0:
            source_stats.append(
                SourceStats(
                    source=src,
                    raw_count=raw_counts[src],
                    unique_count=unique_per_source[src],
                )
            )

    # Classification counts
    classification_counts: Counter = Counter(
        cc.classification for cc in classified_citations
    )

    # Cross-source vs exclusive
    cross_source_count = 0
    exclusive: Dict[str, int] = defaultdict(int)
    for cc in classified_citations:
        n_sources = len(cc.citation.sources)
        if n_sources >= 2:
            cross_source_count += 1
        elif n_sources == 1:
            exclusive[str(cc.citation.sources[0])] += 1

    summary = CitationSummary(
        researcher=researcher,
        total_raw_citations=len(raw_records),
        total_unique_citations=len(classified_citations),
        self_citations=classification_counts[CitationClassification.SELF],
        external_citations=classification_counts[CitationClassification.EXTERNAL],
        unresolved_citations=classification_counts[CitationClassification.UNRESOLVED],
        source_stats=source_stats,
        cross_source_citations=cross_source_count,
        source_exclusive_citations=dict(exclusive),
    )

    logger.info(
        "compute_citation_summary: %d raw → %d unique | self=%d ext=%d unres=%d",
        summary.total_raw_citations,
        summary.total_unique_citations,
        summary.self_citations,
        summary.external_citations,
        summary.unresolved_citations,
    )
    return summary
