"""
app/dedup.py — Layer 3: Citation Reconciliation & Deduplication.

Public API
----------
  deduplicate_citations(records)  — primary API; works on plain list[dict]
                                    (output of get_citing_papers / retrieval.py)
  deduplicate(raw_records)        — legacy API; works on RawCitationRecord objects

Matching hierarchy (applied in this exact order):
  1. Exact DOI match       — case-insensitive, stripped                   O(1)
  2. Source-ID match       — same OpenAlex work ID or S2 paper ID         O(1)
  3. Fuzzy bibliographic   — title similarity (w=0.7) + author-set        O(n)
                             similarity (w=0.3), year within ±1,
                             combined threshold ≥ 85

Merge rules (when duplicates are found):
  - Canonical = record with most complete data
    (DOI > longest author list > non-empty venue)
  - ``sources``           — list of every source that reported this citation
  - ``duplicate_count``   — total raw records merged into this canonical one
  - ``source_record_ids`` — list of raw-record identifiers for full traceability
"""

from __future__ import annotations

import logging
import re
import uuid
from functools import lru_cache
from typing import Any, Dict, List, Optional, Set, Tuple

from rapidfuzz import fuzz, process

from app.schemas import RawCitationRecord, SourceDatabase, UniqueCitation

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Constants
# ---------------------------------------------------------------------------

# Weighted fuzzy match threshold (0–100); ≥85 required to merge
FUZZY_THRESHOLD: float = 85.0

# Weights for the combined bibliographic similarity score
TITLE_WEIGHT: float = 0.7
AUTHOR_WEIGHT: float = 0.3

# Year tolerance for fuzzy matching (±1 year allowed)
YEAR_TOLERANCE: int = 1


# ---------------------------------------------------------------------------
# Normalisation helpers (shared by both APIs)
# ---------------------------------------------------------------------------


def _norm_doi(doi: Optional[str]) -> Optional[str]:
    """
    Normalise a DOI: strip URL prefixes, lowercase, trim whitespace.
    Returns None if the result is empty.
    """
    if not doi:
        return None
    doi = doi.strip().lower()
    doi = re.sub(r"^https?://(dx\.)?doi\.org/", "", doi)
    return doi or None


@lru_cache(maxsize=16384)
def _norm_title_cached(title: str) -> str:
    """
    Normalise a title for fuzzy comparison:
    lowercase → remove punctuation → collapse whitespace.
    """
    t = title.lower().strip()
    t = re.sub(r"[^\w\s]", " ", t)
    t = re.sub(r"\s+", " ", t)
    return t.strip()


def _norm_title(title: Optional[str]) -> str:
    return _norm_title_cached(title or "")


@lru_cache(maxsize=16384)
def _norm_author_set_cached(authors: Tuple[str, ...]) -> frozenset[str]:
    """
    Build a normalised set of author last-names for set-similarity comparison.
    Handles both "Given Family" and "Family, Given" formats.
    """
    last_names: Set[str] = set()
    for name in authors:
        name = name.strip()
        if not name:
            continue
        if "," in name:
            last_names.add(name.split(",")[0].strip().lower())
        else:
            parts = name.split()
            if parts:
                last_names.add(parts[-1].lower())
    return frozenset(last_names)


def _norm_author_set(authors: List[str]) -> Set[str]:
    return set(_norm_author_set_cached(tuple(authors or [])))


def _author_set_similarity(a: List[str], b: List[str]) -> float:
    """
    Jaccard similarity between the two sets of normalised last-names.
    Returns a score in [0, 100].
    Falls back to 100 if either list is empty (can't penalise missing data).
    """
    set_a = _norm_author_set(a)
    set_b = _norm_author_set(b)
    if not set_a or not set_b:
        return 100.0
    intersection = len(set_a & set_b)
    union = len(set_a | set_b)
    return (intersection / union) * 100.0


def _combined_similarity(
    title_a: str, authors_a: List[str],
    title_b: str, authors_b: List[str],
) -> float:
    """
    Weighted similarity: title (0.7) + author-set Jaccard (0.3).
    Both components are on a 0–100 scale.
    """
    normalized_a = _norm_title(title_a)
    normalized_b = _norm_title(title_b)
    # token_set_ratio handles provider title truncation or subtitle loss
    # without ignoring author and year safeguards below.
    t_score = max(
        fuzz.token_sort_ratio(normalized_a, normalized_b),
        fuzz.token_set_ratio(normalized_a, normalized_b),
    )
    a_score = _author_set_similarity(authors_a, authors_b)
    return TITLE_WEIGHT * t_score + AUTHOR_WEIGHT * a_score


def _within_year_tolerance(y1: Optional[int], y2: Optional[int]) -> bool:
    """Return True if both years are within YEAR_TOLERANCE of each other, or either is unknown."""
    if y1 is None or y2 is None:
        return True
    return abs(y1 - y2) <= YEAR_TOLERANCE


# ---------------------------------------------------------------------------
# Completeness scoring (used to pick the canonical record when merging)
# ---------------------------------------------------------------------------


def _completeness(rec: Dict[str, Any]) -> int:
    """
    Score a raw record for completeness (higher = more complete).
    Used to select the canonical base record when merging duplicates.
    """
    score = 0
    if _norm_doi(rec.get("doi")):
        score += 100          # DOI is the most valuable field
    authors = rec.get("authors") or []
    score += len(authors) * 5  # more authors = more complete
    if rec.get("venue"):
        score += 10
    if rec.get("year"):
        score += 5
    if rec.get("title"):
        score += 5
    return score


# ---------------------------------------------------------------------------
# Primary public API — operates on plain list[dict]
# ---------------------------------------------------------------------------


def deduplicate_citations(records: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """
    Deduplicate a flat list of raw citation dicts (Layer 2 output).

    Matching hierarchy (in order of priority):
      1. Exact DOI match (case-insensitive, stripped)
      2. Source-specific ID match (same OpenAlex work ID or S2 paper ID
         appearing from two different source queries)
      3. Fuzzy bibliographic match:
           combined = 0.7 × title_similarity + 0.3 × author_set_similarity
           year must be within ±1 year
           threshold ≥ 85

    Merge rules:
      - Canonical record = the raw record with the highest completeness score
        (DOI → author count → venue → year)
      - ``sources``           : list[str] — every source that reported this citation
      - ``duplicate_count``   : int       — number of raw records merged here
      - ``source_record_ids`` : list[str] — internal IDs of every merged raw record

    Parameters
    ----------
    records:
        Raw citation dicts from ``get_citing_papers`` / ``get_all_citations_for_researcher``.
        Expected keys: title, authors, year, doi, venue, source, source_id,
        cited_work_id, cited_doi.  Missing keys are tolerated.

    Returns
    -------
    list[dict]
        Deduplicated records.  Each dict has all original keys from the canonical
        record, plus the three provenance fields above.
    """
    # Stamp every record with a stable internal ID for traceability
    stamped: List[Dict[str, Any]] = []
    for rec in records:
        r = dict(rec)
        r.setdefault("_raw_id", str(uuid.uuid4()))
        stamped.append(r)

    # Canonical records (one per unique citing paper)
    canonical: List[Dict[str, Any]] = []

    # Fast-lookup indices
    doi_index: Dict[str, int] = {}      # norm_doi  → index in canonical
    sid_index: Dict[str, int] = {}      # "source::source_id" → index
    title_choices: List[str] = []       # normalised titles, indexed like canonical
    fuzzy_indices: List[int] = []       # canonical records without a known DOI

    for rec in stamped:
        matched_idx = _find_match_dict(
            rec, canonical, doi_index, sid_index, title_choices, fuzzy_indices
        )

        if matched_idx is not None:
            _merge_dict(canonical[matched_idx], rec)
            title_choices[matched_idx] = _norm_title(
                canonical[matched_idx].get("title")
            )
            # If the merge backfilled a DOI onto canonical, update doi_index so
            # subsequent records for the same paper can be found in O(1)
            new_doi = _norm_doi(canonical[matched_idx].get("doi"))
            if new_doi and new_doi not in doi_index:
                doi_index[new_doi] = matched_idx
        else:
            idx = len(canonical)
            entry = _make_canonical(rec)
            canonical.append(entry)
            title_choices.append(_norm_title(entry.get("title")))
            # Keep DOI-bearing records in the fuzzy candidate pool too. A
            # later source may omit its DOI; the fuzzy matcher still protects
            # records with conflicting DOIs.
            fuzzy_indices.append(idx)

            # Update indices
            nd = _norm_doi(rec.get("doi"))
            if nd:
                doi_index[nd] = idx

            src = rec.get("source", "")
            sid = rec.get("source_id", "")
            if src and sid:
                sid_index[f"{src}::{sid}"] = idx

    logger.info(
        "deduplicate_citations: %d raw records → %d unique citations",
        len(records),
        len(canonical),
    )
    return canonical


# ---------------------------------------------------------------------------
# dict-based matching & merging helpers
# ---------------------------------------------------------------------------


def _find_match_dict(
    rec: Dict[str, Any],
    canonical: List[Dict[str, Any]],
    doi_index: Dict[str, int],
    sid_index: Dict[str, int],
    title_choices: Optional[List[str]] = None,
    fuzzy_indices: Optional[List[int]] = None,
) -> Optional[int]:
    """
    Find the canonical record that matches ``rec``.
    Returns the index into ``canonical``, or None.
    """

    # --- Priority 1: DOI ---
    nd = _norm_doi(rec.get("doi"))
    if nd and nd in doi_index:
        return doi_index[nd]

    # --- Priority 2: Source-specific ID ---
    src = rec.get("source", "")
    sid = rec.get("source_id", "")
    if src and sid:
        key = f"{src}::{sid}"
        if key in sid_index:
            return sid_index[key]

    # --- Priority 3: Fuzzy bibliographic ---
    # Run as final fallback when both index lookups miss.
    # _fuzzy_match_dict itself will skip canonicals that have a *different*
    # known DOI, preventing false merges between distinct papers.
    return _fuzzy_match_dict(rec, canonical, title_choices, fuzzy_indices)


def _fuzzy_match_dict(
    rec: Dict[str, Any],
    canonical: List[Dict[str, Any]],
    title_choices: Optional[List[str]] = None,
    fuzzy_indices: Optional[List[int]] = None,
) -> Optional[int]:
    """
    Scan canonical records for a fuzzy title+author match.
    Year must be within ±YEAR_TOLERANCE.

    Skip any canonical that already has a different known DOI — two records
    with distinct DOIs are definitively different papers regardless of title
    similarity.  Only merge when the canonical has no DOI, or both share the
    same DOI (the latter is already handled by doi_index, but defensive).

    Returns the index of the best match ≥ FUZZY_THRESHOLD, or None.
    """
    rec_doi = _norm_doi(rec.get("doi"))
    title = rec.get("title") or ""
    authors = rec.get("authors") or []
    year = rec.get("year")

    best_score: float = 0.0
    best_idx: Optional[int] = None

    candidate_pool = (
        fuzzy_indices
        if fuzzy_indices is not None
        else range(len(canonical))
    )
    if not candidate_pool:
        return None

    # RapidFuzz performs the expensive title comparison in native code and
    # returns only candidates that can possibly reach the combined threshold.
    # A title score below this value cannot reach 85 even with perfect authors.
    if title_choices is not None:
        candidate_pool = list(candidate_pool)
        candidate_titles = [title_choices[idx] for idx in candidate_pool]
        title_candidates = (
            process.extract(
                _norm_title(title),
                candidate_titles,
                scorer=fuzz.token_set_ratio,
                score_cutoff=(FUZZY_THRESHOLD - AUTHOR_WEIGHT * 100) / TITLE_WEIGHT,
                limit=None,
            )
            if title
            else [(choice, 100.0, idx) for idx, choice in enumerate(candidate_titles)]
        )
        candidate_indices = (candidate_pool[match[2]] for match in title_candidates)
    else:
        candidate_indices = range(len(canonical))

    for idx in candidate_indices:
        can = canonical[idx]
        # If both records have DOIs and they differ → definitively different papers
        can_doi = _norm_doi(can.get("doi"))
        if rec_doi and can_doi and rec_doi != can_doi:
            continue

        if not _within_year_tolerance(year, can.get("year")):
            continue

        score = _combined_similarity(
            title, authors,
            can.get("title") or "", can.get("authors") or [],
        )

        if score >= FUZZY_THRESHOLD and score > best_score:
            best_score = score
            best_idx = idx

    return best_idx


def _make_canonical(rec: Dict[str, Any]) -> Dict[str, Any]:
    """
    Create a new canonical entry from a raw record, adding provenance fields.
    """
    entry = dict(rec)
    entry["sources"] = [rec.get("source")] if rec.get("source") else []
    entry["source_record_ids"] = [rec["_raw_id"]]
    entry["duplicate_count"] = 1
    return entry


def _merge_dict(canonical: Dict[str, Any], rec: Dict[str, Any]) -> None:
    """
    Merge a duplicate raw record into an existing canonical entry.

    Merge rules:
    - If the incoming record is MORE complete, replace the canonical base fields
      (title, authors, doi, year, venue, publisher_url) with it.
    - Always extend sources, source_record_ids, and increment duplicate_count.
    - Always prefer DOI > more authors > non-empty venue.
    """
    can_score = _completeness(canonical)
    rec_score = _completeness(rec)

    # Append provenance regardless of which record wins
    src = rec.get("source")
    if src and src not in canonical["sources"]:
        canonical["sources"].append(src)
    canonical["source_record_ids"].append(rec["_raw_id"])
    canonical["duplicate_count"] += 1

    # Update DOI if canonical is missing it
    rec_doi = _norm_doi(rec.get("doi"))
    if rec_doi and not _norm_doi(canonical.get("doi")):
        canonical["doi"] = rec_doi

    # Prefer the record with the longer author list
    if len(rec.get("authors") or []) > len(canonical.get("authors") or []):
        canonical["authors"] = rec.get("authors")

    # Fill in missing venue / year / publisher_url
    if not canonical.get("venue") and rec.get("venue"):
        canonical["venue"] = rec["venue"]
    if not canonical.get("year") and rec.get("year"):
        canonical["year"] = rec["year"]
    if not canonical.get("publisher_url") and rec.get("publisher_url"):
        canonical["publisher_url"] = rec["publisher_url"]

    # If the incoming record is significantly more complete and has a DOI,
    # replace the title/venue with the better record's values too
    if rec_score > can_score and rec_doi:
        if rec.get("title"):
            canonical["title"] = rec["title"]
        if rec.get("venue"):
            canonical["venue"] = rec["venue"]


# ---------------------------------------------------------------------------
# Summary helper
# ---------------------------------------------------------------------------


def dedup_summary(
    raw: List[Dict[str, Any]],
    deduped: List[Dict[str, Any]],
) -> Dict[str, Any]:
    """
    Return a summary dict comparing raw vs deduplicated counts.

    Useful for Layer 5 statistics and the /deduplicate API endpoint.
    """
    multi_source = [d for d in deduped if len(d.get("sources", [])) > 1]
    single_source_by_src: Dict[str, int] = {}
    for d in deduped:
        srcs = d.get("sources", [])
        if len(srcs) == 1:
            single_source_by_src[srcs[0]] = single_source_by_src.get(srcs[0], 0) + 1

    return {
        "raw_count": len(raw),
        "unique_count": len(deduped),
        "duplicates_removed": len(raw) - len(deduped),
        "cross_source_citations": len(multi_source),
        "source_exclusive": single_source_by_src,
    }


# ---------------------------------------------------------------------------
# Legacy API — operates on RawCitationRecord / UniqueCitation Pydantic models
# (kept for backward compatibility with existing tests and schemas)
# ---------------------------------------------------------------------------


def _normalise_doi(doi: Optional[str]) -> Optional[str]:
    """Alias kept for backward compat with existing callers."""
    return _norm_doi(doi)


def _normalise_title(title: Optional[str]) -> str:
    return _norm_title(title)


def _first_author_last_name(authors: List[str]) -> str:
    """Extract the last name of the first listed author."""
    if not authors:
        return ""
    first = authors[0].strip()
    if "," in first:
        return first.split(",")[0].strip().lower()
    parts = first.split()
    return parts[-1].lower() if parts else ""


def deduplicate(raw_records: List[RawCitationRecord]) -> List[UniqueCitation]:
    """
    Deduplicate a flat list of RawCitationRecords into unique citing papers.
    Legacy API — kept for existing schema-based callers and tests.
    """
    unique: List[UniqueCitation] = []
    doi_index: Dict[str, int] = {}
    db_id_index: Dict[str, int] = {}

    for record in raw_records:
        matched_idx = _find_match_schema(record, unique, doi_index, db_id_index)

        if matched_idx is not None:
            _merge_into_schema(unique[matched_idx], record)
        else:
            idx = len(unique)
            uc = _to_unique_schema(record)
            unique.append(uc)

            nd = _normalise_doi(record.doi)
            if nd:
                doi_index[nd] = idx
            if record.source_db and record.source_db_id:
                db_id_index[f"{record.source_db}::{record.source_db_id}"] = idx

    logger.info("deduplicate: %d raw → %d unique", len(raw_records), len(unique))
    return unique


def _find_match_schema(
    record: RawCitationRecord,
    unique: List[UniqueCitation],
    doi_index: Dict[str, int],
    db_id_index: Dict[str, int],
) -> Optional[int]:
    nd = _normalise_doi(record.doi)
    if nd and nd in doi_index:
        return doi_index[nd]
    if record.source_db and record.source_db_id:
        key = f"{record.source_db}::{record.source_db_id}"
        if key in db_id_index:
            return db_id_index[key]
    if not nd:
        return _fuzzy_search_schema(record, unique)
    return None


def _fuzzy_search_schema(
    record: RawCitationRecord,
    unique: List[UniqueCitation],
) -> Optional[int]:
    rec_title = _normalise_title(record.title)
    rec_author = _first_author_last_name(record.authors)
    rec_year = record.year
    best_score: float = 0.0
    best_idx: Optional[int] = None

    for idx, uc in enumerate(unique):
        if rec_year and uc.year and abs(rec_year - uc.year) > YEAR_TOLERANCE:
            continue
        uc_title = _normalise_title(uc.title)
        uc_author = _first_author_last_name(uc.authors)
        t_score = fuzz.token_sort_ratio(rec_title, uc_title)
        a_score = fuzz.ratio(rec_author, uc_author) if rec_author and uc_author else 100.0
        combined = TITLE_WEIGHT * t_score + AUTHOR_WEIGHT * a_score
        if combined >= FUZZY_THRESHOLD and combined > best_score:
            best_score = combined
            best_idx = idx

    return best_idx


def _to_unique_schema(record: RawCitationRecord) -> UniqueCitation:
    return UniqueCitation(
        title=record.title,
        authors=record.authors,
        doi=_normalise_doi(record.doi),
        year=record.year,
        venue=record.venue,
        sources=[record.source_db],
        source_db_ids={record.source_db: record.source_db_id} if record.source_db_id else {},
        publisher_url=record.publisher_url,
        cited_doi=_normalise_doi(record.cited_doi),
    )


def _merge_into_schema(uc: UniqueCitation, record: RawCitationRecord) -> None:
    if record.source_db not in uc.sources:
        uc.sources.append(record.source_db)
    if record.source_db_id:
        uc.source_db_ids[record.source_db] = record.source_db_id
    if not uc.doi and record.doi:
        uc.doi = _normalise_doi(record.doi)
    if not uc.year and record.year:
        uc.year = record.year
    if not uc.venue and record.venue:
        uc.venue = record.venue
    if not uc.publisher_url and record.publisher_url:
        uc.publisher_url = record.publisher_url
    if len(record.authors) > len(uc.authors):
        uc.authors = record.authors
