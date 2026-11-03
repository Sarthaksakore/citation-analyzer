"""
app/disambiguation.py — Layer 4: Author Disambiguation & Self-Citation Detection.

Public API (dict-based — works with Layer 2/3 plain dict output)
----------------------------------------------------------------
  resolve_author_identity(author_name, citing_paper, target_researcher)
      → {matched, confidence, evidence, level, needs_review}

  classify_citation(citing_paper, target_researcher)
      → citing_paper dict with "classification", "confidence", "evidence",
        "needs_review" fields added

  classify_all(citing_papers, target_researcher)
      → list of classified citing_paper dicts

Identity hierarchy (per project synopsis):
  1. ORCID match                        confidence 1.00
  2. Database author-ID match            confidence 0.95
  3. Name + affiliation overlap          confidence 0.80
  4. Name + shared co-author             confidence 0.65
  5. Name only (ambiguous)               confidence 0.40
  No name match                          matched=False, confidence 1.00

Safety / ambiguity-flag logic:
  confidence < 0.60 → needs_review=True (Claim/Reject queue)
  Two authors share identical name with no corroborating signal → flagged

Classification rules:
  confidence >= 0.70 and matched          → SELF
  matched is False (confidently external) → EXTERNAL
  everything else                         → UNRESOLVED

Legacy schema-based API (kept for backward compat):
  classify_citation_schema(citation, researcher, extra_author_metadata)
  classify_all_citations(citations, researcher)
"""

from __future__ import annotations

import logging
import re
from typing import Any, Dict, List, Optional, Tuple

from rapidfuzz import fuzz

from app.schemas import (
    CitationClassification,
    ClassifiedCitation,
    ResearcherProfile,
    UniqueCitation,
)

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Confidence thresholds
# ---------------------------------------------------------------------------

# Identity hierarchy confidence scores (exact values specified in synopsis)
CONFIDENCE = {
    "orcid": 1.00,
    "db_author_id": 0.95,
    "affiliation": 0.80,
    "co_author": 0.65,
    "name_only": 0.40,
    "no_match": 0.00,
}

# Classification decision thresholds
SELF_THRESHOLD: float = 0.70       # matched + confidence >= this → SELF
REVIEW_THRESHOLD: float = 0.60     # confidence < this → flag needs_review
EXTERNAL_CONFIDENCE: float = 1.00  # confidently external

# Name fuzzy similarity floor to count as a name match at all
NAME_MATCH_FLOOR: float = 85.0

# Affiliation fuzzy similarity floor
AFFILIATION_MATCH_FLOOR: float = 75.0


# ---------------------------------------------------------------------------
# Normalisation helpers
# ---------------------------------------------------------------------------


def _norm_name(name: str) -> str:
    """Lowercase, collapse whitespace, strip punctuation."""
    name = name.lower().strip()
    name = re.sub(r"[^\w\s]", " ", name)
    return re.sub(r"\s+", " ", name).strip()


def _norm_orcid(orcid: Optional[str]) -> Optional[str]:
    """Strip URL prefix, lowercase."""
    if not orcid:
        return None
    return re.sub(r"^https?://orcid\.org/", "", orcid.strip().lower())


def _name_similarity(a: str, b: str) -> float:
    """Token-sort ratio on normalised names. Returns 0–100."""
    return fuzz.token_sort_ratio(_norm_name(a), _norm_name(b))


def _abbreviated_name_match(target: str, candidate: str) -> bool:
    """Recognize initials plus a shared substantive name token."""
    target_tokens = _norm_name(target).split()
    candidate_tokens = _norm_name(candidate).split()
    substantive = {
        token for token in target_tokens
        if len(token) >= 4 and token in candidate_tokens
    }
    target_initials = {token[0] for token in target_tokens if token}
    candidate_initials = {token[0] for token in candidate_tokens if token}
    return bool(substantive) and len(target_initials & candidate_initials) >= 2


def _affiliation_similarity(a: str, b: str) -> float:
    """Partial ratio for institution names (handles abbreviations)."""
    return fuzz.partial_ratio(a.lower().strip(), b.lower().strip())


def _extract_author_orcid(author_entry: Any) -> Optional[str]:
    """
    Pull an ORCID from various formats:
    - plain string "0000-0002-…"
    - dict with key "orcid"
    """
    if isinstance(author_entry, str):
        return _norm_orcid(author_entry)
    if isinstance(author_entry, dict):
        return _norm_orcid(author_entry.get("orcid"))
    return None


def _extract_author_db_id(author_entry: Any, id_key: str = "openalex_id") -> Optional[str]:
    """Pull a database author ID from a dict."""
    if isinstance(author_entry, dict):
        return author_entry.get(id_key) or author_entry.get("author_id")
    return None


# ---------------------------------------------------------------------------
# Layer 4 — primary public API (dict-based)
# ---------------------------------------------------------------------------


def resolve_author_identity(
    author_name: str,
    citing_paper: Dict[str, Any],
    target_researcher: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Determine whether ``author_name`` in ``citing_paper`` is the same person
    as ``target_researcher``, using the six-level identity hierarchy.

    Parameters
    ----------
    author_name:
        A single author string from the citing paper's ``authors`` list.
    citing_paper:
        Raw or deduplicated citation dict.  Optional enrichment keys:
        ``author_orcids``  : list[str] — ORCIDs aligned with ``authors``
        ``author_ids``     : list[dict] — DB IDs aligned with ``authors``
        ``author_affiliations`` : list[str] — affiliations aligned with ``authors``
    target_researcher:
        Dict returned by ``resolve_researcher()`` or equivalent.  Keys:
        ``display_name``, ``orcid``, ``openalex_id``, ``affiliation``,
        ``known_coauthors`` (list[str]), ``publication_titles`` (list[str]).

    Returns
    -------
    dict with keys:
        matched         : bool   — True if a name-level match was found
        confidence      : float  — in [0.0, 1.00]
        level           : str    — which hierarchy level fired
        evidence        : str    — human-readable evidence description
        needs_review    : bool   — True when confidence < REVIEW_THRESHOLD
    """
    # Locate this author's index in the author list so we can find
    # aligned metadata (orcids, db_ids, affiliations)
    authors = citing_paper.get("authors") or []
    try:
        author_idx = authors.index(author_name)
    except ValueError:
        # Author name not found in list — do best-effort fuzzy alignment
        author_idx = None

    def _get_aligned(field: str) -> Optional[Any]:
        lst = citing_paper.get(field) or []
        if author_idx is not None and author_idx < len(lst):
            return lst[author_idx]
        return None

    # -----------------------------------------------------------------------
    # Priority 1: Exact ORCID match
    # -----------------------------------------------------------------------
    target_orcid = _norm_orcid(target_researcher.get("orcid"))
    author_orcid = _norm_orcid(_get_aligned("author_orcids"))

    if target_orcid and author_orcid and target_orcid == author_orcid:
        return _result(
            matched=True,
            confidence=CONFIDENCE["orcid"],
            level="orcid",
            evidence=f"Exact ORCID match: {author_orcid}",
        )

    # -----------------------------------------------------------------------
    # Priority 2: Database author-ID match (OpenAlex, S2, …)
    # -----------------------------------------------------------------------
    target_db_id = target_researcher.get("openalex_id") or target_researcher.get("author_id")
    author_db_id = _extract_author_db_id(_get_aligned("author_ids"))

    if target_db_id and author_db_id and str(target_db_id) == str(author_db_id):
        return _result(
            matched=True,
            confidence=CONFIDENCE["db_author_id"],
            level="db_author_id",
            evidence=f"Database author-ID match: {author_db_id}",
        )

    # -----------------------------------------------------------------------
    # Name similarity — required for all remaining levels
    # -----------------------------------------------------------------------
    target_name = target_researcher.get("display_name") or ""
    name_score = _name_similarity(target_name, author_name)
    source_names = citing_paper.get("sources") or [citing_paper.get("source")]
    target_tokens = _norm_name(target_name).split()
    candidate_tokens = _norm_name(author_name).split()
    has_distinctive_full_name = (
        _norm_orcid(target_researcher.get("orcid"))
        and len(target_tokens) >= 3
        and len(candidate_tokens) >= 3
        and name_score >= 95.0
    )
    is_abbreviated_name = (
        _norm_name(target_name) != _norm_name(author_name)
        and _abbreviated_name_match(target_name, author_name)
    )
    if _norm_orcid(target_researcher.get("orcid")) and (
        is_abbreviated_name or has_distinctive_full_name
    ):
        name_score = max(name_score, NAME_MATCH_FLOOR)

    if name_score < NAME_MATCH_FLOOR:
        # Definitively a different person — no name match at all
        return _result(
            matched=False,
            confidence=EXTERNAL_CONFIDENCE,
            level="no_match",
            evidence=(
                f"Name '{author_name}' does not match target "
                f"'{target_name}' (score={name_score:.1f} < {NAME_MATCH_FLOOR})"
            ),
        )

    # From here: name matches (score >= NAME_MATCH_FLOOR)

    # -----------------------------------------------------------------------
    # Priority 3: Name + institutional affiliation overlap
    # -----------------------------------------------------------------------
    target_affiliation = target_researcher.get("affiliation") or ""
    author_affiliation = _get_aligned("author_affiliations") or ""

    if target_affiliation and author_affiliation:
        aff_score = _affiliation_similarity(target_affiliation, author_affiliation)
        if aff_score >= AFFILIATION_MATCH_FLOOR:
            return _result(
                matched=True,
                confidence=CONFIDENCE["affiliation"],
                level="affiliation",
                evidence=(
                    f"Name match (score={name_score:.1f}) + "
                    f"affiliation overlap '{author_affiliation}' ≈ '{target_affiliation}' "
                    f"(aff_score={aff_score:.1f})"
                ),
            )

    # -----------------------------------------------------------------------
    # Priority 4: Name + shared co-author
    # -----------------------------------------------------------------------
    known_coauthors: List[str] = target_researcher.get("known_coauthors") or []
    paper_authors: List[str] = authors

    if known_coauthors and paper_authors:
        shared = _find_shared_coauthor(paper_authors, known_coauthors, author_name)
        if shared:
            return _result(
                matched=True,
                confidence=CONFIDENCE["co_author"],
                level="co_author",
                evidence=(
                    f"Name match (score={name_score:.1f}) + "
                    f"shared co-author '{shared}'"
                ),
            )

    # Semantic Scholar often returns reordered or lightly corrupted author
    # strings without stable author IDs. A near-exact name is strong evidence
    # when the selected researcher is independently verified by ORCID.
    if _norm_orcid(target_researcher.get("orcid")) and (
        is_abbreviated_name or has_distinctive_full_name
    ):
        return _result(
            matched=True,
            confidence=0.75,
            level="verified_name",
            evidence=(
                f"Verified researcher ORCID + distinctive name match "
                f"(score={name_score:.1f})"
            ),
        )

    # -----------------------------------------------------------------------
    # Priority 5: Name-only match — ambiguous, flag for review
    # -----------------------------------------------------------------------
    return _result(
        matched=True,
        confidence=CONFIDENCE["name_only"],
        level="name_only",
        evidence=(
            f"Name match only (score={name_score:.1f}); "
            "no ORCID, DB-ID, affiliation, or co-author corroboration"
        ),
    )


def classify_citation(
    citing_paper: Dict[str, Any],
    target_researcher: Dict[str, Any],
) -> Dict[str, Any]:
    """
    Add classification metadata to a single citing_paper dict.

    Iterates over all authors, runs ``resolve_author_identity`` for each,
    picks the highest-confidence author match, and applies the classification
    rules:
      - confidence >= SELF_THRESHOLD and matched → SELF
      - matched is False (confidently external)  → EXTERNAL
      - otherwise                                → UNRESOLVED

    Safety / ambiguity rule: when the best match is name-only with
    confidence < REVIEW_THRESHOLD, ``needs_review`` is set True instead
    of auto-deciding.

    Returns
    -------
    The same dict with these keys added (in-place + returned):
        classification  : str   — "SELF" | "EXTERNAL" | "UNRESOLVED"
        confidence      : float
        evidence        : str   — from the best-matching author
        needs_review    : bool
        author_resolutions : list[dict] — full resolve result per author
    """
    authors = citing_paper.get("authors") or []

    if not authors:
        result = citing_paper.copy()
        result.update({
            "classification": "UNRESOLVED",
            "confidence": 0.0,
            "evidence": "Cannot classify citation because no author metadata was returned",
            "needs_review": True,
            "author_resolutions": [],
        })
        return result

    resolutions = [
        resolve_author_identity(name, citing_paper, target_researcher)
        for name in authors
    ]

    # Pick the author that gives the highest confidence *and* is matched
    matched_resolutions = [r for r in resolutions if r["matched"]]
    best = max(
        resolutions,
        key=lambda r: r["confidence"] if r["matched"] else 0.0,
        default=resolutions[0],
    )

    # Classification rules
    if matched_resolutions and best["confidence"] >= SELF_THRESHOLD:
        classification = "SELF"
    elif not matched_resolutions:
        # All authors confidently non-matching
        classification = "EXTERNAL"
    else:
        classification = "UNRESOLVED"

    # Ambiguity / safety flag
    needs_review = bool(
        best["needs_review"]
        or (
            matched_resolutions
            and best["confidence"] < REVIEW_THRESHOLD
            and classification != "EXTERNAL"
        )
    )

    result = dict(citing_paper)
    result.update({
        "classification": classification,
        "confidence": round(best["confidence"], 4),
        "evidence": best["evidence"],
        "needs_review": needs_review,
        "author_resolutions": resolutions,
    })
    return result


def classify_all(
    citing_papers: List[Dict[str, Any]],
    target_researcher: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """
    Classify every citing paper against the target researcher.

    Parameters
    ----------
    citing_papers:
        List of raw or deduplicated citation dicts (Layer 2/3 output).
    target_researcher:
        Dict from ``resolve_researcher()`` (keys: display_name, orcid,
        openalex_id, affiliation, known_coauthors, publication_titles).

    Returns
    -------
    list[dict] — same order as input, each dict extended with classification
    metadata fields (classification, confidence, evidence, needs_review,
    author_resolutions).
    """
    classified = [classify_citation(p, target_researcher) for p in citing_papers]

    counts: Dict[str, int] = {"SELF": 0, "EXTERNAL": 0, "UNRESOLVED": 0}
    review_count = 0
    for c in classified:
        counts[c["classification"]] = counts.get(c["classification"], 0) + 1
        if c.get("needs_review"):
            review_count += 1

    logger.info(
        "classify_all: %d papers → SELF=%d EXTERNAL=%d UNRESOLVED=%d needs_review=%d",
        len(classified),
        counts["SELF"],
        counts["EXTERNAL"],
        counts["UNRESOLVED"],
        review_count,
    )
    return classified


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _result(
    *,
    matched: bool,
    confidence: float,
    level: str,
    evidence: str,
) -> Dict[str, Any]:
    """Build a standard resolve_author_identity return dict."""
    return {
        "matched": matched,
        "confidence": round(confidence, 4),
        "level": level,
        "evidence": evidence,
        "needs_review": matched and confidence < REVIEW_THRESHOLD,
    }


def _find_shared_coauthor(
    paper_authors: List[str],
    known_coauthors: List[str],
    exclude_name: str,
) -> Optional[str]:
    """
    Return the first co-author in paper_authors (excluding the candidate
    itself) that fuzzy-matches any name in known_coauthors.
    Returns None if no shared co-author is found.
    """
    for author in paper_authors:
        if _name_similarity(author, exclude_name) >= NAME_MATCH_FLOOR:
            continue  # skip the candidate themselves
        for known in known_coauthors:
            if (
                _name_similarity(author, known) >= NAME_MATCH_FLOOR
                or _abbreviated_name_match(author, known)
                or _abbreviated_name_match(known, author)
            ):
                return known
    return None


# ---------------------------------------------------------------------------
# Legacy schema-based API (kept for backward compatibility)
# ---------------------------------------------------------------------------

# Thresholds used by the legacy API
_LEGACY_NAME_THRESHOLD: float = 88.0
_LEGACY_SELF_THRESHOLD: float = 0.65
_LEGACY_EXTERNAL_THRESHOLD: float = 0.60

_LEGACY_SIGNAL_WEIGHTS: Dict[str, float] = {
    "orcid": 1.00,
    "db_author_id": 0.90,
    "affiliation": 0.60,
    "co_author": 0.50,
    "pub_history": 0.45,
    "name_similarity": 0.30,
}


def _normalise_name(name: str) -> str:
    return " ".join(name.lower().split())


def _name_similarity_legacy(a: str, b: str) -> float:
    return fuzz.token_sort_ratio(_normalise_name(a), _normalise_name(b))


def classify_citation_schema(
    citation: UniqueCitation,
    researcher: ResearcherProfile,
    *,
    extra_author_metadata: Optional[List[Dict[str, Any]]] = None,
) -> ClassifiedCitation:
    """
    Legacy API — classify a UniqueCitation using ResearcherProfile objects.
    Kept for backward compatibility with existing tests and schemas.
    """
    evidence: Dict[str, Any] = {}
    total_confidence: float = 0.0
    is_self_signal = False

    author_meta_list = extra_author_metadata or [{} for _ in citation.authors]

    for author_name, author_meta in zip(citation.authors, author_meta_list):
        author_signals: Dict[str, Any] = {}
        author_confidence: float = 0.0

        # ORCID
        if researcher.orcid:
            citing_orcid = author_meta.get("orcid")
            if citing_orcid and citing_orcid == researcher.orcid:
                author_signals["orcid"] = True
                author_confidence = max(author_confidence, _LEGACY_SIGNAL_WEIGHTS["orcid"])

        # DB author ID
        if researcher.openalex_author_id:
            if author_meta.get("openalex_author_id") == researcher.openalex_author_id:
                author_signals["db_author_id"] = True
                author_confidence = max(author_confidence, _LEGACY_SIGNAL_WEIGHTS["db_author_id"])

        # Affiliation
        if researcher.affiliation and author_meta.get("affiliation"):
            aff_score = _name_similarity_legacy(
                researcher.affiliation, author_meta["affiliation"]
            )
            if aff_score >= _LEGACY_NAME_THRESHOLD:
                author_signals["affiliation"] = author_meta["affiliation"]
                author_confidence = max(author_confidence, _LEGACY_SIGNAL_WEIGHTS["affiliation"])

        # Name similarity
        name_score = _name_similarity_legacy(researcher.display_name, author_name)
        if name_score >= _LEGACY_NAME_THRESHOLD:
            scaled = _LEGACY_SIGNAL_WEIGHTS["name_similarity"] * (name_score / 100.0)
            author_signals["name_similarity"] = round(name_score, 1)
            author_confidence = max(author_confidence, scaled)

        if author_confidence > 0:
            evidence[author_name] = author_signals
            total_confidence = max(total_confidence, author_confidence)
            if author_confidence >= _LEGACY_SELF_THRESHOLD:
                is_self_signal = True

    if is_self_signal and total_confidence >= _LEGACY_SELF_THRESHOLD:
        classification = CitationClassification.SELF
    elif total_confidence == 0.0 or total_confidence < _LEGACY_EXTERNAL_THRESHOLD:
        classification = CitationClassification.EXTERNAL
    else:
        classification = CitationClassification.UNRESOLVED

    citation.classification = classification
    citation.classification_confidence = round(total_confidence, 4)
    citation.classification_evidence = evidence

    return ClassifiedCitation(
        citation=citation,
        classification=classification,
        confidence=round(total_confidence, 4),
        evidence=evidence,
    )


def classify_all_citations(
    citations: List[UniqueCitation],
    researcher: ResearcherProfile,
) -> List[ClassifiedCitation]:
    """Legacy batch API."""
    results: List[ClassifiedCitation] = []
    counts = {c: 0 for c in CitationClassification}
    for citation in citations:
        classified = classify_citation_schema(citation, researcher)
        results.append(classified)
        counts[classified.classification] += 1
    logger.info(
        "classify_all_citations: SELF=%d EXTERNAL=%d UNRESOLVED=%d",
        counts[CitationClassification.SELF],
        counts[CitationClassification.EXTERNAL],
        counts[CitationClassification.UNRESOLVED],
    )
    return results
