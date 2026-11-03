"""
app/main.py — FastAPI entrypoint for Citation Analyzer.

Endpoints
---------
  GET  /health                              — liveness probe
  GET  /researchers                         — list all stored researchers
  POST /researcher/resolve                  — Layer 1: resolve researcher candidates
  POST /researcher/{researcher_id}/analyze  — full pipeline (Layers 2-5)
  GET  /researcher/{researcher_id}/citations— cached results, no API calls
  POST /citations/{citation_id}/review      — human Claim/Reject for UNRESOLVED

  GET  /statistics/{researcher_id}          — stats from DB + integrity checksum
  POST /verify-integrity                    — verify HMAC-SHA256 checksum

Security
--------
  - All string inputs sanitised with security.sanitize_input
  - /analyze rate-limited per CITATION_SEARCH_RATE_LIMIT
  - Results signed with HMAC-SHA256 (security.sign_summary)
  - Single failed source never crashes /analyze (per-source try/except)
"""

from __future__ import annotations

import logging
import os
import traceback
from contextlib import asynccontextmanager
from typing import Any, Dict, List, Literal, Optional

from dotenv import load_dotenv
from fastapi import Depends, FastAPI, HTTPException, Path, Query, Request
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from pydantic import BaseModel, Field
from slowapi import _rate_limit_exceeded_handler
from slowapi.errors import RateLimitExceeded
from slowapi.middleware import SlowAPIMiddleware
from sqlalchemy.orm import Session

from app.analysis import compute_statistics, compute_statistics_from_db
from app.dedup import deduplicate_citations, dedup_summary
from app.disambiguation import classify_all
from app.google_scholar import get_google_scholar_profile
from app.retrieval import get_all_citations_for_researcher, resolve_researcher
from app.security import (
    CITATION_SEARCH_RATE_LIMIT,
    IntegrityError,
    limiter,
    sanitize_doi,
    sanitize_input,
    sanitize_orcid,
    sign_summary,
    verify_summary,
)
from app.storage import (
    DBCitation,
    SessionLocal,
    get_db,
    get_all_researchers,
    get_researcher_citations,
    delete_researcher,
    delete_all_researchers,
    init_db,
    save_pipeline_run,
)

load_dotenv()
logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Request / Response bodies (Pydantic)
# ---------------------------------------------------------------------------


class ResolveRequest(BaseModel):
    """Body for POST /researcher/resolve."""
    query: str = Field(..., min_length=1, description="Author name, ORCID (0000-...), or DOI")

    model_config = {
        "json_schema_extra": {
            "example": {"query": "Lokman Meho"}
        }
    }


class AnalyzeRequest(BaseModel):
    """Body for POST /researcher/{researcher_id}/analyze."""
    max_publications: Optional[int] = Field(
        None, ge=1,
        description="Optional maximum number of publications to process; null analyzes all available works"
    )
    max_results_per_source: Optional[int] = Field(
        None, ge=1,
        description="Optional maximum citations per source; null retrieves all available records"
    )
    persist: bool = Field(
        True,
        description="Persist results to the database after analysis"
    )

    model_config = {
        "json_schema_extra": {
            "example": {"max_publications": None, "max_results_per_source": None, "persist": True}
        }
    }


class ReviewRequest(BaseModel):
    """Body for POST /citations/{citation_id}/review."""
    decision: Literal["claim", "reject"] = Field(
        ...,
        description="'claim' → mark as SELF; 'reject' → mark as EXTERNAL"
    )
    reviewer_note: Optional[str] = Field(
        None,
        max_length=500,
        description="Optional free-text note explaining the decision"
    )

    model_config = {
        "json_schema_extra": {
            "example": {"decision": "claim", "reviewer_note": "Confirmed via ORCID lookup"}
        }
    }


# ---------------------------------------------------------------------------
# Lifespan
# ---------------------------------------------------------------------------


@asynccontextmanager
async def lifespan(app: FastAPI):
    """Initialise DB schema on startup."""
    init_db()
    yield


# ---------------------------------------------------------------------------
# FastAPI app
# ---------------------------------------------------------------------------

app = FastAPI(
    title="Citation Analyzer API",
    description=(
        "Multi-Source Citation Analysis with Deduplication and "
        "Researcher Disambiguation — VIT Group 5, AY 2026-27\n\n"
        "**Full pipeline**: OpenAlex + Semantic Scholar → "
        "dedup → author disambiguation → SELF/EXTERNAL/UNRESOLVED."
    ),
    version="0.3.0",
    lifespan=lifespan,
)

_DEFAULT_ORIGINS = "http://localhost:5173,http://127.0.0.1:5173,http://localhost:3000,http://127.0.0.1:3000"
_ALLOWED_ORIGINS = [
    origin.strip()
    for origin in os.getenv("ALLOWED_ORIGINS", _DEFAULT_ORIGINS).split(",")
    if origin.strip()
]
app.add_middleware(
    CORSMiddleware,
    allow_origins=_ALLOWED_ORIGINS,
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

app.state.limiter = limiter
app.add_exception_handler(RateLimitExceeded, _rate_limit_exceeded_handler)
app.add_middleware(SlowAPIMiddleware)


# ---------------------------------------------------------------------------
# Health / root
# ---------------------------------------------------------------------------


@app.get("/health", tags=["Meta"], summary="Liveness probe")
@limiter.exempt
async def health(request: Request):
    """Returns service status. Never rate-limited."""
    return {
        "status": "ok",
        "service": "citation-analyzer",
        "version": app.version,
        "citation_search_rate_limit": CITATION_SEARCH_RATE_LIMIT,
    }


@app.get("/", include_in_schema=False)
async def root():
    return JSONResponse(
        {"message": "Citation Analyzer API — see /docs for interactive reference."}
    )


@app.get("/researchers", tags=["Researchers"], summary="List all stored researchers")
@limiter.limit("60/minute")
async def list_researchers(request: Request, db: Session = Depends(get_db)):
    """Return a summary list of all researchers stored in the database."""
    return {"researchers": get_all_researchers(db)}


@app.delete("/researcher/{researcher_id}", tags=["Researchers"], summary="Delete a stored researcher")
@limiter.limit("60/minute")
async def delete_researcher_endpoint(
    request: Request,
    researcher_id: str = Path(..., description="OpenAlex author ID or ORCID"),
    db: Session = Depends(get_db),
):
    """Delete a researcher profile and all their stored citations."""
    author_id = sanitize_input(researcher_id) or ""
    if not author_id:
        raise HTTPException(status_code=400, detail="Invalid researcher_id.")
    success = delete_researcher(author_id, db)
    if not success:
        raise HTTPException(status_code=404, detail="Researcher not found in cache.")
    return {"message": f"Researcher {author_id} deleted successfully."}


@app.delete("/researchers", tags=["Researchers"], summary="Clear all stored researchers from cache")
@limiter.limit("30/minute")
async def clear_all_researchers_endpoint(
    request: Request,
    db: Session = Depends(get_db),
):
    """Wipe all cached researcher profiles, publications, and citations."""
    count = delete_all_researchers(db)
    return {"message": f"Cleared {count} stored researchers from cache."}



# ---------------------------------------------------------------------------
# POST /researcher/resolve   (Layer 1)
# ---------------------------------------------------------------------------


@app.post(
    "/researcher/resolve",
    tags=["Researchers"],
    summary="Resolve researcher candidates (Layer 1)",
    response_description=(
        "Up to 3 candidate researcher dicts with openalex_id, display_name, "
        "orcid, affiliation, works_count"
    ),
)
@limiter.limit("30/minute")
async def resolve_researcher_endpoint(
    request: Request,
    body: ResolveRequest,
):
    """
    **Layer 1 — Researcher Identity Resolution**

    Accepts a free-form query (author name, ORCID, or DOI) and returns up to
    three candidate researcher profiles from OpenAlex for the user to select
    from before running the full analysis pipeline.

    - ORCID format `0000-0000-0000-0000` → direct ORCID lookup
    - DOI format `10.xxxx/...` → resolve paper → extract authors
    - Anything else → name search
    """
    clean_query = sanitize_input(body.query) or ""
    if not clean_query:
        raise HTTPException(status_code=400, detail="Empty or invalid query after sanitisation.")

    try:
        candidates = resolve_researcher(clean_query)
    except Exception as exc:
        logger.error("resolve_researcher failed for %r: %s", clean_query, exc)
        raise HTTPException(
            status_code=502,
            detail=f"Upstream API error during researcher resolution: {exc}",
        )

    return {
        "query": clean_query,
        "candidate_count": len(candidates),
        "candidates": candidates,
    }


# ---------------------------------------------------------------------------
# POST /researcher/{researcher_id}/analyze   (Layers 2-5 pipeline)
# ---------------------------------------------------------------------------


@app.post(
    "/researcher/{researcher_id}/analyze",
    tags=["Analysis"],
    summary="Run full citation pipeline (Layers 2-5)",
    response_description=(
        "Citation profile: dedup summary, source-wise counts, "
        "SELF/EXTERNAL/UNRESOLVED breakdown, and the full citing-paper list."
    ),
)
@limiter.limit(CITATION_SEARCH_RATE_LIMIT)
async def analyze_researcher(
    request: Request,
    researcher_id: str = Path(
        ..., description="OpenAlex author ID, e.g. A5039581335"
    ),
    body: AnalyzeRequest = None,
    db: Session = Depends(get_db),
):
    """
    **Full citation pipeline** for one researcher:

    1. **Layer 1** — resolve researcher metadata from OpenAlex
    2. **Layer 2** — fetch raw citations: OpenAlex + Semantic Scholar
       *(each source is isolated — one failure does not cancel others)*
    3. **Layer 3** — deduplicate via DOI → source-ID → fuzzy title+author
    4. **Layer 4** — classify each citing paper SELF / EXTERNAL / UNRESOLVED
    5. **Layer 5** — compute statistics + sign result with HMAC-SHA256

    Returns the full citation profile with:
    - `source_wise_counts` — unique citations per source
    - `deduplicated_count` — after deduplication
    - `self_citation_count`, `external_citation_count`, `unresolved_count`
    - `source_overlap` — multi-source vs exclusive
    - `citations` — full list with DOI, source links, and classification
    - `statistics._integrity` — HMAC-SHA256 tamper-detection checksum

    Rate-limited to **{CITATION_SEARCH_RATE_LIMIT}** per client IP.
    Large requests may take 60-120 s depending on publication count.
    """
    if body is None:
        body = AnalyzeRequest()

    # Sanitise path param
    author_id = sanitize_input(researcher_id) or ""
    if not author_id:
        raise HTTPException(status_code=400, detail="Invalid researcher_id.")

    errors: List[str] = []  # non-fatal errors collected during pipeline

    # ── Layer 1: resolve researcher metadata ──────────────────────────────
    try:
        candidates = resolve_researcher(author_id)
        if candidates:
            researcher_dict = candidates[0]
            # Ensure known_coauthors key exists for Layer 4
            if "known_coauthors" not in researcher_dict:
                researcher_dict["known_coauthors"] = []
        else:
            researcher_dict = {
                "display_name": author_id,
                "openalex_id": author_id,
                "orcid": None,
                "affiliation": None,
                "known_coauthors": [],
            }
    except Exception as exc:
        logger.warning("Layer 1 fallback for %s: %s", author_id, exc)
        errors.append(f"Layer 1 (researcher metadata): {exc}")
        researcher_dict = {
            "display_name": author_id,
            "openalex_id": author_id,
            "orcid": None,
            "affiliation": None,
            "known_coauthors": [],
        }

    # ── Layer 2: fetch raw citations ──────────────────────────────────────
    # get_all_citations_for_researcher already isolates source failures
    # internally; we wrap the whole call for belt-and-suspenders safety.
    google_scholar: Dict[str, Any] = {
        "enabled": False,
        "source": "google_scholar",
        "reason": "Google Scholar data unavailable",
    }
    provider_status: Dict[str, Any] = {}
    try:
        pipeline_result = get_all_citations_for_researcher(
            author_id,
            researcher_name=researcher_dict.get("display_name"),
            max_publications=body.max_publications,
            max_results_per_source=body.max_results_per_source,
        )
        publications: List[Dict] = pipeline_result.get("publications", [])
        researcher_dict["publication_titles"] = [
            publication.get("title", "")
            for publication in publications
            if publication.get("title")
        ]
        researcher_dict["known_coauthors"] = list({
            author
            for publication in publications
            for author in publication.get("authors", [])
            if author and author.lower() != researcher_dict.get("display_name", "").lower()
        })
        # Add Scholar citing papers to the same raw/dedup/classification graph.
        google_scholar = get_google_scholar_profile(
            researcher_dict.get("display_name", author_id)
        )
        scholar_citations = [
            {**paper, "source": "google_scholar"}
            for paper in google_scholar.get("citing_papers", [])
        ]
        publication_by_title = {
            (publication.get("title") or "").strip().lower(): publication["work_id"]
            for publication in publications
            if publication.get("title")
        }
        for paper in scholar_citations:
            cited_title = (paper.get("cited_article_title") or "").strip().lower()
            if cited_title in publication_by_title:
                paper["cited_work_id"] = publication_by_title[cited_title]
        # Key is "raw_citations" — as returned by get_all_citations_for_researcher
        raw_records: List[Dict] = pipeline_result.get("raw_citations", [])
        counts_by_source: Dict = pipeline_result.get("counts_by_source", {})
        provider_status = {"openalex": pipeline_result.get("publication_status", {})}
        source_key_map = {
            "OpenAlex": "openalex_citations",
            "Semantic Scholar": "semantic_scholar",
            "OpenCitations": "opencitations",
            "IEEE/Scopus/WoS": "institutional_sources",
        }
        for source_name, source_report in pipeline_result.get("source_status", {}).items():
            report = dict(source_report)
            report["status"] = (
                "complete"
                if report["works_failed"] == 0 and report["works_empty_or_unavailable"] == 0
                else "partial_or_unavailable"
            )
            report["note"] = (
                "No records were returned for one or more works; this may be a genuine zero "
                "or provider unavailability."
                if report["status"] != "complete" else None
            )
            provider_status[source_key_map.get(source_name, source_name)] = report
        raw_records.extend(scholar_citations)
        counts_by_source["google_scholar"] = len(scholar_citations)
    except Exception as exc:
        logger.error("Layer 2 fatal for %s: %s\n%s", author_id, exc, traceback.format_exc())
        raise HTTPException(
            status_code=502,
            detail=f"Citation retrieval failed: {exc}",
        )

    warning = None
    if not publications:
        warning = (
            "No publications were returned by the publication provider. "
            "Citations from other available providers are still included."
        )
        errors.append("Publication provider returned no publications; continuing with available citation sources.")
        provider_status.setdefault("openalex", {
            "status": "empty_or_unavailable",
            "note": "No publications returned; completeness cannot be confirmed.",
        })

    # ── Layer 3: deduplicate ──────────────────────────────────────────────
    try:
        deduped: List[Dict] = deduplicate_citations(raw_records)
        overlap: Dict = dedup_summary(raw_records, deduped)
    except Exception as exc:
        logger.error("Layer 3 failed: %s", exc)
        errors.append(f"Layer 3 (deduplication): {exc}")
        deduped = raw_records   # fall back to raw if dedup crashes
        overlap = {}

    # ── Layer 4: classify ─────────────────────────────────────────────────
    try:
        classified: List[Dict] = classify_all(deduped, researcher_dict)
    except Exception as exc:
        logger.error("Layer 4 failed: %s", exc)
        errors.append(f"Layer 4 (classification): {exc}")
        classified = deduped

    scholar_classified = [
        paper for paper in classified
        if "google_scholar" in (paper.get("sources") or [])
        or paper.get("source") == "google_scholar"
    ]
    google_scholar["self_citation_count"] = sum(
        paper.get("classification") == "SELF" for paper in scholar_classified
    )
    google_scholar["unresolved_count"] = sum(
        paper.get("classification") == "UNRESOLVED" for paper in scholar_classified
    )
    google_scholar["classified_citing_papers"] = scholar_classified
    scholar_complete = (
        google_scholar.get("enabled", False)
        and google_scholar.get("matched", False)
        and google_scholar.get("profile_fetch", {}).get("complete", False)
        and google_scholar.get("citing_paper_fetch", {}).get("complete", False)
    )
    provider_status["google_scholar"] = {
        "status": "complete" if scholar_complete else (
            "unavailable" if not google_scholar.get("enabled") else "partial_or_unavailable"
        ),
        "note": None if scholar_complete else google_scholar.get(
            "reason",
            "Profile or citation pagination was incomplete.",
        ),
        "profile_articles": google_scholar.get("profile_article_count", 0),
        "profile_citations": google_scholar.get("profile_citation_total"),
        "citing_papers": google_scholar.get("citing_paper_count", 0),
        "fetch": google_scholar.get("citing_paper_fetch") or {},
    }

    # Group by publication for storage
    citations_by_pub: Dict[str, List[Dict]] = {p["work_id"]: [] for p in publications}
    publication_by_doi = {
        (p.get("doi") or "").lower(): p["work_id"]
        for p in publications
        if p.get("doi")
    }
    for cit in classified:
        work_id = cit.get("cited_work_id", "") or publication_by_doi.get(
            (cit.get("cited_doi") or "").lower(),
            "",
        )
        if work_id in citations_by_pub:
            citations_by_pub[work_id].append(cit)

    # ── Layer 5: statistics + integrity signing ───────────────────────────
    try:
        stats = compute_statistics(classified)
    except Exception as exc:
        logger.error("Layer 5 statistics failed: %s", exc)
        errors.append(f"Layer 5 (statistics): {exc}")
        stats = {}

    signed_stats = sign_summary({
        **stats,
        "openalex_author_id": author_id,
        "researcher_name": researcher_dict.get("display_name"),
        "publications_processed": len(publications),
        "raw_source_counts": counts_by_source,
    })

    # ── Persist to DB ─────────────────────────────────────────────────────
    persist_error: Optional[str] = None
    if body.persist:
        try:
            save_pipeline_run(researcher_dict, publications, citations_by_pub, db)
            db.commit()
        except Exception as exc:
            db.rollback()
            persist_error = str(exc)
            logger.error("Persist failed for %s: %s", author_id, exc)
            errors.append(f"Layer 5 (persist): {exc}")

    # ── Build citation list (add source links and persisted IDs) ──────────
    citation_list = _enrich_citations(classified)
    if body.persist:
        stored = get_researcher_citations(author_id, db)
        if stored.get("citations"):
            citation_list = stored["citations"]

    return {
        "researcher": researcher_dict,
        "warning": warning,
        "publications_processed": len(publications),
        "raw_citation_count": len(raw_records),
        "dedup_summary": overlap,
        "statistics": signed_stats,
        "citations": citation_list,
        "google_scholar": google_scholar,
        "provider_status": provider_status,
        "pipeline_errors": errors,          # non-fatal errors (empty = clean run)
    }


# ---------------------------------------------------------------------------
# GET /researcher/{researcher_id}/citations   (cached / DB re-query)
# ---------------------------------------------------------------------------


@app.get(
    "/researcher/{researcher_id}/citations",
    tags=["Citations"],
    summary="Get stored citations for a researcher (no API calls)",
    response_description=(
        "Previously stored citation profile with researcher metadata, "
        "publication list, and classified citations."
    ),
)
@limiter.limit("60/minute")
async def get_researcher_citations_endpoint(
    request: Request,
    researcher_id: str = Path(..., description="OpenAlex author ID"),
    classification: Optional[str] = Query(
        None,
        description="Optional filter: SELF | EXTERNAL | UNRESOLVED",
    ),
    db: Session = Depends(get_db),
):
    """
    Return previously stored citations for a researcher **without calling
    any external APIs**.  Results come from the local SQLite database
    populated by `/researcher/{researcher_id}/analyze`.

    Use the optional `classification` query parameter to filter the
    returned citations to just SELF, EXTERNAL, or UNRESOLVED records.
    """
    author_id = sanitize_input(researcher_id) or ""
    if not author_id:
        raise HTTPException(status_code=400, detail="Invalid researcher_id.")

    if classification and classification not in ("SELF", "EXTERNAL", "UNRESOLVED"):
        raise HTTPException(
            status_code=400,
            detail="classification must be one of: SELF, EXTERNAL, UNRESOLVED",
        )

    result = get_researcher_citations(author_id, db, classification_filter=classification)

    if result["researcher"] is None:
        raise HTTPException(
            status_code=404,
            detail=(
                f"No stored data found for researcher '{author_id}'. "
                "Run /researcher/{researcher_id}/analyze first."
            ),
        )

    result["citations"] = _enrich_citations(result["citations"])
    return result


@app.get(
    "/researcher/{researcher_id}/google-scholar",
    tags=["Citations"],
    summary="Get the Google Scholar profile and citing papers for a cached researcher",
)
@limiter.limit("30/minute")
async def get_cached_google_scholar_endpoint(
    request: Request,
    researcher_id: str = Path(..., description="OpenAlex author ID"),
    db: Session = Depends(get_db),
):
    """Fetch Scholar data for a cached profile without changing DB citations."""
    author_id = sanitize_input(researcher_id) or ""
    if not author_id:
        raise HTTPException(status_code=400, detail="Invalid researcher_id.")

    stored = get_researcher_citations(author_id, db)
    researcher = stored.get("researcher")
    if researcher is None:
        raise HTTPException(status_code=404, detail="Researcher is not cached.")

    profile = get_google_scholar_profile(
        researcher.get("display_name") or author_id
    )
    return {"google_scholar": profile}


# ---------------------------------------------------------------------------
# POST /citations/{citation_id}/review   (Human-in-the-loop Claim/Reject)
# ---------------------------------------------------------------------------


@app.post(
    "/citations/{citation_id}/review",
    tags=["Review"],
    summary="Human Claim/Reject decision for an UNRESOLVED or low-confidence citation",
    response_description="Updated citation record with new classification.",
)
@limiter.limit("120/minute")
async def review_citation(
    request: Request,
    citation_id: int = Path(..., description="Internal DB citation ID (integer)"),
    body: ReviewRequest = ...,
    db: Session = Depends(get_db),
):
    """
    **Human-in-the-loop review** for citations flagged as UNRESOLVED or
    `needs_review=True` by the disambiguation layer.

    - `decision: "claim"` → overrides classification to **SELF**, confidence 1.0
    - `decision: "reject"` → overrides classification to **EXTERNAL**, confidence 1.0

    The override is stored in the database and will persist across API restarts.
    A `reviewer_note` is appended to the citation's evidence field for audit trail.
    """
    citation_row: Optional[DBCitation] = (
        db.query(DBCitation).filter(DBCitation.id == citation_id).first()
    )
    if citation_row is None:
        raise HTTPException(
            status_code=404,
            detail=f"Citation with id={citation_id} not found.",
        )

    # Sanitise reviewer note
    note = sanitize_input(body.reviewer_note or "") or ""

    # Apply decision
    if body.decision == "claim":
        new_classification = "SELF"
        new_confidence = 1.0
        evidence_suffix = f"[HUMAN CLAIM]{': ' + note if note else ''}"
    else:  # reject
        new_classification = "EXTERNAL"
        new_confidence = 1.0
        evidence_suffix = f"[HUMAN REJECT]{': ' + note if note else ''}"

    # Append to existing evidence string
    existing_evidence = citation_row.evidence or ""
    citation_row.evidence = (
        f"{existing_evidence} | {evidence_suffix}".lstrip(" | ")
    )
    citation_row.classification = new_classification
    citation_row.confidence = new_confidence
    citation_row.needs_review = 0  # resolved by human — clear the flag

    try:
        db.commit()
    except Exception as exc:
        db.rollback()
        logger.error("review_citation commit failed: %s", exc)
        raise HTTPException(status_code=500, detail=f"Database error: {exc}")

    return {
        "citation_id": citation_id,
        "title": citation_row.title,
        "previous_needs_review": True,
        "new_classification": new_classification,
        "new_confidence": new_confidence,
        "evidence": citation_row.evidence,
        "message": (
            f"Citation successfully {'claimed as SELF' if body.decision == 'claim' else 'rejected as EXTERNAL'}."
        ),
    }


# ---------------------------------------------------------------------------
# GET /statistics/{researcher_id}   (DB stats + integrity checksum)
# ---------------------------------------------------------------------------


@app.get(
    "/statistics/{researcher_id}",
    tags=["Statistics"],
    summary="Compute and sign statistics from stored data",
)
@limiter.limit("60/minute")
async def get_statistics(
    request: Request,
    researcher_id: str = Path(..., description="OpenAlex author ID"),
    db: Session = Depends(get_db),
):
    """
    Re-compute citation statistics for a researcher from the database.
    Returns the statistics dict signed with an HMAC-SHA256 integrity checksum.
    """
    author_id = sanitize_input(researcher_id) or ""
    if not author_id:
        raise HTTPException(status_code=400, detail="Invalid researcher_id.")

    stats = compute_statistics_from_db(author_id, db)
    if "error" in stats:
        raise HTTPException(status_code=404, detail=stats["error"])

    return sign_summary(stats)


# ---------------------------------------------------------------------------
# POST /verify-integrity
# ---------------------------------------------------------------------------


@app.post(
    "/verify-integrity",
    tags=["Security"],
    summary="Verify the HMAC-SHA256 checksum of a signed statistics summary",
)
@limiter.limit("60/minute")
async def verify_integrity_endpoint(request: Request, summary: Dict[str, Any]):
    """
    POST a statistics dict (previously returned by /analyze or /statistics)
    to verify its integrity checksum has not been tampered with.
    """
    try:
        verify_summary(summary)
        return {"valid": True, "message": "Integrity check passed."}
    except IntegrityError as exc:
        return JSONResponse({"valid": False, "message": str(exc)}, status_code=400)


# ---------------------------------------------------------------------------
# Internal helpers
# ---------------------------------------------------------------------------


def _enrich_citations(citations: List[Dict]) -> List[Dict]:
    """
    Add a ``source_links`` field to each citation dict — a list of
    ``{source, url}`` dicts so the frontend can render clickable links
    to the original record on each database.
    """
    source_url_templates = {
        "openalex": "https://openalex.org/works/{id}",
        "semantic_scholar": "https://www.semanticscholar.org/paper/{id}",
    }

    enriched = []
    for cit in citations:
        c = dict(cit)
        links = []

        doi = cit.get("doi")
        sources = cit.get("sources") or []
        source_id = cit.get("source_id")

        for src in sources:
            if src == "openalex" and source_id:
                links.append({
                    "source": "openalex",
                    "url": f"https://openalex.org/{source_id}",
                })
            elif src == "semantic_scholar" and source_id:
                links.append({
                    "source": "semantic_scholar",
                    "url": f"https://www.semanticscholar.org/paper/{source_id}",
                })
            elif src == "opencitations" and doi:
                links.append({
                    "source": "opencitations",
                    "url": f"https://doi.org/{doi}",
                })
            elif doi:
                links.append({"source": src, "url": f"https://doi.org/{doi}"})

        c["source_links"] = links
        enriched.append(c)
    return enriched
